"""Tests for onboard-clock correlation segments.

Covers both the integer-only conversion core (``app.timecore``) and the
batch HTTP layer (``app.server``).  Every arithmetic expectation below is
stated in integer TAI nanoseconds; no float is involved anywhere.
"""

from __future__ import annotations

import json
import unittest

from app.timecore import (
    CorrelationError,
    TimeConversionError,
    build_correlations,
    normalize_onboard_event,
)


# 2017-01-01T00:00:00Z (offset 37) and a few nearby instants, in TAI ns.
T0 = 1_483_228_837_000_000_000
NS_PER_MS = 1_000_000


def seg(
    partition: str = "A",
    start: int = 0,
    end: int = 100,
    anchor_tick: int | None = None,
    *,
    anchor_utc: str = "2017-01-01T00:00:00Z",
    num: int = NS_PER_MS,
    den: int = 1,
) -> dict[str, object]:
    return {
        "clockPartition": partition,
        "tickRangeStart": start,
        "tickRangeEnd": end,
        "anchorTick": anchor_tick if anchor_tick is not None else start,
        "anchorUtc": anchor_utc,
        "nanosecondsNumerator": num,
        "nanosecondsDenominator": den,
    }


def gps_seg(
    partition: str = "A", start: int = 0, end: int = 100,
    anchor_tick: int | None = None, *, week: int = 1930, sow: int = 18,
    nanos: int = 0, num: int = 1, den: int = 1,
) -> dict[str, object]:
    d: dict[str, object] = {
        "clockPartition": partition,
        "tickRangeStart": start,
        "tickRangeEnd": end,
        "anchorTick": anchor_tick if anchor_tick is not None else start,
        "anchorGpsWeek": week,
        "anchorGpsSecondsInWeek": sow,
        "nanosecondsNumerator": num,
        "nanosecondsDenominator": den,
    }
    if nanos:
        d["anchorGpsNanoseconds"] = nanos
    return d


class CorrelationParsingTests(unittest.TestCase):
    def test_absent_returns_none(self) -> None:
        self.assertIsNone(build_correlations(None))

    def test_single_segment_utc_anchor(self) -> None:
        cs = build_correlations([seg()])
        (s,) = cs.segments
        self.assertEqual(s.clock_partition, "A")
        self.assertEqual(s.anchor_tai_ns, T0)
        self.assertEqual(s.map_tick(7), T0 + 7 * NS_PER_MS)

    def test_single_segment_gps_anchor(self) -> None:
        # GPS 1930:18 == 2017-01-01T00:00:00Z.
        cs = build_correlations([gps_seg(num=NS_PER_MS)])
        (s,) = cs.segments
        self.assertEqual(s.anchor_tai_ns, T0)
        self.assertEqual(s.map_tick(3), T0 + 3 * NS_PER_MS)

    def test_gps_anchor_with_nanos(self) -> None:
        cs = build_correlations([gps_seg(nanos=500_000_000, sow=17)])
        (s,) = cs.segments
        # 1930:17.5 is the leap second 23:59:60.5.
        self.assertEqual(s.anchor_tai_ns, T0 - 500_000_000)

    def test_limit_1_to_32_segments(self) -> None:
        build_correlations([
            seg(f"P{i}", i, i + 1, anchor_utc="1980-01-06T00:00:00Z")
            for i in range(32)
        ])  # one segment per partition: no per-partition joins
        for n in (0, 33):
            with self.subTest(n=n):
                with self.assertRaises(CorrelationError):
                    build_correlations([seg() for _ in range(n)])

    def test_non_array(self) -> None:
        with self.assertRaises(CorrelationError):
            build_correlations({"not": "an array"})


