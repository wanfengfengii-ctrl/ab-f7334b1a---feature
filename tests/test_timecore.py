"""Tests for the leap-second conversion core.

Run with:  python -m unittest discover -s tests -v

Independent reference values (Unix timestamps / offsets) come from the
IANA tzdb leap-seconds.list table and IERS Bulletin C 52; nothing here
reads the production table to produce its own expected values.
"""

from __future__ import annotations

import random
import unittest

from app.timecore import (
    LEAP_BOUNDS,
    MIN_SUPPORTED_TAI_NS,
    NS_PER_SECOND,
    SECONDS_PER_WEEK,
    TABLE_EXPIRY_TAI_NS,
    NormalizedEvent,
    TimeConversionError,
    normalize_gps_event,
    normalize_utc_event,
    tai_to_utc,
    utc_to_tai,
)
from app.timecore import (
    _check_civil_validity,
    _civil_from_days,
    _days_from_civil,
    _format_utc,
    GPS_EPOCH_TAI_NS,
)


# Unix (UTC label) timestamps of independent reference instants.
UNIX_2016_PRE = 1_483_228_799   # 2016-12-31T23:59:59Z
UNIX_2017_JAN1 = 1_483_228_800  # 2017-01-01T00:00:00Z
UNIX_EXPIRY = 1_498_608_000     # 2017-06-28T00:00:00Z


class KnownVectorTests(unittest.TestCase):
    def test_2016_leap_triple(self) -> None:
        pre = normalize_utc_event("2016-12-31T23:59:59Z")
        leap = normalize_utc_event("2016-12-31T23:59:60Z")
        post = normalize_utc_event("2017-01-01T00:00:00Z")
        self.assertEqual(pre.tai_ns, (UNIX_2016_PRE + 36) * NS_PER_SECOND)
        self.assertEqual(leap.tai_ns, pre.tai_ns + NS_PER_SECOND)
        self.assertEqual(post.tai_ns, (UNIX_2017_JAN1 + 37) * NS_PER_SECOND)
        self.assertEqual(post.tai_ns - leap.tai_ns, NS_PER_SECOND)
        # Old offset (-36) applies *during* the leap second.
        self.assertEqual(
            (pre.utc_minus_tai, leap.utc_minus_tai, post.utc_minus_tai),
            (-36, -36, -37),
        )

    def test_gps_epoch(self) -> None:
        g = normalize_gps_event(0, 0, 0)
        self.assertEqual(g.utc_canonical, "1980-01-06T00:00:00Z")
        self.assertEqual(g.utc_minus_tai, -19)
        self.assertEqual(g.tai_ns, 315_964_819 * NS_PER_SECOND)

    def test_2016_leap_via_gps(self) -> None:
        # GPS runs 18 s ahead of UTC after the 2016 leap: UTC midnight
        # 2017-01-01 is GPS week 1930, SOW 18; the leap second is SOW 17.
        post = normalize_utc_event("2017-01-01T00:00:00Z")
        leap = normalize_utc_event("2016-12-31T23:59:60Z")
        pre = normalize_utc_event("2016-12-31T23:59:59Z")
        self.assertEqual(normalize_gps_event(1930, 18, 0).tai_ns, post.tai_ns)
        self.assertEqual(
            normalize_gps_event(1930, 17, 0),
            NormalizedEvent(leap.tai_ns, "2016-12-31T23:59:60Z", -36),
        )
        self.assertEqual(normalize_gps_event(1930, 16, 0).tai_ns, pre.tai_ns)

    def test_first_table_rows_1972(self) -> None:
        self.assertEqual(
            normalize_utc_event("1972-01-01T00:00:00Z").utc_minus_tai, -10
        )
        self.assertEqual(
            normalize_utc_event("1972-06-30T23:59:60Z").utc_minus_tai, -10
        )
        self.assertEqual(
            normalize_utc_event("1972-07-01T00:00:00Z").utc_minus_tai, -11
        )

    def test_table_shape(self) -> None:
        # 28 rows: opening row 1972 (offset 10) plus 27 positive leaps.
        self.assertEqual(len(LEAP_BOUNDS), 28)
        self.assertEqual(LEAP_BOUNDS[0].offset, 10)
        self.assertEqual(LEAP_BOUNDS[-1].offset, 37)
        self.assertEqual(LEAP_BOUNDS[-1].date, (2017, 1, 1))
        for a, b in zip(LEAP_BOUNDS, LEAP_BOUNDS[1:]):
            self.assertEqual(b.offset - a.offset, 1)
            self.assertGreater(b.tai_ns, a.tai_ns)

    def test_expiry_reference(self) -> None:
        self.assertEqual(
            TABLE_EXPIRY_TAI_NS, (UNIX_EXPIRY + 37) * NS_PER_SECOND
        )
        last_ok = normalize_utc_event("2017-06-27T23:59:59.999999999Z")
        self.assertEqual(last_ok.tai_ns, TABLE_EXPIRY_TAI_NS - 1)


