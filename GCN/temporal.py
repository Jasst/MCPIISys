"""Время события: нормализация дат, которые извлекает LLM из источников.

Разделены три времени факта:
  created      — когда факт записан в память (служебное, не менять);
  last_accessed — когда факт последний раз использовался (влияет на свежесть поиска);
  event_time   — когда произошло то, о чём факт (дата из источника, может быть неизвестна).
Этот модуль отвечает только за event_time: приводит строку даты к канонической форме.
"""
import re
from typing import Optional

_ISO = re.compile(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?$")
_UNKNOWN = {"", "unknown", "неизвестно", "н/д", "none", "null", "-"}


def normalize_event_time(raw) -> Optional[str]:
    """'2024-03-05' -> '2024-03-05', '2024-3' -> '2024-03', '2024' -> '2024'.
    Неизвестные или некорректные даты -> None (факт сохраняется без event_time)."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in _UNKNOWN:
        return None
    m = _ISO.match(s)
    if not m:
        return None
    year, month, day = m.group(1), m.group(2), m.group(3)
    if not (1900 <= int(year) <= 2100):
        return None
    if month is None:
        return year
    mo = int(month)
    if not 1 <= mo <= 12:
        return None
    if day is None:
        return f"{year}-{mo:02d}"
    d = int(day)
    if not 1 <= d <= 31:
        return None
    return f"{year}-{mo:02d}-{d:02d}"
