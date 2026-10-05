"""Tests for onboard clock correlation segments.

Two layers:

* ``app.timecore``: the integer-only tick -> TAI ns segment mapping;
* ``app.server``: parsing/validation of the ``correlations`` array,
  same-partition overlap/continuity checks, onboard events mixed with
  UTC/GPS events, and batch-error localization.
"""

from __future__ import annotations

import json
import unittest

from app.server import RequestError, normalize_batch
from app.timecore import (
    NS_PER_SECOND,
    TickSegment,
    TimeConversionError,
    normalize_onboard_event,
)


def enc(obj: object) -> bytes:
    return json.dumps(obj).encode()


# TAI ns at 2017-01-01T00:00:00Z (offset -37) and one second later.
T0 = 1_483_228_837 * NS_PER_SECOND
T1 = T0 + NS_PER_SECOND


def seg(
    partition: int = 7,
    start: int = 0,
    end: int = 1000,
    anchor_tick: int = 0,
    *,
    num: int = 1_000_000,
    den: int = 1,
    utc: str | None = "2017-01-01T00:00:00Z",
    gps: tuple[int, int] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "clockPartition": partition,
        "tickStart": start,
        "tickEnd": end,
        "anchorTick": anchor_tick,
        "nanosecondsPerTickNumerator": num,
        "nanosecondsPerTickDenominator": den,
    }
    if utc is not None:
        body["utc"] = utc
    else:
        week, sow = gps if gps is not None else (1930, 19)
        body["gpsWeek"] = week
        body["gpsSecondsInWeek"] = sow
    return body


class SegmentMathTests(unittest.TestCase):
    def test_integer_scale_maps_exactly(self) -> None:
        s = TickSegment(0, 1000, 0, T0, 1_000_000, 1)
        self.assertEqual(s.map_tick(0), T0)
        self.assertEqual(s.map_tick(999), T0 + 999_000_000)

    def test_anchor_inside_segment_not_at_start(self) -> None:
        s = TickSegment(0, 1000, 500, T0, 2, 1)
        self.assertEqual(s.map_tick(500), T0)
        self.assertEqual(s.map_tick(501), T0 + 2)
        self.assertEqual(s.map_tick(0), T0 - 1000)

    def test_rational_scale_exact_ticks_only(self) -> None:
        # 3 ns per 2 ticks: even offsets from the anchor are exact...
        s = TickSegment(0, 1000, 10, T0, 3, 2)
        self.assertEqual(s.map_tick(10), T0)
        self.assertEqual(s.map_tick(12), T0 + 3)
        self.assertEqual(s.map_tick(8), T0 - 3)
        # ...odd offsets leave a sub-nanosecond remainder.
        with self.assertRaises(TimeConversionError):
            s.map_tick(11)

    def test_normalize_renders_utc_and_offset(self) -> None:
        s = TickSegment(0, 1000, 0, T0, 1_000_000, 1)
        ev = normalize_onboard_event(s, 500)
        self.assertEqual(ev.tai_ns, T0 + 500_000_000)
        self.assertEqual(ev.utc_canonical, "2017-01-01T00:00:00.5Z")
        self.assertEqual(ev.utc_minus_tai, -37)