class CrossInputEquivalenceTests(unittest.TestCase):
    def _gps_for_tai(self, tai: int) -> tuple[int, int, int]:
        raw = tai - GPS_EPOCH_TAI_NS
        w, rem = divmod(raw, SECONDS_PER_WEEK * NS_PER_SECOND)
        sow, ns = divmod(rem, NS_PER_SECOND)
        return w, sow, ns

    def test_same_instant_identical_results_2016(self) -> None:
        via_utc = normalize_utc_event("2016-12-31T23:59:60.5Z")
        via_gps = normalize_gps_event(1930, 17, 500_000_000)
        self.assertEqual(via_utc, via_gps)
        self.assertEqual(via_utc.utc_canonical, "2016-12-31T23:59:60.5Z")
        # Serialized TAI values must be byte-identical decimal strings.
        self.assertEqual(
            via_utc.to_response()["taiNanoseconds"],
            via_gps.to_response()["taiNanoseconds"],
        )
        self.assertEqual(
            via_utc.to_response()["utcTaiOffsetSeconds"],
            via_gps.to_response()["utcTaiOffsetSeconds"],
        )

    def test_random_instant_equivalence(self) -> None:
        rng = random.Random(20261005)
        checked = 0
        while checked < 3000:
            tai = rng.randrange(
                GPS_EPOCH_TAI_NS, TABLE_EXPIRY_TAI_NS
            )  # GPS cannot express pre-1980
            w, sow, ns = self._gps_for_tai(tai)
            utc_text = _format_utc(*tai_to_utc(tai)[:7])
            via_gps = normalize_gps_event(w, sow, ns)
            via_utc = normalize_utc_event(utc_text)
            self.assertEqual(via_gps.tai_ns, tai)
            self.assertEqual(via_utc.tai_ns, tai)
            self.assertEqual(via_gps.utc_canonical, via_utc.utc_canonical)
            self.assertEqual(
                via_gps.utc_minus_tai, via_utc.utc_minus_tai
            )
            self.assertEqual(via_gps.to_response(), via_utc.to_response())
            checked += 1


class RoundTripTests(unittest.TestCase):
    def test_all_leap_days_canonical(self) -> None:
        for b in LEAP_BOUNDS[1:]:
            py, pm, pd = _civil_from_days(
                _days_from_civil(*b.date) - 1
            )
            pre = normalize_utc_event(
                f"{py:04d}-{pm:02d}-{pd:02d}T23:59:59Z"
            )
            leap = normalize_utc_event(
                f"{py:04d}-{pm:02d}-{pd:02d}T23:59:60.123456789Z"
            )
            post = normalize_utc_event(
                f"{b.date[0]:04d}-{b.date[1]:02d}-{b.date[2]:02d}"
                "T00:00:00Z"
            )
            self.assertEqual(
                leap.utc_canonical,
                f"{py:04d}-{pm:02d}-{pd:02d}T23:59:60.123456789Z",
            )
            self.assertEqual(
                leap.tai_ns - pre.tai_ns, 1_123_456_789
            )
            self.assertEqual(post.tai_ns - leap.tai_ns, 876_543_211)
            self.assertEqual(leap.utc_minus_tai, -(b.offset - 1))
            self.assertEqual(post.utc_minus_tai, -b.offset)

    def test_dense_nanoseconds_around_every_edge(self) -> None:
        for b in LEAP_BOUNDS[1:]:
            for edge in (b.tai_ns - NS_PER_SECOND, b.tai_ns):
                lo, hi = edge - 500, edge + 500
                for tai in range(lo, hi):
                    comps = tai_to_utc(tai)
                    _check_civil_validity(*comps[:6])
                    self.assertEqual(utc_to_tai(*comps[:7]), tai)

    def test_whole_seconds_around_every_boundary(self) -> None:
        for b in LEAP_BOUNDS[1:]:
            for k in range(-5, 6):
                tai = b.tai_ns + k * NS_PER_SECOND
                if tai < MIN_SUPPORTED_TAI_NS:
                    continue
                comps = tai_to_utc(tai)
                self.assertEqual(utc_to_tai(*comps[:7]), tai)

    def test_coarse_sweep_of_whole_era(self) -> None:
        t = MIN_SUPPORTED_TAI_NS
        step = 7919 * NS_PER_SECOND + 123_456_789
        while t < TABLE_EXPIRY_TAI_NS:
            comps = tai_to_utc(t)
            self.assertEqual(utc_to_tai(*comps[:7]), t)
            t += step

    def test_canonical_fraction_formatting(self) -> None:
        cases = {
            "2016-12-31T23:59:59.0Z": "2016-12-31T23:59:59Z",
            "2016-12-31T23:59:59.500Z": "2016-12-31T23:59:59.5Z",
            "2016-12-31T23:59:59.000000001Z":
                "2016-12-31T23:59:59.000000001Z",
            "2016-12-31T23:59:60.999999999Z":
                "2016-12-31T23:59:60.999999999Z",
        }
        for raw, canon in cases.items():
            self.assertEqual(
                normalize_utc_event(raw).utc_canonical, canon
            )


