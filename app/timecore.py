"""Leap-second aware time-scale conversion core.

All arithmetic is integer arithmetic on purpose: the specification forbids
floating point anywhere in the conversions.

Time scales
-----------
* TAI: a linear count of SI seconds.  Internally represented as signed
  integer nanoseconds since the TAI instant 1970-01-01T00:00:00 TAI.
* GPS: weeks and seconds-in-week since the GPS epoch 1980-01-06T00:00:00
  UTC.  At that instant TAI-UTC was already 19 s, so GPS time tracks
  TAI minus a constant 19 s and never sees leap seconds.
* UTC: civil time with occasional positive leap seconds.  A UTC seconds
  value of 60 is valid **only** as 23:59:60 on a day that ends in a leap
  second.

During the leap second itself (e.g. 2016-12-31T23:59:60Z) the offset
TAI-UTC is still the *old* value (36 s); it becomes 37 s only at
2017-01-01T00:00:00Z.

The leap second table is the public IANA tzdb ``leap-seconds.list`` in the
version that added the positive leap second on 2016-12-31 (IERS Bulletin
C 52, tzdata 2016g, committed 2016-07-19).  That file declares an expiry
of 2017-06-28T00:00:00Z, which is taken to be the end of the supported era.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NS_PER_SECOND = 1_000_000_000
SECONDS_PER_DAY = 86_400
SECONDS_PER_WEEK = 7 * SECONDS_PER_DAY
NS_PER_DAY = SECONDS_PER_DAY * NS_PER_SECOND

# 1980-01-06T00:00:00 UTC as a raw UTC label (seconds since 1970-01-01,
# ignoring leap seconds -- the well-known GPS POSIX epoch constant).
GPS_EPOCH_LABEL_NS = 315_964_800 * NS_PER_SECOND
# TAI = GPS + 19 s, always.
GPS_TAI_OFFSET_NS = 19 * NS_PER_SECOND
GPS_EPOCH_TAI_NS = GPS_EPOCH_LABEL_NS + GPS_TAI_OFFSET_NS

MIN_YEAR = 1972

# leap-seconds.list "#@" expiry line of tzdata 2016g (IERS Bulletin C 52):
# 2017-06-28T00:00:00Z.  UTC instants at or past it are outside the
# supported era.
TABLE_EXPIRY_UTC = (2017, 6, 28, 0, 0, 0, 0)


# ---------------------------------------------------------------------------
# Leap second table (IANA tzdb leap-seconds.list, tzdata 2016g)
# ---------------------------------------------------------------------------

# UTC civil date of each boundary at whose 00:00:00 the TAI-UTC offset
# increases by one.  The first row opens the current-form UTC table on
# 1972-01-01 with offset 10; every subsequent row is a positive leap
# second on the preceding day.
_LEAP_DATES: list[tuple[int, int, int]] = [
    (1972, 1, 1),
    (1972, 7, 1),
    (1973, 1, 1),
    (1974, 1, 1),
    (1975, 1, 1),
    (1976, 1, 1),
    (1977, 1, 1),
    (1978, 1, 1),
    (1979, 1, 1),
    (1980, 1, 1),
    (1981, 7, 1),
    (1982, 7, 1),
    (1983, 7, 1),
    (1985, 7, 1),
    (1988, 1, 1),
    (1990, 1, 1),
    (1991, 1, 1),
    (1992, 7, 1),
    (1993, 7, 1),
    (1994, 7, 1),
    (1996, 1, 1),
    (1997, 7, 1),
    (1999, 1, 1),
    (2006, 1, 1),
    (2009, 1, 1),
    (2012, 7, 1),
    (2015, 7, 1),
    (2017, 1, 1),  # leap second inserted 2016-12-31T23:59:60
]

_FIRST_OFFSET = 10


@dataclass(frozen=True)
class LeapBound:
    date: tuple[int, int, int]   # UTC civil date of the 00:00:00 boundary
    tai_ns: int                  # TAI instant of that boundary
    offset: int                  # TAI-UTC (s) valid from that instant on


def _build_table() -> list[LeapBound]:
    bounds: list[LeapBound] = []
    for i, (y, m, d) in enumerate(_LEAP_DATES):
        offset = _FIRST_OFFSET + i
        label_ns = _utc_civil_to_label_ns(y, m, d, 0, 0, 0, 0)
        bounds.append(
            LeapBound(date=(y, m, d), tai_ns=label_ns + offset * NS_PER_SECOND,
                      offset=offset)
        )
    return bounds


# ---------------------------------------------------------------------------
# Proleptic Gregorian civil-time helpers (integer only)
# ---------------------------------------------------------------------------

def _is_leap_gregorian(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        return 29 if _is_leap_gregorian(year) else 28
    if month in (4, 6, 9, 11):
        return 30
    return 31


# Howard Hinnant's days_from_civil / civil_from_days (public domain).
def _days_from_civil(year: int, month: int, day: int) -> int:
    y = year - (1 if month <= 2 else 0)
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _civil_from_days(z: int) -> tuple[int, int, int]:
    z += 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    year = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    if month <= 2:
        year += 1
    return year, month, day


def _utc_civil_to_label_ns(
    year: int, month: int, day: int, hour: int, minute: int, second: int,
    nanos: int,
) -> int:
    """Raw UTC label ns since 1970 (second may be 60, i.e. next midnight)."""
    days = _days_from_civil(year, month, day)
    return (
        days * NS_PER_DAY
        + hour * 3600 * NS_PER_SECOND
        + minute * 60 * NS_PER_SECOND
        + second * NS_PER_SECOND
        + nanos
    )


# Table construction (after _utc_civil_to_label_ns is defined).
LEAP_BOUNDS: list[LeapBound] = _build_table()

# UTC civil dates on which 23:59:60 exists (day before each boundary
# except the 1972 table start).
LEAP_SECOND_DATES = frozenset(
    _civil_from_days(_days_from_civil(*b.date) - 1)
    for b in LEAP_BOUNDS[1:]
)

MIN_SUPPORTED_TAI_NS = LEAP_BOUNDS[0].tai_ns

# Expiry as a TAI instant (offset 37 applies in June 2017).
_ey, _em, _ed, _eh, _emi, _es, _ens = TABLE_EXPIRY_UTC
_expiry_offset = LEAP_BOUNDS[-1].offset
TABLE_EXPIRY_TAI_NS = (
    _utc_civil_to_label_ns(_ey, _em, _ed, _eh, _emi, _es, _ens)
    + _expiry_offset * NS_PER_SECOND
)


class TimeConversionError(ValueError):
    """Invalid input for a single event; message is caller-safe."""


@dataclass(frozen=True)
class NormalizedEvent:
    tai_ns: int
    utc_canonical: str
    utc_minus_tai: int  # e.g. -37 means TAI-UTC = 37 s

    def to_response(self) -> dict[str, object]:
        return {
            "taiNanoseconds": str(self.tai_ns),
            "utc": self.utc_canonical,
            "utcTaiOffsetSeconds": str(self.utc_minus_tai),
        }


# ---------------------------------------------------------------------------
# UTC parsing / formatting
# ---------------------------------------------------------------------------

_UTC_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"
    r"T(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})(?P<frac>\.\d{1,9})?Z$"
)


def _format_fraction(nanos: int) -> str:
    if nanos == 0:
        return ""
    return "." + f"{nanos:09d}".rstrip("0")


def _format_utc(
    year: int, month: int, day: int,
    hour: int, minute: int, second: int, nanos: int,
) -> str:
    return (
        f"{year:04d}-{month:02d}-{day:02d}"
        f"T{hour:02d}:{minute:02d}:{second:02d}{_format_fraction(nanos)}Z"
    )


def parse_utc(text: object) -> tuple[int, int, int, int, int, int, int]:
    if not isinstance(text, str):
        raise TimeConversionError(
            "utc must be a string ending in Z, e.g. 2016-12-31T23:59:59Z"
        )
    m = _UTC_RE.match(text)
    if not m:
        raise TimeConversionError(
            "utc must match YYYY-MM-DDTHH:MM:SS[.fraction]Z with the Z"
            " suffix; seconds may be 60 only at 23:59:60 of a leap second"
            " day; fractional part may have at most 9 digits"
        )
    frac = m.group("frac") or ""
    nanos = int((frac[1:] + "000000000")[:9]) if frac else 0
    return (
        int(m.group("y")), int(m.group("m")), int(m.group("d")),
        int(m.group("h")), int(m.group("mi")), int(m.group("s")), nanos,
    )


def _check_civil_validity(
    year: int, month: int, day: int, hour: int, minute: int, second: int,
) -> None:
    if year < MIN_YEAR:
        raise TimeConversionError(
            f"year {year} is before the supported era (UTC table starts on"
            " 1972-01-01)"
        )
    if not 1 <= month <= 12:
        raise TimeConversionError(f"invalid calendar date: month {month}")
    dim = _days_in_month(year, month)
    if not 1 <= day <= dim:
        raise TimeConversionError(
            f"invalid calendar date: {year:04d}-{month:02d}-{day:02d}"
            f" does not exist (month has {dim} days)"
        )
    if not 0 <= hour <= 23:
        raise TimeConversionError(f"invalid time: hour {hour} out of [0,23]")
    if not 0 <= minute <= 59:
        raise TimeConversionError(
            f"invalid time: minute {minute} out of [0,59]"
        )
    if second == 60:
        if (year, month, day) not in LEAP_SECOND_DATES:
            raise TimeConversionError(
                f"seconds value 60 is not a real leap second position:"
                f" {year:04d}-{month:02d}-{day:02d}T23:59:60 is not in the"
                " leap-second table"
            )
        if (hour, minute) != (23, 59):
            raise TimeConversionError(
                "seconds value 60 is only valid at 23:59:60 of a leap"
                " second day"
            )
    elif not 0 <= second <= 59:
        raise TimeConversionError(
            f"invalid time: second {second} out of [0,59]"
        )


# ---------------------------------------------------------------------------
# UTC <-> TAI
# ---------------------------------------------------------------------------

def _offset_at_utc_label(
    year: int, month: int, day: int,
    hour: int, minute: int, second: int, nanos: int,
) -> int:
    """TAI-UTC in effect at a regular (second <= 59) UTC label."""
    target_days = _days_from_civil(year, month, day)
    offset = _FIRST_OFFSET
    for b in LEAP_BOUNDS:
        # Boundaries always occur at 00:00:00 of their civil date.
        if _days_from_civil(*b.date) <= target_days:
            offset = b.offset
        else:
            break
    return offset


def utc_to_tai(
    year: int, month: int, day: int,
    hour: int, minute: int, second: int, nanos: int,
) -> int:
    if second == 60:
        # Physical leap second, e.g. 2016-12-31T23:59:60.5: the raw label
        # with 60 equals the next midnight; the old offset (36) applies.
        # Equivalent integer computation: label at second 59 + new offset.
        new_offset = _bounds_after(
            _civil_from_days(_days_from_civil(year, month, day) + 1)
        ).offset
        label59_ns = _utc_civil_to_label_ns(
            year, month, day, hour, minute, 59, nanos
        )
        return label59_ns + new_offset * NS_PER_SECOND
    offset = _offset_at_utc_label(
        year, month, day, hour, minute, second, nanos
    )
    label_ns = _utc_civil_to_label_ns(
        year, month, day, hour, minute, second, nanos
    )
    return label_ns + offset * NS_PER_SECOND


def _bounds_after(date: tuple[int, int, int]) -> LeapBound:
    for b in LEAP_BOUNDS:
        if b.date == date:
            return b
    raise TimeConversionError(f"internal: no boundary at {date}")


def tai_to_utc(
    tai_ns: int,
) -> tuple[int, int, int, int, int, int, int, int]:
    """TAI ns -> (y, m, d, h, mi, s, nanos, utc_minus_tai).

    A TAI instant inside a positive leap second is rendered with second 60
    on the preceding day and carries the *old* UTC-TAI offset.
    """
    idx = 0
    for i, b in enumerate(LEAP_BOUNDS):
        if tai_ns >= b.tai_ns:
            idx = i
        else:
            break
    offset = _FIRST_OFFSET + idx
    label_ns = tai_ns - offset * NS_PER_SECOND
    days, rem = divmod(label_ns, NS_PER_DAY)
    year, month, day = _civil_from_days(days)
    hour, rem = divmod(rem, 3600 * NS_PER_SECOND)
    minute, rem = divmod(rem, 60 * NS_PER_SECOND)
    second, nanos = divmod(rem, NS_PER_SECOND)

    # A physical positive leap second is the TAI interval [B - 1s, B)
    # before the next boundary; with the old offset subtracted its raw
    # label reads as the boundary date 00:00:00.x, which we render as the
    # previous day's 23:59:60.x with the old offset.
    if idx + 1 < len(LEAP_BOUNDS):
        nxt = LEAP_BOUNDS[idx + 1]
        if nxt.tai_ns - NS_PER_SECOND <= tai_ns < nxt.tai_ns:
            into = tai_ns - (nxt.tai_ns - NS_PER_SECOND)
            leap_nanos = into  # 0 .. NS_PER_SECOND-1
            py, pm, pd = _civil_from_days(_days_from_civil(*nxt.date) - 1)
            return py, pm, pd, 23, 59, 60, leap_nanos, -offset

    return year, month, day, hour, minute, second, nanos, -offset


# ---------------------------------------------------------------------------
# Event-level normalization
# ---------------------------------------------------------------------------

def _check_supported_era(tai_ns: int) -> None:
    if tai_ns < MIN_SUPPORTED_TAI_NS:
        raise TimeConversionError(
            "instant is before the supported era (table starts at"
            " 1972-01-01T00:00:00Z with UTC-TAI offset -10 s)"
        )
    if tai_ns >= TABLE_EXPIRY_TAI_NS:
        raise TimeConversionError(
            "instant is at or beyond the leap-second table expiry"
            " 2017-06-28T00:00:00Z; this table does not cover that era"
        )


def normalize_utc_event(text: object) -> NormalizedEvent:
    y, mo, d, h, mi, s, ns = parse_utc(text)
    _check_civil_validity(y, mo, d, h, mi, s)
    tai_ns = utc_to_tai(y, mo, d, h, mi, s, ns)
    _check_supported_era(tai_ns)
    ry, rm, rd, rh, rmi, rs, rns, utc_minus_tai = tai_to_utc(tai_ns)
    return NormalizedEvent(
        tai_ns=tai_ns,
        utc_canonical=_format_utc(ry, rm, rd, rh, rmi, rs, rns),
        utc_minus_tai=utc_minus_tai,
    )


def _strict_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TimeConversionError(f"{field} must be an integer")
    return value


def normalize_gps_event(
    week: object, sow: object, nanos: object = 0,
) -> NormalizedEvent:
    w = _strict_int(week, "gpsWeek")
    s = _strict_int(sow, "gpsSecondsInWeek")
    ns = _strict_int(nanos, "gpsNanoseconds")
    if w < 0:
        raise TimeConversionError(
            f"gpsWeek out of range: {w} is negative (GPS epoch is week 0,"
            " 1980-01-06)"
        )
    if not 0 <= s < SECONDS_PER_WEEK:
        raise TimeConversionError(
            f"gpsSecondsInWeek out of range: {s} is not in [0, 604799]"
        )
    if not 0 <= ns < NS_PER_SECOND:
        raise TimeConversionError(
            f"gpsNanoseconds out of range: {ns} is not in [0, 999999999]"
        )
    tai_ns = (
        GPS_EPOCH_LABEL_NS
        + w * SECONDS_PER_WEEK * NS_PER_SECOND
        + s * NS_PER_SECOND
        + ns
        + GPS_TAI_OFFSET_NS
    )
    _check_supported_era(tai_ns)
    ry, rm, rd, rh, rmi, rs, rns, utc_minus_tai = tai_to_utc(tai_ns)
    return NormalizedEvent(
        tai_ns=tai_ns,
        utc_canonical=_format_utc(ry, rm, rd, rh, rmi, rs, rns),
        utc_minus_tai=utc_minus_tai,
    )


# ---------------------------------------------------------------------------
# Onboard clock correlation segments
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TickSegment:
    """One half-open onboard-clock segment pinned to the TAI axis.

    Covers ticks ``[start, end)`` of one clock partition and maps a tick
    ``t`` to TAI nanoseconds as::

        tai(t) = anchor_tai_ns + (t - anchor_tick) * ns_num / ns_den

    where ``ns_num / ns_den`` is the positive rational nanoseconds-per-tick
    scale of the segment.  Every value stays an integer: the result is only
    defined when the division has no remainder (a sub-nanosecond mapping is
    a caller error, never a truncation).
    """

    start: int
    end: int
    anchor_tick: int
    anchor_tai_ns: int
    ns_per_tick_num: int
    ns_per_tick_den: int

    def contains(self, tick: int) -> bool:
        return self.start <= tick < self.end

    def map_tick(self, tick: int) -> int:
        """Map an onboard tick to exact integer TAI nanoseconds.

        Raises TimeConversionError when the rational scale would leave a
        sub-nanosecond remainder for this tick.
        """
        delta_ticks = tick - self.anchor_tick
        # anchor_tai * den + delta * num, all divided by den -- integer only.
        unscaled = (
            self.anchor_tai_ns * self.ns_per_tick_den
            + delta_ticks * self.ns_per_tick_num
        )
        scaled, remainder = divmod(unscaled, self.ns_per_tick_den)
        if remainder:
            raise TimeConversionError(
                f"onboard tick {tick} does not map to an integer number of"
                f" TAI nanoseconds with scale {self.ns_per_tick_num}/"
                f"{self.ns_per_tick_den} ns per tick (sub-nanosecond"
                f" remainder {remainder}/{self.ns_per_tick_den})"
            )
        return scaled


def normalize_onboard_event(segment: TickSegment, tick: int) -> NormalizedEvent:
    """Map an onboard tick through a validated segment and render it."""
    tai_ns = segment.map_tick(tick)
    _check_supported_era(tai_ns)
    ry, rm, rd, rh, rmi, rs, rns, utc_minus_tai = tai_to_utc(tai_ns)
    return NormalizedEvent(
        tai_ns=tai_ns,
        utc_canonical=_format_utc(ry, rm, rd, rh, rmi, rs, rns),
        utc_minus_tai=utc_minus_tai,
    )