class BatchHappyPathTests(unittest.TestCase):
    def test_onboard_mixed_with_utc_and_gps_preserves_order(self) -> None:
        out = normalize_batch(enc({
            "correlations": [seg()],
            "events": [
                {"id": "tick-a", "clockPartition": 7, "onboardTick": 0},
                {"id": "utc-a", "utc": "2017-01-01T00:00:00Z"},
                {"id": "tick-b", "clockPartition": 7, "onboardTick": 999},
                {"id": "gps-a", "gpsWeek": 1930,
                 "gpsSecondsInWeek": 18, "gpsNanoseconds": 999_000_000},
            ],
        }))
        self.assertEqual(
            [r["id"] for r in out], ["tick-a", "utc-a", "tick-b", "gps-a"]
        )
        self.assertEqual(out[0]["taiNanoseconds"], str(T0))
        self.assertEqual(out[0]["taiNanoseconds"], out[1]["taiNanoseconds"])
        self.assertEqual(out[2]["taiNanoseconds"], str(T0 + 999_000_000))
        self.assertEqual(out[2]["taiNanoseconds"], out[3]["taiNanoseconds"])
        for r in out:
            self.assertEqual(set(r), {
                "id", "taiNanoseconds", "utc", "utcTaiOffsetSeconds"})

    def test_onboard_anchor_matches_same_instant_via_utc(self) -> None:
        out = normalize_batch(enc({
            "correlations": [seg(anchor_tick=10, utc="2016-12-31T23:59:60Z")],
            "events": [
                {"id": "anchor-tick", "clockPartition": 7, "onboardTick": 10},
                {"id": "anchor-utc", "utc": "2016-12-31T23:59:60Z"},
            ],
        }))
        self.assertEqual(
            out[0]["taiNanoseconds"], out[1]["taiNanoseconds"]
        )
        self.assertEqual(out[0]["utc"], "2016-12-31T23:59:60Z")
        self.assertEqual(out[0]["utcTaiOffsetSeconds"], "-36")

    def test_gps_anchor_accepted(self) -> None:
        # GPS week 1930 SOW 19 is 2017-01-01T00:00:01Z.
        out = normalize_batch(enc({
            "correlations": [seg(utc=None, gps=(1930, 19))],
            "events": [{"id": "t", "clockPartition": 7, "onboardTick": 0}],
        }))
        self.assertEqual(out[0]["taiNanoseconds"], str(T1))

    def test_rational_scale_exact_tick_accepted(self) -> None:
        out = normalize_batch(enc({
            "correlations": [seg(num=3, den=2)],
            "events": [{"id": "t", "clockPartition": 7, "onboardTick": 2}],
        }))
        self.assertEqual(out[0]["taiNanoseconds"], str(T0 + 3))


class AbuttingSegmentTests(unittest.TestCase):
    def _two_segments(self, second: dict[str, object]) -> list[dict[str, object]]:
        return [seg(end=1000), second]

    def test_boundary_tick_maps_same_from_either_side(self) -> None:
        correlations = self._two_segments(
            seg(start=1000, end=2000, anchor_tick=1000,
                num=2_000_000, den=2, utc=None, gps=(1930, 19))
        )
        out = normalize_batch(enc({
            "correlations": correlations,
            "events": [
                {"id": "last-of-a", "clockPartition": 7, "onboardTick": 999},
                {"id": "boundary", "clockPartition": 7, "onboardTick": 1000},
                {"id": "first-of-b", "clockPartition": 7, "onboardTick": 1001},
            ],
        }))
        self.assertEqual(out[1]["taiNanoseconds"], str(T1))
        self.assertEqual(
            int(out[1]["taiNanoseconds"]) - int(out[0]["taiNanoseconds"]),
            1_000_000,
        )
        self.assertEqual(
            int(out[2]["taiNanoseconds"]) - int(out[1]["taiNanoseconds"]),
            1_000_000,
        )

    def test_segments_given_in_reverse_order_are_sorted(self) -> None:
        correlations = [
            seg(start=1000, end=2000, anchor_tick=1000,
                utc=None, gps=(1930, 19)),
            seg(start=0, end=1000),
        ]
        out = normalize_batch(enc({
            "correlations": correlations,
            "events": [{"id": "t", "clockPartition": 7, "onboardTick": 1500}],
        }))
        self.assertEqual(out[0]["taiNanoseconds"], str(T1 + 500_000_000))

    def test_boundary_disagreement_rejected_with_both_indices(self) -> None:
        # Same tick geometry, but the second anchor claims the boundary tick
        # is a whole second later than continuity requires.
        correlations = self._two_segments(
            seg(start=1000, end=2000, anchor_tick=1000,
                utc="2017-01-01T00:00:02Z")
        )
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({"correlations": correlations, "events": [
                {"id": "t", "clockPartition": 7, "onboardTick": 500},
            ]}))
        exc = cm.exception
        self.assertEqual(exc.code, "INVALID_CORRELATION")
        self.assertEqual(exc.correlation_index, 0)
        self.assertEqual(exc.related_correlation_index, 1)
        self.assertIn("1000", exc.message)

    def test_boundary_subnanosecond_on_one_side_rejected(self) -> None:
        # Second scale 3/2 ns per tick makes the boundary tick itself
        # non-integral relative to its anchor at the boundary.
        correlations = self._two_segments(
            seg(start=1000, end=2000, anchor_tick=1001, num=3, den=2,
                utc=None, gps=(1930, 19))
        )
        # anchor tick 1001 -> T1 + ? must make anchor in range and valid;
        # the boundary tick 1000 is one tick before anchor -> 3/2 ns offset
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({"correlations": correlations, "events": [
                {"id": "t", "clockPartition": 7, "onboardTick": 500},
            ]}))
        self.assertEqual(cm.exception.code, "INVALID_CORRELATION")
        self.assertIn("boundary", cm.exception.message)


