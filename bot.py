from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, time, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from schedule_parser import WEEKDAYS, Lesson, format_day, gaps_for_day, parse_schedule, parse_schedule_file
from state_store import StateStore

load_dotenv()
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Qyzylorda"))
OWNER_CHAT_ID = int(os.environ["OWNER_CHAT_ID"]) if os.getenv("OWNER_CHAT_ID") else None
REMINDER_MINUTES = int(os.getenv("REMINDER_MINUTES", "15"))
MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
SUNDAY = "Воскресенье"
STATE = StateStore(os.getenv("STATE_DB_PATH", "bot_state.sqlite3"))


def load_lessons() -> list[Lesson]:
    fixture = os.getenv("SCHEDULE_HTML_FILE")
    if fixture:
        return parse_schedule_file(fixture)
    headers = {"Cookie": os.environ["SCHEDULE_COOKIE"]} if os.getenv("SCHEDULE_COOKIE") else {}
    auth = None
    if os.getenv("SCHEDULE_USERNAME"):
        auth = (os.environ["SCHEDULE_USERNAME"], os.environ["SCHEDULE_PASSWORD"])
    response = requests.get(os.environ["SCHEDULE_URL"], headers=headers, auth=auth, timeout=30)
    response.raise_for_status()
    return parse_schedule(response.content.decode("utf-8"))


def target_date(offset: int = 0):
    return datetime.now(TZ).date() + timedelta(days=offset)


def day_for(offset: int = 0) -> str:
    day_index = target_date(offset).weekday()
    return WEEKDAYS[day_index] if day_index < len(WEEKDAYS) else SUNDAY


def date_label(offset: int) -> str:
    target = target_date(offset)
    return f"{target.day} {MONTHS_RU[target.month - 1]} {target.year}"


def lesson_start(lesson: Lesson) -> tuple[int, int]:
    hour, minute = lesson.time[:5].split(":")
    return int(hour), int(minute)


def day_menu(offset: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️", callback_data=f"day:{offset - 1}"), InlineKeyboardButton("📅 Сегодня", callback_data="day:0"), InlineKeyboardButton("➡️", callback_data=f"day:{offset + 1}")],
        [InlineKeyboardButton("⏰ Следующая", callback_data="next"), InlineKeyboardButton("🪟 Окна", callback_data=f"windows:{offset}")],
        [InlineKeyboardButton("🗓 Неделя", callback_data="week"), InlineKeyboardButton("🔄 Обновить", callback_data=f"day:{offset}")],
    ])


def home_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Сегодня", callback_data="day:0"), InlineKeyboardButton("🌅 Завтра", callback_data="day:1")],
        [InlineKeyboardButton("⏰ Следующая пара", callback_data="next"), InlineKeyboardButton("🪟 Мои окна", callback_data="windows:0")],
        [InlineKeyboardButton("🗓 Расписание на неделю", callback_data="week")],
    ])


def next_lesson(lessons: list[Lesson]) -> tuple[Lesson, int] | None:
    now = datetime.now(TZ)
    for offset in range(8):
        date = target_date(offset)
        weekday = day_for(offset)
        for lesson in sorted((item for item in lessons if item.weekday == weekday), key=lesson_start):
            hour, minute = lesson_start(lesson)
            start = datetime.combine(date, time(hour, minute), TZ)
            if start > now:
                return lesson, offset
    return None


