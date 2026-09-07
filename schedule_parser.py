"""Extract a personal subgroup schedule from the EKTU semester schedule page."""

from __future__ import annotations

import re
from html import escape
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup, Tag

TARGET_SUBGROUP = 1
WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота")


@dataclass(frozen=True)
class Lesson:
    weekday: str
    time: str
    room: str | None
    subject: str
    lesson_type: str | None
    teacher: str | None


@dataclass(frozen=True)
class Gap:
    start: str
    end: str
    minutes: int


def _normalise(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _belongs_to_target_subgroup(text: str) -> bool:
    """Include whole-group lessons and only the configured subgroup."""
    compact = _normalise(text).lower()
    if "вся группа" in compact:
        return True
    return bool(re.search(r"\(\s*1-я\s+подгруппа\s*\)", compact))


def _lesson_blocks(cell: Tag) -> list[Tag]:
    """Nested cells separate simultaneous lessons for different subgroups."""
    nested = cell.find("table")
    if nested is None:
        return [cell]
    return [td for td in nested.find_all("td") if not td.find("table")]


def _parse_block(weekday: str, time: str, block: Tag) -> Lesson | None:
    text = _normalise(block.get_text(" ", strip=True))
    if not text or not _belongs_to_target_subgroup(text):
        return None

    room_match = re.search(r"\[([^\]]+)\]", text)
    room = room_match.group(1) if room_match else None
    subject_node = block.find("span", class_=lambda classes: classes and "bold" in classes)
    subject = _normalise(subject_node.get_text(" ", strip=True)) if subject_node else "Занятие"

    chunks = [_normalise(chunk) for chunk in block.get_text("\n", strip=True).split("\n")]
    chunks = [chunk for chunk in chunks if chunk]
    subject_index = next((i for i, item in enumerate(chunks) if item == subject), -1)
    lesson_type = chunks[subject_index + 1] if subject_index >= 0 and subject_index + 1 < len(chunks) else None
    teacher = next((item for item in chunks if "преподавател" in item.lower()), None)
    return Lesson(weekday, time, room, subject, lesson_type, teacher)


def parse_schedule(html: str) -> list[Lesson]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="tblSchedule")
    if table is None:
        raise ValueError("Не найдена таблица расписания tblSchedule")

    rows = table.find_all("tr", recursive=False)
    if not rows:
        return []
    headers = [_normalise(td.get_text(" ", strip=True)) for td in rows[0].find_all("td", recursive=False)][1:]
    lessons: list[Lesson] = []
    for row in rows[1:]:
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        time = _normalise(cells[0].get_text(" ", strip=True))
        if not re.match(r"^\d{2}:\d{2}\s*-\s*\d{2}:\d{2}$", time):
            continue
        for weekday, cell in zip(headers, cells[1:]):
            if weekday not in WEEKDAYS:
                continue
            for block in _lesson_blocks(cell):
                lesson = _parse_block(weekday, time, block)
                if lesson:
                    lessons.append(lesson)
    return lessons


def parse_schedule_file(path: str | Path) -> list[Lesson]:
    return parse_schedule(Path(path).read_text(encoding="utf-8"))


def _minutes(value: str) -> tuple[int, int]:
    start, end = re.findall(r"\d{2}:\d{2}", value)
    return (
        int(start[:2]) * 60 + int(start[3:]),
        int(end[:2]) * 60 + int(end[3:]),
    )


def _duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    if hours:
        return f"{hours} ч"
    return f"{rest} мин"


def gaps_for_day(lessons: list[Lesson], weekday: str, minimum_minutes: int = 30) -> list[Gap]:
    """Return meaningful breaks between lessons; 30+ minutes counts as a window."""
    selected = sorted((item for item in lessons if item.weekday == weekday), key=lambda item: _minutes(item.time)[0])
    gaps: list[Gap] = []
    last_end: int | None = None
    for lesson in selected:
        start, end = _minutes(lesson.time)
        if last_end is not None and start - last_end >= minimum_minutes:
            gaps.append(Gap(f"{last_end // 60:02}:{last_end % 60:02}", f"{start // 60:02}:{start % 60:02}", start - last_end))
        last_end = max(last_end or 0, end)
    return gaps


def format_day(lessons: list[Lesson], weekday: str, date_label: str | None = None) -> str:
    selected = sorted(
        (lesson for lesson in lessons if lesson.weekday == weekday),
        key=lambda lesson: _minutes(lesson.time)[0],
    )
    heading = f"📅 <b>{escape(weekday)}</b>"
    if date_label:
        heading += f"\n<blockquote>{escape(date_label)}</blockquote>"
    if not selected:
        return f"{heading}\n\n🎉 <b>Пар нет</b>\nМожно немного выдохнуть."
    lines = [heading]
    for index, lesson in enumerate(selected):
        details = [f"📚 <b>{escape(lesson.subject)}</b>"]
        if lesson.lesson_type:
            details.append(f"🎓 {escape(lesson.lesson_type.capitalize())}")
        if lesson.room:
            details.append(f"🏫 Аудитория: {escape(lesson.room)}")
        lines.append(f"🕐 <b>{escape(lesson.time)}</b>\n" + "\n".join(details))
        if index + 1 < len(selected):
            current_end = _minutes(lesson.time)[1]
            next_start = _minutes(selected[index + 1].time)[0]
            gap = next_start - current_end
            if gap > 0:
                label = "🪟 <b>Окно</b>" if gap >= 30 else "☕ Перемена"
                lines.append(f"{label}: {_duration(gap)}")
    return "\n\n────────────\n\n".join(lines)