class IndependentPartitionsTests(unittest.TestCase):
    def test_partitions_may_reuse_tick_counts(self) -> None:
        out = normalize_batch(enc({
            "correlations": [
                seg(partition=1, utc="2017-01-01T00:00:00Z"),
                seg(partition=2, utc="2016-12-31T23:59:60Z"),
            ],
            "events": [
                {"id": "p1", "clockPartition": 1, "onboardTick": 0},
                {"id": "p2", "clockPartition": 2, "onboardTick": 0},
            ],
        }))
        self.assertEqual(out[0]["utcTaiOffsetSeconds"], "-37")
        self.assertEqual(out[1]["utcTaiOffsetSeconds"], "-36")
        self.assertNotEqual(
            out[0]["taiNanoseconds"], out[1]["taiNanoseconds"]
        )

    def test_identical_ranges_in_two_partitions_not_overlap(self) -> None:
        # Same [0, 1000) range in both partitions must not be flagged as
        # overlapping; overlap is only meaningful within one partition.
        normalize_batch(enc({
            "correlations": [seg(partition=1), seg(partition=2)],
            "events": [
                {"id": "a", "clockPartition": 1, "onboardTick": 999},
                {"id": "b", "clockPartition": 2, "onboardTick": 999},
            ],
        }))


class CorrelationErrorTests(unittest.TestCase):
    def _expect(self, correlations: object, events: object | None = None,
                ) -> RequestError:
        payload: dict[str, object] = {
            "events": events or [
                {"id": "ev", "clockPartition": 7, "onboardTick": 0}
            ]
        }
        if correlations is not None:
            payload["correlations"] = correlations
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc(payload))
        return cm.exception

    def test_overlap_reports_both_indices(self) -> None:
        exc = self._expect([
            seg(start=0, end=1000),
            seg(start=500, end=1500, anchor_tick=500,
                utc="2017-01-01T00:00:00.5Z"),
        ])
        self.assertEqual(exc.code, "INVALID_CORRELATION")
        self.assertEqual((exc.correlation_index,
                          exc.related_correlation_index), (0, 1))
        self.assertIn("overlap", exc.message)

    def test_gap_reports_break_location(self) -> None:
        exc = self._expect([
            seg(end=1000),
            seg(start=1001, end=2000, anchor_tick=1001,
                utc="2017-01-01T00:00:01.001Z"),
        ])
        self.assertEqual(exc.correlation_index, 1)
        self.assertIn("1000", exc.message)
        self.assertIn("1001", exc.message)

    def test_anchor_outside_segment(self) -> None:
        exc = self._expect([seg(anchor_tick=1000)])
        self.assertEqual(exc.correlation_index, 0)
        self.assertIn("anchorTick", exc.message)

    def test_empty_or_backwards_range(self) -> None:
        exc = self._expect([seg(start=1000, end=1000)])
        self.assertEqual(exc.correlation_index, 0)
        self.assertIn("tickStart < tickEnd", exc.message)

    def test_nonpositive_scale_parts(self) -> None:
        exc = self._expect([seg(num=0, den=1)])
        self.assertIn("positive", exc.message)
        exc = self._expect([seg(num=1, den=-2)])
        self.assertIn("positive", exc.message)

    def test_illegal_anchor_time(self) -> None:
        exc = self._expect([seg(utc="2016-12-30T23:59:60Z")])
        self.assertEqual(exc.correlation_index, 0)
        self.assertIn("anchor", exc.message)

    def test_anchor_beyond_supported_era(self) -> None:
        exc = self._expect([seg(utc="2020-01-01T00:00:00Z")])
        self.assertEqual(exc.code, "INVALID_CORRELATION")

    def test_anchor_both_utc_and_gps(self) -> None:
        bad = seg()
        bad["gpsWeek"] = 1930
        bad["gpsSecondsInWeek"] = 19
        exc = self._expect([bad])
        self.assertEqual(exc.correlation_index, 0)

    def test_anchor_neither_utc_nor_gps(self) -> None:
        bad = seg()
        del bad["utc"]
        exc = self._expect([bad])
        self.assertIn("anchor", exc.message)

    def test_bad_gps_anchor(self) -> None:
        exc = self._expect([seg(utc=None, gps=(0, 604_800))])
        self.assertEqual(exc.correlation_index, 0)

    def test_missing_required_field(self) -> None:
        bad = seg()
        del bad["anchorTick"]
        exc = self._expect([bad])
        self.assertIn("anchorTick", exc.message)

    def test_unknown_correlation_field(self) -> None:
        bad = seg()
        bad["extra"] = 1
        exc = self._expect([bad])
        self.assertIn("unknown", exc.message)

    def test_correlation_count_limits(self) -> None:
        exc = self._expect([])
        self.assertIn("between 1 and 32", exc.message)
        exc = self._expect([seg(partition=i) for i in range(33)])
        self.assertIn("between 1 and 32", exc.message)

    def test_correlation_not_an_object(self) -> None:
        exc = self._expect([42])
        self.assertEqual(exc.correlation_index, 0)

    def test_string_integers_rejected(self) -> None:
        bad = seg()
        bad["tickStart"] = "0"
        exc = self._expect([bad])
        self.assertEqual(exc.correlation_index, 0)

    def test_float_scale_rejected(self) -> None:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "correlations": [{
                    "clockPartition": 7, "tickStart": 0, "tickEnd": 1000,
                    "anchorTick": 0,
                    "nanosecondsPerTickNumerator": 1.5,
                    "nanosecondsPerTickDenominator": 1,
                    "utc": "2017-01-01T00:00:00Z",
                }],
                "events": [{"id": "ev", "clockPartition": 7,
                            "onboardTick": 0}],
            }))
        self.assertIn("floating-point", cm.exception.message)


