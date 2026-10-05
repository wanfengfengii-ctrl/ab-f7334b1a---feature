"""Tests for the batch HTTP layer (no network: calls normalize_batch)."""

from __future__ import annotations

import json
import unittest

from app.server import (
    MAX_EVENTS,
    RequestError,
    normalize_batch,
)


def enc(obj: object) -> bytes:
    return json.dumps(obj).encode()


class BatchHappyPathTests(unittest.TestCase):
    def test_order_preserved_and_mixed_inputs(self) -> None:
        payload = {
            "events": [
                {"id": "a", "utc": "2016-12-31T23:59:60Z"},
                {"id": 7, "gpsWeek": 1930, "gpsSecondsInWeek": 18},
                {"id": "c", "gpsWeek": 0, "gpsSecondsInWeek": 0,
                 "gpsNanoseconds": 1},
                {"id": "d", "utc": "1972-01-01T00:00:00Z"},
            ]
        }
        out = normalize_batch(enc(payload))
        self.assertEqual([r["id"] for r in out], ["a", "7", "c", "d"])
        self.assertEqual(out[0]["utc"], "2016-12-31T23:59:60Z")
        self.assertEqual(out[0]["utcTaiOffsetSeconds"], "-36")
        self.assertEqual(out[1]["utc"], "2017-01-01T00:00:00Z")
        self.assertEqual(out[1]["utcTaiOffsetSeconds"], "-37")
        self.assertEqual(out[2]["utc"], "1980-01-06T00:00:00.000000001Z")
        self.assertEqual(out[3]["utcTaiOffsetSeconds"], "-10")
        for r in out:
            int(r["taiNanoseconds"])  # decimal string
            self.assertIsInstance(r["taiNanoseconds"], str)

    def test_same_instant_two_inputs_identical_values(self) -> None:
        payload = {
            "events": [
                {"id": "via-gps", "gpsWeek": 1930,
                 "gpsSecondsInWeek": 17, "gpsNanoseconds": 500_000_000},
                {"id": "via-utc", "utc": "2016-12-31T23:59:60.5Z"},
            ]
        }
        out = normalize_batch(enc(payload))
        self.assertEqual(
            out[0]["taiNanoseconds"], out[1]["taiNanoseconds"]
        )
        self.assertEqual(out[0]["utc"], out[1]["utc"])
        self.assertEqual(
            out[0]["utcTaiOffsetSeconds"], out[1]["utcTaiOffsetSeconds"]
        )

    def test_up_to_200_events(self) -> None:
        events = [
            {"id": i, "utc": "2016-12-31T23:59:59Z"}
            for i in range(MAX_EVENTS)
        ]
        self.assertEqual(len(normalize_batch(enc({"events": events}))), 200)


class BatchErrorTests(unittest.TestCase):
    def _expect_error(self, payload: object) -> RequestError:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(enc(payload))
        return cm.exception

    def test_bad_event_reports_id_and_index(self) -> None:
        exc = self._expect_error({
            "events": [
                {"id": "ok-1", "utc": "2016-12-31T23:59:59Z"},
                {"id": "ev-42", "utc": "2016-12-30T23:59:60Z"},
            ]
        })
        self.assertEqual(exc.status, 400)
        self.assertEqual(exc.code, "INVALID_EVENT")
        self.assertEqual(exc.event_index, 1)
        self.assertEqual(exc.event_id, "ev-42")
        self.assertIn("leap second", exc.message)

    def test_invalid_gps_reports_id_and_index(self) -> None:
        exc = self._expect_error({
            "events": [
                {"id": "g1", "gpsWeek": 1930,
                 "gpsSecondsInWeek": 604_800},
            ]
        })
        self.assertEqual(exc.event_index, 0)
        self.assertEqual(exc.event_id, "g1")
        self.assertIn("gpsSecondsInWeek", exc.message)

    def test_error_never_carries_partial_results(self) -> None:
        # Normalize raises; callers can only catch the error, and the error
        # has no result payload field.
        payload = {
            "events": [
                {"id": "ok-1", "utc": "2016-12-31T23:59:59Z"},
                {"id": "ok-2", "utc": "2016-12-31T23:59:60Z"},
                {"id": "bad", "utc": "2018-01-01T00:00:00Z"},
                {"id": "ok-4", "utc": "2017-01-01T00:00:00Z"},
            ]
        }
        exc = self._expect_error(payload)
        self.assertFalse(
            hasattr(exc, "results"),
            "error must not contain partial normalization results",
        )
        self.assertNotIn("taiNanoseconds", vars(exc).__repr__())

    def test_duplicate_ids_rejected(self) -> None:
        exc = self._expect_error({
            "events": [
                {"id": "x", "utc": "2016-12-31T23:59:59Z"},
                {"id": "x", "utc": "2016-12-31T23:59:60Z"},
            ]
        })
        self.assertEqual(exc.event_index, 1)
        self.assertIn("duplicate", exc.message.lower())

    def test_duplicate_ids_int_and_string_distinct(self) -> None:
        # "7" and 7 collide because ids identify the same caller entity.
        exc = self._expect_error({
            "events": [
                {"id": 7, "utc": "2016-12-31T23:59:59Z"},
                {"id": "7", "utc": "2016-12-31T23:59:60Z"},
            ]
        })
        self.assertEqual(exc.event_index, 1)

    def test_batch_size_limits(self) -> None:
        exc = self._expect_error({"events": []})
        self.assertIn("between", exc.message)
        exc = self._expect_error({
            "events": [
                {"id": i, "utc": "2016-12-31T23:59:59Z"}
                for i in range(MAX_EVENTS + 1)
            ]
        })
        self.assertIn("200", exc.message)

    def test_malformed_json(self) -> None:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(b"{not json")
        self.assertIn("JSON", cm.exception.message)

    def test_floating_point_numbers_rejected(self) -> None:
        with self.assertRaises(RequestError) as cm:
            normalize_batch(
                b'{"events":[{"id":"x","gpsWeek":1.0,'
                b'"gpsSecondsInWeek":1}]}'
            )
        self.assertIn("floating-point", cm.exception.message)

    def test_both_kinds_or_neither(self) -> None:
        for event in (
            {"id": "x", "utc": "2016-12-31T23:59:59Z",
             "gpsWeek": 1, "gpsSecondsInWeek": 1},
            {"id": "x"},
        ):
            with self.subTest(event=event):
                self._expect_error({"events": [event]})

    def test_unknown_field_rejected(self) -> None:
        exc = self._expect_error({
            "events": [
                {"id": "x", "utc": "2016-12-31T23:59:59Z",
                 "leapSeconds": 37},
            ]
        })
        self.assertIn("unknown", exc.message)

    def test_string_gps_fields_rejected(self) -> None:
        exc = self._expect_error({
            "events": [
                {"id": "x", "gpsWeek": "1930",
                 "gpsSecondsInWeek": 17},
            ]
        })
        self.assertEqual(exc.event_id, "x")
        self.assertIn("gpsWeek", exc.message)

    def test_missing_id(self) -> None:
        exc = self._expect_error({
            "events": [{"utc": "2016-12-31T23:59:59Z"}]
        })
        self.assertEqual(exc.event_index, 0)
        self.assertIn("id", exc.message)

    def test_first_invalid_event_wins_no_side_effects(self) -> None:
        # Even when later events are also invalid, the first failure is
        # reported and nothing is returned.
        exc = self._expect_error({
            "events": [
                {"id": 1, "utc": "not-a-time"},
                {"id": 2, "utc": "also-bad"},
            ]
        })
        self.assertEqual(exc.event_index, 0)
        self.assertEqual(exc.event_id, "1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
