"""Deterministic arithmetic for dates extracted by the reasoning provider."""

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import dateparser
from dateparser.search import search_dates
from pydantic import BaseModel, ConfigDict, Field


class DateBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_phrase: str = Field(min_length=1, max_length=250)


class AbsoluteDate(DateBase):
    kind: Literal["absolute"]
    local_date: date
    local_time: time


class RelativeDate(DateBase):
    kind: Literal["relative"]
    amount: float = Field(gt=0)
    unit: Literal["minutes", "hours", "days", "weeks"]


class WeekdayDate(DateBase):
    kind: Literal["weekday"]
    weekday: int = Field(ge=0, le=6, description="Monday=0")
    local_time: time


class UnresolvedDate(DateBase):
    kind: Literal["unresolved"]
    question: str = Field(min_length=1, max_length=300)


DateSpec = Annotated[
    AbsoluteDate | RelativeDate | WeekdayDate | UnresolvedDate, Field(discriminator="kind")
]
VAGUE = re.compile(r"вечер\w*|после\s+обеда|нескольк\w*\s+дн|утром|днём|ночью", re.I)


CLOCK = re.compile(r"\b\d{1,2}:\d{2}\b|\b(?:в|к|at)\s+\d{1,2}\b", re.I)
INTERVAL = re.compile(
    r"(?:через|in)\s+.+(?:час|минут|секунд|дн|день|недел|hour|minute|day|week)", re.I
)
WEEKDAYS = ("понедельн", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресен")
CALENDAR = re.compile(
    r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}|\d{1,2}\s+(?:янв|фев|мар|апр|мая|июн|июл|авг|сен|окт|ноя|дек)",
    re.I,
)


def resolve_date(spec, reference: datetime, timezone: str, source: str):
    phrase = spec.source_phrase.casefold().strip()
    if phrase not in source.casefold():
        return None, "Укажите дату и время в сообщении."
    precise_source = CLOCK.search(source) or INTERVAL.search(source)
    if VAGUE.search(source) and not precise_source:
        return None, "Уточните точную дату и время."
    if VAGUE.search(phrase) and not (CLOCK.search(phrase) or INTERVAL.search(phrase)):
        return None, "Уточните точную дату и время."
    # The source must itself contain a precise time. A model-selected non-temporal
    # substring or a guessed absolute/relative value never supplies that precision.
    if not (CLOCK.search(phrase) or INTERVAL.search(phrase)):
        return None, "Уточните дату и точное время."
    local = reference.astimezone(ZoneInfo(timezone))
    parse_settings = {
        "RELATIVE_BASE": local.replace(tzinfo=None),
        "TIMEZONE": timezone,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
    }
    # Recover complete source spans, including calendar anchors the model may have
    # omitted from its substring (e.g. «в 15:00» inside «завтра в 15:00»).
    candidates = [
        (span.casefold(), value)
        for span, value in (
            search_dates(source, languages=["ru", "en"], settings=parse_settings) or []
        )
        if CLOCK.search(span) or INTERVAL.search(span)
    ]
    matching = [(span, value) for span, value in candidates if phrase in span]
    if len(matching) == 1:
        phrase, parsed = matching[0]
    elif len(candidates) == 1:
        phrase, parsed = candidates[0]
    elif len(candidates) > 1:
        return None, "В сообщении несколько временных выражений. Уточните срок поручения."
    else:
        parsed = dateparser.parse(phrase, languages=["ru", "en"], settings=parse_settings)
    if parsed is None:
        return None, (
            spec.question
            if isinstance(spec, UnresolvedDate)
            else "Уточните дату и время в явном виде."
        )
    # dateparser prefers next week even on Friday before the requested clock time;
    # the prototype contract explicitly chooses the nearest suitable Friday.
    weekday = next((i for i, stem in enumerate(WEEKDAYS) if stem in phrase), None)
    if weekday is not None and not CALENDAR.search(phrase) and "через" not in phrase:
        target = local.date() + timedelta(days=(weekday - local.weekday()) % 7)
        parsed = datetime.combine(target, parsed.time().replace(tzinfo=None), local.tzinfo)
        if parsed <= local or (target == local.date() and "следующ" in phrase):
            parsed += timedelta(days=7)
    if parsed <= local:
        return None, "Это время уже прошло. Уточните будущую дату и время."
    return parsed.astimezone(UTC), None