def format_next(lessons: list[Lesson]) -> str:
    found = next_lesson(lessons)
    if not found:
        return "⏰ <b>Ближайших пар не найдено.</b>"
    lesson, offset = found
    start = datetime.combine(target_date(offset), time(*lesson_start(lesson)), TZ)
    remaining = max(0, int((start - datetime.now(TZ)).total_seconds() // 60))
    when = "сегодня" if offset == 0 else "завтра" if offset == 1 else date_label(offset)
    details = [f"📚 <b>{escape(lesson.subject)}</b>", f"🕐 {lesson.time}", f"📆 {when}"]
    if lesson.lesson_type:
        details.append(f"🎓 {escape(lesson.lesson_type.capitalize())}")
    if lesson.room:
        details.append(f"🏫 Аудитория: {escape(lesson.room)}")
    details.append(f"⏳ До начала: <b>{remaining} мин</b>")
    return "⏰ <b>Следующая пара</b>\n\n" + "\n".join(details)


def format_windows(lessons: list[Lesson], offset: int) -> str:
    weekday = day_for(offset)
    gaps = gaps_for_day(lessons, weekday)
    header = f"🪟 <b>Окна: {escape(weekday)}</b>\n<blockquote>{date_label(offset)}</blockquote>"
    if not gaps:
        return f"{header}\n\n✨ Окон от 30 минут нет."
    rows = [f"• {gap.start}–{gap.end} — <b>{gap.minutes // 60} ч {gap.minutes % 60} мин</b>" for gap in gaps]
    return f"{header}\n\n" + "\n".join(rows)


def schedule_snapshot(lessons: list[Lesson]) -> str:
    return json.dumps([f"{item.weekday}|{item.time}|{item.subject}|{item.lesson_type}|{item.room}" for item in lessons], ensure_ascii=False)


async def send_day(chat_id: int, weekday: str, context: ContextTypes.DEFAULT_TYPE, offset: int = 0) -> None:
    try:
        message = format_day(load_lessons(), weekday, date_label(offset))
    except Exception:
        logging.exception("Could not load schedule")
        message = "Не удалось обновить расписание. Попробую снова при следующем запросе."
    await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML", reply_markup=day_menu(offset))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 <b>Привет! Я помогу следить за расписанием.</b>\n\n"
        "Показываю только занятия всей группы и 1-й подгруппы.",
        parse_mode="HTML", reply_markup=home_menu(),
    )


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_day(update.effective_chat.id, day_for(), context)


async def tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_day(update.effective_chat.id, day_for(1), context, 1)


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        lessons = load_lessons()
        message = "\n\n".join(format_day(lessons, weekday) for weekday in WEEKDAYS)
    except Exception:
        logging.exception("Could not load schedule")
        message = "Не удалось обновить расписание."
    await update.message.reply_text(message, parse_mode="HTML", reply_markup=home_menu())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        action = query.data
        lessons = load_lessons()
        if action == "week":
            message, markup = "\n\n".join(format_day(lessons, weekday) for weekday in WEEKDAYS), home_menu()
        elif action == "next":
            message, markup = format_next(lessons), home_menu()
        elif action.startswith("windows:"):
            offset = max(-14, min(42, int(action.split(":", 1)[1])))
            message, markup = format_windows(lessons, offset), day_menu(offset)
        else:
            offset = max(-14, min(42, int(action.split(":", 1)[1])))
            message, markup = format_day(lessons, day_for(offset), date_label(offset)), day_menu(offset)
        await query.edit_message_text(message, parse_mode="HTML", reply_markup=markup)
    except BadRequest as error:
        if "Message is not modified" in str(error):
            await query.answer("Расписание уже актуально")
            return
        raise
    except Exception:
        logging.exception("Could not load schedule")
        await query.edit_message_text("Не удалось обновить расписание. Попробуйте ещё раз.", reply_markup=home_menu())


async def morning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if OWNER_CHAT_ID:
        await send_day(OWNER_CHAT_ID, day_for(), context)


async def evening_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if OWNER_CHAT_ID:
        await send_day(OWNER_CHAT_ID, day_for(1), context, 1)


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not OWNER_CHAT_ID:
        return
    now = datetime.now(TZ)
    for lesson in load_lessons():
        if lesson.weekday != day_for():
            continue
        hour, minute = lesson_start(lesson)
        start = datetime.combine(now.date(), time(hour, minute), TZ)
        seconds = (start - now).total_seconds()
        if REMINDER_MINUTES * 60 - 35 <= seconds <= REMINDER_MINUTES * 60 + 35:
            key = f"{now.date()}|{lesson.time}|{lesson.subject}|{lesson.room}"
            if STATE.remember_reminder(key):
                room = f"\n🏫 Аудитория: {escape(lesson.room)}" if lesson.room else ""
                await context.bot.send_message(
                    OWNER_CHAT_ID,
                    f"🔔 <b>Через {REMINDER_MINUTES} мин пара</b>\n\n📚 <b>{escape(lesson.subject)}</b>\n🕐 {lesson.time}{room}",
                    parse_mode="HTML",
                )


async def schedule_change_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not OWNER_CHAT_ID:
        return
    try:
        snapshot = schedule_snapshot(load_lessons())
        previous = STATE.get("schedule_snapshot")
        if previous is None:
            STATE.set("schedule_snapshot", snapshot)
        elif previous != snapshot:
            STATE.set("schedule_snapshot", snapshot)
            await context.bot.send_message(OWNER_CHAT_ID, "⚠️ <b>Расписание изменилось.</b>\nОткройте «Сегодня» или «Неделя», чтобы увидеть актуальную версию.", parse_mode="HTML", reply_markup=home_menu())
    except Exception:
        logging.exception("Could not check schedule changes")


def clock(variable: str, default: str) -> time:
    hour, minute = map(int, os.getenv(variable, default).split(":"))
    return time(hour, minute, tzinfo=TZ)


def main() -> None:
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("tomorrow", tomorrow))
    app.add_handler(CommandHandler("week", week))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.job_queue.run_daily(morning_job, clock("MORNING_SEND_TIME", "07:00"))
    app.job_queue.run_daily(evening_job, clock("EVENING_SEND_TIME", "19:00"))
    app.job_queue.run_repeating(reminder_job, interval=30, first=10)
    app.job_queue.run_repeating(schedule_change_job, interval=900, first=15)
    app.run_polling()


if __name__ == "__main__":
    main()