class SegmentValidationTests(unittest.TestCase):
    def _rejected(self, raw: object, index: int | None = None) -> None:
        with self.assertRaises(CorrelationError) as cm:
            build_correlations(raw if isinstance(raw, list) else [raw])
        if index is not None:
            self.assertEqual(cm.exception.segment_index, index)

    def test_segment_must_be_object(self) -> None:
        with self.assertRaises(CorrelationError) as cm:
            build_correlations(["nope"])
        self.assertEqual(cm.exception.segment_index, 0)

    def test_missing_and_unknown_fields(self) -> None:
        d = seg()
        del d["anchorTick"]
        self._rejected(d, 0)
        d = seg()
        d["bogus"] = 1
        self._rejected(d, 0)

    def test_bad_partition(self) -> None:
        for p in ("", "   ", 7, None):
            with self.subTest(p=p):
                self._rejected(seg(partition=p))  # type: ignore[arg-type]

    def test_empty_or_inverted_range(self) -> None:
        self._rejected(seg(start=10, end=10))
        self._rejected(seg(start=11, end=10))

    def test_anchor_tick_out_of_range(self) -> None:
        self._rejected(seg(start=0, end=100, anchor_tick=100))
        self._rejected(seg(start=0, end=100, anchor_tick=-1))

    def test_non_positive_slope_parts(self) -> None:
        self._rejected(seg(num=0))
        self._rejected(seg(num=-1))
        self._rejected(seg(den=0))
        self._rejected(seg(den=-2))

    def test_wrong_field_types(self) -> None:
        for field in ("tickRangeStart", "tickRangeEnd", "anchorTick",
                      "nanosecondsNumerator", "nanosecondsDenominator"):
            d = seg()
            d[field] = "1"  # quoted strings are not integers at the core
            with self.subTest(field=field):
                self._rejected(d)
            d[field] = True
            with self.subTest(field=field, booleans=True):
                self._rejected(d)

    def test_two_anchors_or_none(self) -> None:
        d = seg()
        d.update({"anchorGpsWeek": 1930, "anchorGpsSecondsInWeek": 18})
        self._rejected(d)
        d = seg()
        del d["anchorUtc"]
        self._rejected(d)

    def test_gps_anchor_partial_fields(self) -> None:
        d = gps_seg()
        del d["anchorGpsSecondsInWeek"]
        self._rejected(d)

    def test_illegal_anchor_utc(self) -> None:
        # Not a real leap-second position.
        self._rejected(seg(anchor_utc="2016-12-30T23:59:60Z"), 0)
        # Beyond table expiry.
        self._rejected(seg(anchor_utc="2018-01-01T00:00:00Z"), 0)
        # Before table era.
        self._rejected(seg(anchor_utc="1971-01-01T00:00:00Z"), 0)

    def test_illegal_anchor_gps(self) -> None:
        self._rejected([gps_seg(week=0, sow=604_800)], 0)
        self._rejected([gps_seg(week=-1, sow=0)], 0)


class ContiguityTests(unittest.TestCase):
    def test_touching_segments_agree_at_boundary(self) -> None:
        cs = build_correlations([
            seg(start=0, end=100, anchor_tick=0, num=NS_PER_MS, den=1),
            seg(start=100, end=200, anchor_tick=100,
                anchor_utc="2017-01-01T00:00:00.1Z", num=2 * NS_PER_MS, den=2),
        ])
        self.assertEqual(cs.resolve("A", 100).map_tick(100), T0 + 100 * NS_PER_MS)
        self.assertEqual(cs.resolve("A", 99).map_tick(99), T0 + 99 * NS_PER_MS)
        self.assertEqual(cs.resolve("A", 150).map_tick(150), T0 + 150 * NS_PER_MS)

    def test_overlap_rejected_with_later_index(self) -> None:
        with self.assertRaises(CorrelationError) as cm:
            build_correlations([
                seg(start=0, end=100),
                seg(start=99, end=200),
            ])
        self.assertEqual(cm.exception.segment_index, 1)
        self.assertIn("overlap", cm.exception.message)

    def test_gap_rejected(self) -> None:
        with self.assertRaises(CorrelationError) as cm:
            build_correlations([
                seg(start=0, end=100),
                seg(start=101, end=200,
                    anchor_utc="2017-01-01T00:00:00.2Z"),
            ])
        self.assertEqual(cm.exception.segment_index, 1)
        self.assertIn("gap", cm.exception.message)

    def test_boundary_disagreement_rejected(self) -> None:
        with self.assertRaises(CorrelationError) as cm:
            build_correlations([
                seg(start=0, end=100, num=NS_PER_MS),
                seg(start=100, end=200,
                    anchor_utc="2017-01-01T00:00:01Z", num=NS_PER_MS),
            ])
        self.assertEqual(cm.exception.segment_index, 1)
        self.assertIn("boundary", cm.exception.message)

    def test_boundary_sub_nanosecond_rejected_as_segment_error(self) -> None:
        # Slope 1/2 ns per tick: the boundary tick 3 is unreachable from
        # anchor 0 on the first segment (odd residue), so the segments
        # cannot be joined even though they geometrically touch.
        with self.assertRaises(CorrelationError) as cm:
            build_correlations([
                seg(start=0, end=3, num=1, den=2),
                seg(start=3, end=10, anchor_tick=3, num=1, den=2,
                    anchor_utc="2017-01-01T00:00:00Z"),
            ])
        self.assertEqual(cm.exception.segment_index, 1)

    def test_unsorted_submission_still_validated(self) -> None:
        with self.assertRaises(CorrelationError) as cm:
            build_correlations([
                seg(start=100, end=200,
                    anchor_utc="2017-01-01T00:00:00.1Z"),
                seg(start=0, end=101),  # overlaps segment 0
            ])
        self.assertEqual(cm.exception.segment_index, 1)

    def test_distinct_partitions_are_independent(self) -> None:
        cs = build_correlations([
            seg(partition="A", start=0, end=10),
            seg(partition="B", start=0, end=10,
                anchor_utc="2017-02-01T00:00:00Z"),
        ])
        self.assertNotEqual(
            cs.resolve("A", 0).map_tick(0),
            cs.resolve("B", 0).map_tick(0),
        )


class OnboardEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cs = build_correlations([
            seg(start=0, end=100, num=NS_PER_MS),
            seg(start=100, end=200, anchor_tick=100,
                anchor_utc="2017-01-01T00:00:00.1Z", num=NS_PER_MS),
        ])

    def test_maps_to_same_event_shape_as_utc(self) -> None:
        ev = normalize_onboard_event(self.cs, "A", 25)
        self.assertEqual(ev.tai_ns, T0 + 25 * NS_PER_MS)
        self.assertEqual(ev.utc_canonical, "2017-01-01T00:00:00.025Z")
        self.assertEqual(ev.utc_minus_tai, -37)
        resp = ev.to_response()
        self.assertEqual(resp["taiNanoseconds"], str(T0 + 25 * NS_PER_MS))

    def test_boundary_tick_belongs_to_later_segment(self) -> None:
        # Half-open ranges: tick 100 falls only in the second segment, and
        # both segments agree there.
        ev = normalize_onboard_event(self.cs, "A", 100)
        self.assertEqual(ev.tai_ns, T0 + 100 * NS_PER_MS)

    def test_unknown_partition(self) -> None:
        with self.assertRaises(TimeConversionError):
            normalize_onboard_event(self.cs, "ZZ", 0)

    def test_uncovered_tick_at_gap_edges(self) -> None:
        for tick in (-1, 200):
            with self.subTest(tick=tick):
                with self.assertRaises(TimeConversionError):
                    normalize_onboard_event(self.cs, "A", tick)

    def test_bad_arguments(self) -> None:
        with self.assertRaises(TimeConversionError):
            normalize_onboard_event(self.cs, "", 0)
        with self.assertRaises(TimeConversionError):
            normalize_onboard_event(self.cs, 7, 0)  # type: ignore[arg-type]
        with self.assertRaises(TimeConversionError):
            normalize_onboard_event(self.cs, "A", "1")  # type: ignore[arg-type]
        with self.assertRaises(TimeConversionError):
            normalize_onboard_event(self.cs, "A", True)  # type: ignore[arg-type]

    def test_sub_nanosecond_result_rejected(self) -> None:
        cs = build_correlations([seg(num=1, den=2)])
        normalize_onboard_event(cs, "A", 2)   # exact 1 ns
        with self.assertRaises(TimeConversionError):
            normalize_onboard_event(cs, "A", 1)
        with self.assertRaises(TimeConversionError):
            normalize_onboard_event(cs, "A", 99)

    def test_fractional_slope_exact_points(self) -> None:
        # 3 ns per 2 counts: anchor 0 exact; even ticks exact; odd rejected.
        cs = build_correlations([seg(num=3, den=2)])
        self.assertEqual(
            normalize_onboard_event(cs, "A", 4).tai_ns, T0 + 6
        )

    def test_mapped_instant_outside_supported_era(self) -> None:
        # Anchor inside the era, but a large positive tick walks past the
        # table expiry.
        cs = build_correlations([
            seg(anchor_utc="2017-06-27T23:59:59Z", num=1_000_000_000),
        ])
        with self.assertRaises(TimeConversionError):
            normalize_onboard_event(cs, "A", 10)


# ---------------------------------------------------------------------------
# HTTP batch layer
# ---------------------------------------------------------------------------

from app.server import RequestError, normalize_batch  # noqa: E402


