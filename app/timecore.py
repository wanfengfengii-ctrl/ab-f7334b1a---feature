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

from bisect import bisect_right
from collections.abc import Mapping
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
# Onboard-clock correlation segments
# ---------------------------------------------------------------------------
#
# A segment maps a half-open interval of integer onboard counts of one
# clock partition onto the TAI nanosecond axis.  Within a segment the
# mapping is linear with an integer slope (nanosecondsNumerator /
# nanosecondsDenominator ns per count, both strictly positive integers):
#
#     tai(count) = anchorTaiNs
#                 + (count - anchorCount) * numerator // denominator
#
# The specification requires the result to be an *exact* integer number of
# nanoseconds, so the remainder of that floor division must be zero for
# every mapped count; equivalently the residue class of the anchor count
# must be the only representable one and each event must belong to it.
# Every operation below is integer arithmetic -- no floats anywhere.

class CorrelationError(ValueError):
    """Invalid correlation set; message is caller-safe.

    ``segment_index`` is the zero-based position in the request-level
    ``correlations`` array (``None`` for errors that span the whole set).
    """

    def __init__(self, message: str, segment_index: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.segment_index = segment_index


@dataclass(frozen=True)
class Segment:
    clock_partition: str
    range_start: int          # inclusive
    range_end: int            # exclusive
    anchor_count: int
    anchor_tai_ns: int
    nanos_num: int
    nanos_den: int

    def contains(self, count: int) -> bool:
        return self.range_start <= count < self.range_end

    def map_tick(self, count: int) -> int:
        """TAI ns of *count*; raises when the result is not an integer ns."""
        delta_num = (count - self.anchor_count) * self.nanos_num
        q, rem = divmod(delta_num, self.nanos_den)
        if rem:
            raise CorrelationError(
                "onboard count maps to a sub-nanosecond TAI instant:"
                f" partition {self.clock_partition!r}, count {count} leaves"
                f" residue {rem}/{self.nanos_den} of a nanosecond under"
                f" {self.nanos_num}/{self.nanos_den} ns per count"
            )
        return self.anchor_tai_ns + q


def _seg_int(value: object, field: str, index: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CorrelationError(
            f"correlation segment {index} field {field!r} must be an integer",
            index,
        )
    return value


def _parse_segment(raw: object, index: int) -> Segment:
    if not isinstance(raw, Mapping):
        raise CorrelationError(
            f"correlation segment {index} must be a JSON object", index
        )
    required = (
        "clockPartition", "tickRangeStart", "tickRangeEnd", "anchorTick",
        "nanosecondsNumerator", "nanosecondsDenominator",
    )
    missing = [f for f in required if f not in raw]
    if missing:
        raise CorrelationError(
            f"correlation segment {index} is missing required field(s):"
            f" {', '.join(missing)}",
            index,
        )
    allowed = {
        *required, "anchorUtc", "anchorGpsWeek", "anchorGpsSecondsInWeek",
        "anchorGpsNanoseconds",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise CorrelationError(
            f"correlation segment {index} has unknown field(s):"
            f" {', '.join(unknown)}",
            index,
        )

    partition = raw["clockPartition"]
    if not isinstance(partition, str) or not partition.strip():
        raise CorrelationError(
            f"correlation segment {index} field 'clockPartition' must be a"
            " non-empty string",
            index,
        )
    start = _seg_int(raw["tickRangeStart"], "tickRangeStart", index)
    end = _seg_int(raw["tickRangeEnd"], "tickRangeEnd", index)
    anchor = _seg_int(raw["anchorTick"], "anchorTick", index)
    num = _seg_int(raw["nanosecondsNumerator"], "nanosecondsNumerator", index)
    den = _seg_int(raw["nanosecondsDenominator"], "nanosecondsDenominator",
                   index)
    if end <= start:
        raise CorrelationError(
            f"correlation segment {index} (partition {partition!r}) has an"
            f" empty or inverted half-open tick range: [{start}, {end});"
            " tickRangeEnd must be strictly greater than tickRangeStart",
            index,
        )
    if not start <= anchor < end:
        raise CorrelationError(
            f"correlation segment {index} (partition {partition!r}) has an"
            " out-of-range anchor tick: anchorTick"
            f" {anchor} is not in [{start}, {end})",
            index,
        )
    if num <= 0:
        raise CorrelationError(
            f"correlation segment {index} (partition {partition!r}) requires a"
            f" strictly positive nanosecondsNumerator (got {num})",
            index,
        )
    if den <= 0:
        raise CorrelationError(
            f"correlation segment {index} (partition {partition!r}) requires a"
            f" strictly positive nanosecondsDenominator (got {den})",
            index,
        )

    has_utc = "anchorUtc" in raw
    gps_fields = (
        "anchorGpsWeek", "anchorGpsSecondsInWeek", "anchorGpsNanoseconds",
    )
    gps_present = [f for f in gps_fields if f in raw]
    if has_utc and gps_present:
        raise CorrelationError(
            f"correlation segment {index} (partition {partition!r}) gives both"
            " 'anchorUtc' and GPS anchor fields; choose exactly one",
            index,
        )
    if not has_utc and not gps_present:
        raise CorrelationError(
            f"correlation segment {index} (partition {partition!r}) must give"
            " an anchor either as 'anchorUtc' or as GPS fields"
            " ('anchorGpsWeek' and 'anchorGpsSecondsInWeek', optionally"
            " 'anchorGpsNanoseconds')",
            index,
        )
    try:
        if has_utc:
            anchor_event = normalize_utc_event(raw["anchorUtc"])
        else:
            if "anchorGpsWeek" not in raw:
                raise CorrelationError(
                    f"correlation segment {index} (partition {partition!r})"
                    " GPS anchor is missing 'anchorGpsWeek'", index
                )
            if "anchorGpsSecondsInWeek" not in raw:
                raise CorrelationError(
                    f"correlation segment {index} (partition {partition!r})"
                    " GPS anchor is missing 'anchorGpsSecondsInWeek'", index
                )
            week = _seg_int(raw["anchorGpsWeek"], "anchorGpsWeek", index)
            sow = _seg_int(raw["anchorGpsSecondsInWeek"],
                           "anchorGpsSecondsInWeek", index)
            nanos = 0
            if "anchorGpsNanoseconds" in raw:
                nanos = _seg_int(raw["anchorGpsNanoseconds"],
                                 "anchorGpsNanoseconds", index)
            anchor_event = normalize_gps_event(week, sow, nanos)
    except TimeConversionError as exc:
        raise CorrelationError(
            f"correlation segment {index} (partition {partition!r}) has an"
            f" invalid anchor: {exc}",
            index,
        ) from None

    return Segment(
        clock_partition=partition,
        range_start=start,
        range_end=end,
        anchor_count=anchor,
        anchor_tai_ns=anchor_event.tai_ns,
        nanos_num=num,
        nanos_den=den,
    )


@dataclass(frozen=True)
class CorrelationSet:
    """Validated segments plus O(log n) per-partition lookup."""

    segments: tuple[Segment, ...]
    _by_partition: Mapping[str, tuple[tuple[int, ...], tuple[Segment, ...]]]

    def resolve(self, partition: str, count: int) -> Segment:
        """Return the unique segment of *partition* covering *count*."""
        table = self._by_partition.get(partition)
        if table is None:
            raise CorrelationError(
                f"no correlation segment covers partition {partition!r}"
            )
        starts, segs = table
        pos = bisect_right(starts, count) - 1
        if pos < 0 or not segs[pos].contains(count):
            raise CorrelationError(
                f"onboard tick {count} of partition {partition!r} is not"
                " covered by any correlation segment half-open tick range"
            )
        return segs[pos]


def _build_correlation_set(segments: tuple[Segment, ...]) -> CorrelationSet:
    grouped: dict[str, list[Segment]] = {}
    order: dict[str, list[int]] = {}
    for i, seg in enumerate(segments):
        grouped.setdefault(seg.clock_partition, []).append(seg)
        order.setdefault(seg.clock_partition, []).append(i)

    tables: dict[str, tuple[tuple[int, ...], tuple[Segment, ...]]] = {}
    for partition, segs in grouped.items():
        # Order by range start; report problems at the later segment in
        # request order so the index is unambiguous.
        indexed = sorted(
            zip(order[partition], segs), key=lambda pair: pair[1].range_start
        )
        for k in range(1, len(indexed)):
            prev_idx, prev = indexed[k - 1]
            idx, seg = indexed[k]
            at = max(idx, prev_idx)
            if seg.range_start < prev.range_end:
                raise CorrelationError(
                    f"correlation segments {prev_idx} and {idx} of partition"
                    f" {partition!r} overlap: [{prev.range_start},"
                    f" {prev.range_end}) and [{seg.range_start},"
                    f" {seg.range_end}) share onboard ticks",
                    at,
                )
            if seg.range_start > prev.range_end:
                raise CorrelationError(
                    f"correlation segments {prev_idx} and {idx} of partition"
                    f" {partition!r} are not contiguous: a coverage gap exists"
                    f" between {prev.range_end} and {seg.range_start}"
                    " (touching segments must share their common boundary)",
                    at,
                )
            # Same-partition segments touch at the common boundary tick.
            # Both linear maps must place that tick at the same TAI instant.
            try:
                prev_edge = prev.map_tick(seg.range_start)
                edge = seg.map_tick(seg.range_start)
            except CorrelationError as exc:
                raise CorrelationError(exc.message, at) from None
            if prev_edge != edge:
                raise CorrelationError(
                    f"correlation segments {prev_idx} and {idx} of partition"
                    f" {partition!r} disagree at their common boundary tick"
                    f" {seg.range_start}: the earlier segment maps it to TAI"
                    f" ns {prev_edge} but the later segment to TAI ns"
                    f" {edge}",
                    at,
                )
        tables[partition] = (
            tuple(s.range_start for _, s in indexed),
            tuple(s for _, s in indexed),
        )
    return CorrelationSet(segments=segments, _by_partition=tables)


def build_correlations(raw: object) -> CorrelationSet | None:
    """Parse and validate the optional request-level ``correlations``.

    Returns ``None`` when the field is absent so that legacy requests keep
    their exact previous semantics.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise CorrelationError("'correlations' must be an array")
    if not (1 <= len(raw) <= 32):
        raise CorrelationError(
            f"'correlations' must contain between 1 and 32 segments; got"
            f" {len(raw)}"
        )
    segments = tuple(_parse_segment(item, i) for i, item in enumerate(raw))
    return _build_correlation_set(segments)


def normalize_onboard_event(
    correlations: CorrelationSet, partition: object, tick: object,
) -> NormalizedEvent:
    if not isinstance(partition, str) or not partition.strip():
        raise TimeConversionError(
            "clockPartition must be a non-empty string naming a correlation"
            " partition"
        )
    if isinstance(tick, bool) or not isinstance(tick, int):
        raise TimeConversionError("onboardTick must be an integer")
    try:
        segment = correlations.resolve(partition, tick)
        tai_ns = segment.map_tick(tick)
    except CorrelationError as exc:
        # Coverage/sub-nanosecond failures are attributed to the event; the
        # message itself names the partition and offending tick.
        raise TimeConversionError(exc.message) from None
    # The mapped instant must lie on the same supported TAI era as the
    # GPS/UTC events, so the three streams mix without out-of-table labels.
    _check_supported_era(tai_ns)
    ry, rm, rd, rh, rmi, rs, rns, utc_minus_tai = tai_to_utc(tai_ns)
    return NormalizedEvent(
        tai_ns=tai_ns,
        utc_canonical=_format_utc(ry, rm, rd, rh, rmi, rs, rns),
        utc_minus_tai=utc_minus_tai,
    )