class RejectionTests(unittest.TestCase):
    def assert_rejected_utc(self, text: object) -> None:
        with self.assertRaises(TimeConversionError):
            normalize_utc_event(text)

    def test_invalid_calendar_dates(self) -> None:
        for text in (
            "2017-02-29T00:00:00Z",   # 2017 not a leap year
            "2016-02-30T12:00:00Z",
            "2016-04-31T00:00:00Z",
            "2016-13-01T00:00:00Z",
            "2016-00-01T00:00:00Z",
            "2016-12-00T00:00:00Z",
            "0000-01-01T00:00:00Z",
        ):
            with self.subTest(text=text):
                self.assert_rejected_utc(text)

    def test_invalid_clock_fields(self) -> None:
        for text in (
            "2016-12-31T24:00:00Z",
            "2016-12-31T23:60:00Z",
            "2016-12-31T23:59:61Z",
            "2016-12-31T23:59:62Z",
        ):
            with self.subTest(text=text):
                self.assert_rejected_utc(text)

    def test_fake_leap_second_positions(self) -> None:
        for text in (
            "2016-12-30T23:59:60Z",   # day before the leap day
            "2016-12-31T23:58:60Z",   # wrong minute
            "2016-12-31T22:59:60Z",   # wrong hour
            "2017-01-01T00:00:60Z",   # after boundary
            "2000-01-01T23:59:60Z",   # 2000 had no leap second
            "2008-06-30T23:59:60Z",   # 2008 leap was Dec 31
            "1971-12-31T23:59:60Z",   # before table era
        ):
            with self.subTest(text=text):
                self.assert_rejected_utc(text)

    def test_real_leap_second_positions_accepted(self) -> None:
        for text in (
            "2008-12-31T23:59:60Z",
            "2015-06-30T23:59:60Z",
            "2016-12-31T23:59:60Z",
            "1998-12-31T23:59:60Z",
            "1972-06-30T23:59:60Z",
        ):
            normalize_utc_event(text)  # must not raise

    def test_unsupported_era(self) -> None:
        for text in (
            "1971-12-31T23:59:59Z",
            "1900-01-01T00:00:00Z",
            "2017-06-28T00:00:00Z",
            "2020-01-01T00:00:00Z",
            "9999-12-31T23:59:59Z",
        ):
            with self.subTest(text=text):
                self.assert_rejected_utc(text)

    def test_malformed_strings(self) -> None:
        for text in (
            "2016-12-31T23:59:60",      # no Z
            "2016-12-31 23:59:59Z",     # space separator
            "23:59:60Z",
            "2016-12-31T23:59Z",
            "2016-12-31T23:59:59+00:00",
            "2016-12-31T23:59:59.1e3Z",
            "2016-12-31T23:59:59.1234567891Z",  # > 9 frac digits
            "",
            "garbage",
            None,
            42,
        ):
            with self.subTest(text=text):
                self.assert_rejected_utc(text)

    def test_gps_out_of_range(self) -> None:
        for week, sow, ns in (
            (0, 604_800, 0),
            (0, -1, 0),
            (0, 0, -1),
            (0, 0, 1_000_000_000),
            (-1, 0, 0),
            (3000, 0, 0),   # far beyond table expiry
        ):
            with self.subTest(week=week, sow=sow, ns=ns):
                with self.assertRaises(TimeConversionError):
                    normalize_gps_event(week, sow, ns)

    def test_gps_types(self) -> None:
        for week, sow in (("0", 0), (0, 1.0), (1.5, 0), (True, 0), (0, None)):
            with self.subTest(week=week, sow=sow):
                with self.assertRaises(TimeConversionError):
                    normalize_gps_event(week, sow)  # type: ignore[arg-type]

    def test_gps_at_expiry_rejected(self) -> None:
        # Smallest GPS week/SOW at/after 2017-06-28T00:00:00Z.
        raw = TABLE_EXPIRY_TAI_NS - GPS_EPOCH_TAI_NS
        w, rem = divmod(raw, SECONDS_PER_WEEK * NS_PER_SECOND)
        sow, ns = divmod(rem, NS_PER_SECOND)
        with self.assertRaises(TimeConversionError):
            normalize_gps_event(w, sow, ns)
        # one ns earlier is fine
        w0, rem0 = divmod(raw - 1, SECONDS_PER_WEEK * NS_PER_SECOND)
        sow0, ns0 = divmod(rem0, NS_PER_SECOND)
        normalize_gps_event(w0, sow0, ns0)


class NoFloatingPointTests(unittest.TestCase):
    def test_responses_use_decimal_strings(self) -> None:
        r = normalize_utc_event("2016-12-31T23:59:60Z").to_response()
        for key in ("taiNanoseconds", "utcTaiOffsetSeconds"):
            self.assertIsInstance(r[key], str)
            int(r[key])  # purely decimal

    def test_source_contains_no_float_arithmetic(self) -> None:
        import ast
        import inspect
        import app.timecore as tc

        tree = ast.parse(inspect.getsource(tc))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                self.fail(f"float literal at line {node.lineno}")
            if isinstance(node, ast.BinOp) and isinstance(
                node.op, ast.Div
            ):
                self.fail(f"true-division operator at line {node.lineno}")
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None
                )
                if name == "float":
                    self.fail(f"float() call at line {node.lineno}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