def enc(obj: object) -> bytes:
    return json.dumps(obj).encode()


class BatchOnboardTests(unittest.TestCase):
    def test_mixed_batch_order_preserved(self) -> None:
        payload = {
            "correlations": [
                seg(start=0, end=100, num=NS_PER_MS),
                seg(start=100, end=200, anchor_tick=100,
                    anchor_utc="2017-01-01T00:00:00.1Z", num=NS_PER_MS),
                gps_seg(partition="B", start=0, end=10, num=1),
            ],
            "events": [
                {"id": "u", "utc": "2017-01-01T00:00:00Z"},
                {"id": "a0", "clockPartition": "A", "onboardTick": 0},
                {"id": "g", "gpsWeek": 1930, "gpsSecondsInWeek": 18},
                {"id": "a-edge", "clockPartition": "A", "onboardTick": 100},
                {"id": "a150", "clockPartition": "A", "onboardTick": 150},
                {"id": "b5", "clockPartition": "B", "onboardTick": 5},
            ],
        }
        out = normalize_batch(enc(payload))
        self.assertEqual(
            [r["id"] for r in out], ["u", "a0", "g", "a-edge", "a150", "b5"]
        )
        self.assertEqual(out[0]["taiNanoseconds"], out[1]["taiNanoseconds"])
        self.assertEqual(out[1]["taiNanoseconds"], out[2]["taiNanoseconds"])
        self.assertEqual(
            int(out[3]["taiNanoseconds"]) - int(out[1]["taiNanoseconds"]),
            100 * NS_PER_MS,
        )
        self.assertEqual(
            int(out[4]["taiNanoseconds"]) - int(out[1]["taiNanoseconds"]),
            150 * NS_PER_MS,
        )
        self.assertEqual(
            int(out[5]["taiNanoseconds"]) - int(out[2]["taiNanoseconds"]), 5
        )
        for r in out:
            self.assertEqual(set(r), {
                "id", "taiNanoseconds", "utc", "utcTaiOffsetSeconds"})

    def test_onboard_without_correlations_is_event_error(self) -> None:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({"events": [
                {"id": "x", "clockPartition": "A", "onboardTick": 3}]}))
        self.assertEqual(cm.exception.code, "INVALID_EVENT")
        self.assertEqual(cm.exception.event_index, 0)
        self.assertEqual(cm.exception.event_id, "x")

    def test_segment_overlap_reports_correlation_index(self) -> None:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "correlations": [seg(start=0, end=100), seg(start=99, end=200)],
                "events": [{"id": "x", "clockPartition": "A",
                            "onboardTick": 5}],
            }))
        self.assertEqual(cm.exception.code, "INVALID_CORRELATION")
        self.assertEqual(cm.exception.correlation_index, 1)
        self.assertIsNone(cm.exception.event_index)

    def test_illegal_anchor_reports_segment_index(self) -> None:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "correlations": [seg(anchor_utc="2018-01-01T00:00:00Z")],
                "events": [{"id": "x", "clockPartition": "A",
                            "onboardTick": 5}],
            }))
        self.assertEqual(cm.exception.code, "INVALID_CORRELATION")
        self.assertEqual(cm.exception.correlation_index, 0)

    def test_uncovered_event_reports_event_index(self) -> None:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "correlations": [seg(start=0, end=100)],
                "events": [
                    {"id": "ok", "clockPartition": "A", "onboardTick": 5},
                    {"id": "lost", "clockPartition": "A",
                     "onboardTick": 100},
                ],
            }))
        self.assertEqual(cm.exception.code, "INVALID_EVENT")
        self.assertEqual(cm.exception.event_index, 1)
        self.assertEqual(cm.exception.event_id, "lost")

    def test_sub_nanosecond_event_rejected_batchwide(self) -> None:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "correlations": [seg(num=1, den=2)],
                "events": [{"id": "frac", "clockPartition": "A",
                            "onboardTick": 1}],
            }))
        self.assertEqual(cm.exception.event_index, 0)
        self.assertEqual(cm.exception.event_id, "frac")
        self.assertFalse(hasattr(cm.exception, "results"))

    def test_correlations_must_be_array_and_sized(self) -> None:
        body = {"correlations": {}, "events": [
            {"id": "x", "utc": "2017-01-01T00:00:00Z"}]}
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc(body))
        self.assertEqual(cm.exception.code, "INVALID_CORRELATION")
        body = {"correlations": [], "events": [
            {"id": "x", "utc": "2017-01-01T00:00:00Z"}]}
        with self.assertRaises(RequestError):
            normalize_batch(enc(body))

    def test_float_and_strings_in_segments_rejected(self) -> None:
        raw = (
            b'{"correlations":[{"clockPartition":"A","tickRangeStart":0,'
            b'"tickRangeEnd":100,"anchorTick":0,"anchorUtc":'
            b'"2017-01-01T00:00:00Z","nanosecondsNumerator":1.5,'
            b'"nanosecondsDenominator":1}],"events":'
            b'[{"id":"x","clockPartition":"A","onboardTick":5}]}'
        )
        with self.assertRaises(RequestError) as cm:
            normalize_batch(raw)
        self.assertIn("floating-point", cm.exception.message)

        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "correlations": [{**seg(), "tickRangeStart": "0"}],
                "events": [{"id": "x", "clockPartition": "A",
                            "onboardTick": 5}],
            }))
        self.assertEqual(cm.exception.code, "INVALID_CORRELATION")

    def test_onboard_tick_must_be_json_integer(self) -> None:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "correlations": [seg()],
                "events": [{"id": "x", "clockPartition": "A",
                            "onboardTick": "7"}],
            }))
        self.assertEqual(cm.exception.event_id, "x")
        self.assertIn("onboardTick", cm.exception.message)

    def test_clock_partition_must_be_json_string(self) -> None:
        # A JSON number literal arrives as an int-like token; it must not
        # be silently accepted as a partition name.
        raw = (
            b'{"correlations":[{"clockPartition":"A","tickRangeStart":0,'
            b'"tickRangeEnd":100,"anchorTick":0,"anchorUtc":'
            b'"2017-01-01T00:00:00Z","nanosecondsNumerator":1,'
            b'"nanosecondsDenominator":1}],"events":'
            b'[{"id":"x","clockPartition":7,"onboardTick":1}]}'
        )
        with self.assertRaises(RequestError) as cm:
            normalize_batch(raw)
        self.assertEqual(cm.exception.event_id, "x")
        self.assertIn("clockPartition", cm.exception.message)

    def test_numeric_partition_in_segment_rejected(self) -> None:
        d = seg()
        d["clockPartition"] = 7
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "correlations": [d],
                "events": [{"id": "x", "clockPartition": "A",
                            "onboardTick": 1}],
            }))
        self.assertEqual(cm.exception.correlation_index, 0)

    def test_onboard_event_field_combination_rules(self) -> None:
        base_corr = [seg()]
        for ev in (
            {"id": "x", "clockPartition": "A", "onboardTick": 1,
             "utc": "2017-01-01T00:00:00Z"},
            {"id": "x", "clockPartition": "A"},
            {"id": "x", "onboardTick": 1},
        ):
            with self.subTest(ev=ev):
                with self.assertRaises(RequestError) as cm:
                    normalize_batch(enc({
                        "correlations": base_corr, "events": [ev]}))
                self.assertEqual(cm.exception.code, "INVALID_EVENT")

    def test_unknown_top_level_and_event_fields(self) -> None:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "correlations": [seg()],
                "events": [{"id": "x", "clockPartition": "A",
                            "onboardTick": 1, "rateScale": 2}],
            }))
        self.assertIn("unknown", cm.exception.message)
        with self.assertRaises(RequestError):
            normalize_batch(enc({
                "correlations": [seg()],
                "events": [{"id": "x", "utc": "2017-01-01T00:00:00Z"}],
                "leap": 37,
            }))

    def test_duplicate_ids_still_checked_with_correlations(self) -> None:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({
                "correlations": [seg()],
                "events": [
                    {"id": "x", "clockPartition": "A", "onboardTick": 1},
                    {"id": "x", "clockPartition": "A", "onboardTick": 2},
                ],
            }))
        self.assertEqual(cm.exception.event_index, 1)

    def test_legacy_request_without_correlations_unchanged(self) -> None:
        out = normalize_batch(enc({"events": [
            {"id": "x", "utc": "2017-01-01T00:00:00Z"}]}))
        self.assertEqual(out[0]["taiNanoseconds"], str(T0))
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc({"events": [
                {"id": "x", "utc": "2017-01-01T00:00:00Z"}], "extra": 1}))
        self.assertEqual(
            cm.exception.message,
            "request body may only contain the 'events' field",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