class OnboardEventErrorTests(unittest.TestCase):
    def _expect(self, events: list[object]) -> RequestError:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "correlations": [seg()],
                "events": events,
            }))
        return cm.exception

    def test_tick_above_range_not_covered(self) -> None:
        exc = self._expect([
            {"id": "late", "clockPartition": 7, "onboardTick": 1000}
        ])
        self.assertEqual(exc.code, "INVALID_EVENT")
        self.assertEqual(exc.event_index, 0)
        self.assertEqual(exc.event_id, "late")
        self.assertIsNone(exc.correlation_index)
        self.assertIn("not covered", exc.message)

    def test_tick_below_range_not_covered(self) -> None:
        exc = self._expect([
            {"id": "early", "clockPartition": 7, "onboardTick": -1}
        ])
        self.assertEqual(exc.event_id, "early")
        self.assertIn("not covered", exc.message)

    def test_unknown_partition_not_covered(self) -> None:
        exc = self._expect([
            {"id": "other", "clockPartition": 99, "onboardTick": 0}
        ])
        self.assertEqual(exc.event_id, "other")
        self.assertIn("99", exc.message)

    def test_subnanosecond_result_locates_event_and_segment(self) -> None:
        # Scale 1 ns per 2 ticks; odd tick offset from anchor tick 0 is
        # sub-nanosecond.
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "correlations": [seg(num=1, den=2)],
                "events": [
                    {"id": "ok", "clockPartition": 7, "onboardTick": 0},
                    {"id": "half", "clockPartition": 7, "onboardTick": 1},
                ],
            }))
        exc = cm.exception
        self.assertEqual(exc.code, "INVALID_EVENT")
        self.assertEqual(exc.event_index, 1)
        self.assertEqual(exc.event_id, "half")
        self.assertEqual(exc.correlation_index, 0)
        self.assertIn("nanosecond", exc.message)

    def test_onboard_instant_outside_supported_era(self) -> None:
        # Scale large enough that a covered tick lands past table expiry.
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "correlations": [seg(end=10**12, num=10**9, den=1)],
                "events": [
                    {"id": "future", "clockPartition": 7,
                     "onboardTick": 10**11},
                ],
            }))
        exc = cm.exception
        self.assertEqual(exc.event_id, "future")
        self.assertEqual(exc.correlation_index, 0)

    def test_missing_onboard_field(self) -> None:
        exc = self._expect([{"id": "x", "clockPartition": 7}])
        self.assertEqual(exc.event_id, "x")
        self.assertIn("onboardTick", exc.message)

    def test_mixed_kind_event_rejected(self) -> None:
        exc = self._expect([{
            "id": "x", "clockPartition": 7, "onboardTick": 0,
            "utc": "2017-01-01T00:00:00Z",
        }])
        self.assertIn("exactly one kind", exc.message)

    def test_segment_error_takes_precedence_over_events(self) -> None:
        # Bad correlation must be reported even though the first event also
        # could not be mapped; no event locator is attached.
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "correlations": [seg(utc="not-a-time")],
                "events": [{"id": "ev", "clockPartition": 7,
                            "onboardTick": 0}],
            }))
        exc = cm.exception
        self.assertEqual(exc.code, "INVALID_CORRELATION")
        self.assertIsNone(exc.event_index)
        self.assertIsNone(exc.event_id)

    def test_error_body_carries_no_results(self) -> None:
        try:
            normalize_batch(enc({
                "correlations": [seg(num=1, den=2)],
                "events": [
                    {"id": "a", "clockPartition": 7, "onboardTick": 0},
                    {"id": "b", "clockPartition": 7, "onboardTick": 1},
                ],
            }))
        except RequestError as exc:
            self.assertFalse(hasattr(exc, "results"))
            self.assertNotIn("taiNanoseconds", repr(vars(exc)))
        else:
            self.fail("expected rejection")


class LegacyUnchangedTests(unittest.TestCase):
    def test_request_without_correlations_still_works(self) -> None:
        out = normalize_batch(enc({
            "events": [{"id": "x", "utc": "2016-12-31T23:59:60.5Z"}],
        }))
        self.assertEqual(out[0]["taiNanoseconds"], "1483228836500000000")

    def test_onboard_event_without_correlations_rejected(self) -> None:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "events": [{"id": "x", "clockPartition": 7,
                            "onboardTick": 0}],
            }))
        self.assertEqual(cm.exception.event_id, "x")
        self.assertIn("not covered", cm.exception.message)

    def test_unknown_top_level_field_rejected(self) -> None:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "events": [{"id": "x", "utc": "2017-01-01T00:00:00Z"}],
                "correlations": [],
                "bogus": 1,
            }))
        self.assertIn("bogus", cm.exception.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
