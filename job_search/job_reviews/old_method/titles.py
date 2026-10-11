"""User-facing names show list kind and coverage, independent of completion time."""
from zoneinfo import ZoneInfo

from ..contracts import timestamp


def list_title(kind, start, end, *, part=1, parts=1):
    zone = ZoneInfo('America/Chicago')
    start, end = timestamp(start).astimezone(zone), timestamp(end).astimezone(zone)
    def day(value):
        return f'{value:%b} {value.day}, {value.year}'
    def clock(value):
        return value.strftime('%I:%M %p').lstrip('0')
    if start.date() == end.date() and start.utcoffset() == end.utcoffset():
        period = f'{day(start)} · {clock(start)}–{clock(end)} {end:%Z}'
    else:
        period = f'{day(start)}, {clock(start)} {start:%Z} → {day(end)}, {clock(end)} {end:%Z}'
    return f'{kind.title()} · {period}' + (f' · Part {part}/{parts}' if parts > 1 else '')
