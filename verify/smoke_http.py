"""Cross-leap-second HTTP smoke test, executed inside the API container.

Talks real HTTP over the loopback interface and exits non-zero on the
first discrepancy.  Every assertion is about serialized (decimal string)
values exactly as a deep-space ground-system client would see them.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = (
    f"http://{os.environ.get('API_HOST', '127.0.0.1')}:"
    f"{os.environ.get('PORT', '8080')}"
)
URL = BASE + "/api/times/normalize"
failures: list[str] = []


def check(cond: bool, message: str) -> None:
    if cond:
        print(f"  ok  {message}")
    else:
        print(f"FAIL  {message}")
        failures.append(message)


def post(body: dict[str, object]) -> tuple[int, dict[str, object]]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        URL, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def get_health() -> int:
    with urllib.request.urlopen(BASE + "/healthz", timeout=5) as resp:
        return resp.status


def by_id(results: list[dict[str, str]], key: str) -> dict[str, str]:
    return next(r for r in results if r["id"] == key)


def main() -> int:
    print("[smoke] GET /healthz")
    check(get_health() == 200, "health endpoint returns 200")

    print("[smoke] batch spanning the 2016-12-31 leap second")
    status, body = post({
        "events": [
            {"id": "pre-utc", "utc": "2016-12-31T23:59:59Z"},
            {"id": "leap-utc", "utc": "2016-12-31T23:59:60.5Z"},
            {"id": "post-utc", "utc": "2017-01-01T00:00:00Z"},
            # Identical physical instants expressed via GPS week/SOW:
            {"id": "pre-gps", "gpsWeek": 1930,
             "gpsSecondsInWeek": 16, "gpsNanoseconds": 0},
            {"id": "leap-gps", "gpsWeek": 1930,
             "gpsSecondsInWeek": 17, "gpsNanoseconds": 500_000_000},
            {"id": "post-gps", "gpsWeek": 1930,
             "gpsSecondsInWeek": 18, "gpsNanoseconds": 0},
            {"id": "gps-epoch", "gpsWeek": 0,
             "gpsSecondsInWeek": 0, "gpsNanoseconds": 0},
        ]
    })
    check(status == 200, f"normalize batch status 200 (got {status})")
    if status != 200:
        print(json.dumps(body, indent=2))
        return 1

    results = body["results"]
    check([r["id"] for r in results] == [
        "pre-utc", "leap-utc", "post-utc",
        "pre-gps", "leap-gps", "post-gps", "gps-epoch",
    ], "result order matches input order")

    pre_u, leap_u, post_u = (by_id(results, k) for k in
                             ("pre-utc", "leap-utc", "post-utc"))
    pre_g, leap_g, post_g = (by_id(results, k) for k in
                             ("pre-gps", "leap-gps", "post-gps"))
    epoch = by_id(results, "gps-epoch")

    check(
        pre_u["taiNanoseconds"] == pre_g["taiNanoseconds"]
        == "1483228835000000000",
        "pre-leap UTC and GPS share TAI 1483228835000000000",
    )
    check(
        leap_u["taiNanoseconds"] == leap_g["taiNanoseconds"]
        == "1483228836500000000",
        "leap second 23:59:60.5 UTC and GPS share TAI 1483228836500000000",
    )
    check(
        post_u["taiNanoseconds"] == post_g["taiNanoseconds"]
        == "1483228837000000000",
        "post-leap UTC and GPS share TAI 1483228837000000000",
    )
    check(
        int(leap_u["taiNanoseconds"]) - int(pre_u["taiNanoseconds"])
        == 1_500_000_000
        and int(post_u["taiNanoseconds"])
        - int(leap_u["taiNanoseconds"]) == 500_000_000,
        "the leap second physically exists (pre -> :60.5 = 1.5 s,"
        " :60.5 -> midnight = 0.5 s)",
    )
    check(
        pre_u["utcTaiOffsetSeconds"] == pre_g["utcTaiOffsetSeconds"]
        == "-36",
        "offset is -36 before/at the leap second",
    )
    check(
        leap_u["utcTaiOffsetSeconds"] == "-36"
        and leap_u["utc"] == "2016-12-31T23:59:60.5Z",
        "offset stays -36 *during* 23:59:60 and UTC renders the :60",
    )
    check(
        post_u["utcTaiOffsetSeconds"] == post_g["utcTaiOffsetSeconds"]
        == "-37",
        "offset becomes -37 at 2017-01-01T00:00:00Z",
    )
    check(
        epoch["utc"] == "1980-01-06T00:00:00Z"
        and epoch["taiNanoseconds"] == "315964819000000000",
        "GPS week 0 SOW 0 maps to 1980-01-06T00:00:00Z (TAI -19 s)",
    )

    print("[smoke] invalid batches fail wholesale with locatable errors")
    status, body = post({"events": [
        {"id": "fine", "utc": "2016-12-31T23:59:59Z"},
        {"id": "bad-leap", "utc": "2016-12-30T23:59:60Z"},
    ]})
    err = body.get("error", {})
    check(status == 400, "fake leap position -> 400")
    check(err.get("eventId") == "bad-leap", "error names event id")
    check(err.get("eventIndex") == 1, "error gives zero-based index")
    check("results" not in body, "no partial results in error response")
    check("leap second" in err.get("message", ""),
          "message pinpoints the leap-second issue")

    status, body = post({"events": [
        {"id": "bad-sow", "gpsWeek": 0, "gpsSecondsInWeek": 604_800},
    ]})
    err = body.get("error", {})
    check(status == 400 and err.get("eventId") == "bad-sow"
          and "604799" in err.get("message", ""),
          "out-of-range SOW -> 400 with id and legal range")

    status, body = post({"events": [
        {"id": "too-new", "utc": "2018-01-01T00:00:00Z"},
    ]})
    err = body.get("error", {})
    check(status == 400 and err.get("eventId") == "too-new"
          and "2017-06-28" in err.get("message", ""),
          "event beyond supported era -> 400 naming the table expiry")

    status, body = post({"events": [
        {"id": "dup", "utc": "2016-12-31T23:59:59Z"},
        {"id": "dup", "utc": "2016-12-31T23:59:60Z"},
    ]})
    check(status == 400 and body["error"].get("eventIndex") == 1,
          "duplicate ids -> 400 at the second occurrence")

    status, _ = post({"events": [
        {"id": i, "utc": "2016-12-31T23:59:59Z"} for i in range(201)
    ]})
    check(status == 400, "201 events -> 400")

    status, body = post({"events": [
        {"id": "float", "gpsWeek": 1.0, "gpsSecondsInWeek": 1},
    ]})
    check(status == 400 and "floating-point"
          in body["error"].get("message", ""),
          "floating-point JSON number rejected")

    print("[smoke] onboard-clock correlation segments spanning a segment"
          " boundary")
    # Two touching segments of partition 'A', both with 1 ms per tick but
    # expressed with different numerator/denominator pairs, anchored at
    # 2017-01-01T00:00:00Z and ...00.1Z respectively.  Partition 'B' is an
    # independent 1-ns-per-tick clock anchored via GPS (1930:18 == the same
    # UTC midnight).  The batch mixes UTC, GPS and onboard events.
    status, body = post({
        "correlations": [
            {"clockPartition": "A", "tickRangeStart": 0,
             "tickRangeEnd": 100, "anchorTick": 0,
             "anchorUtc": "2017-01-01T00:00:00Z",
             "nanosecondsNumerator": 1000000,
             "nanosecondsDenominator": 1},
            {"clockPartition": "A", "tickRangeStart": 100,
             "tickRangeEnd": 200, "anchorTick": 100,
             "anchorUtc": "2017-01-01T00:00:00.1Z",
             "nanosecondsNumerator": 2000000,
             "nanosecondsDenominator": 2},
            {"clockPartition": "B", "tickRangeStart": 0,
             "tickRangeEnd": 1000, "anchorTick": 0,
             "anchorGpsWeek": 1930, "anchorGpsSecondsInWeek": 18,
             "nanosecondsNumerator": 1,
             "nanosecondsDenominator": 1},
        ],
        "events": [
            {"id": "midnight-utc", "utc": "2017-01-01T00:00:00Z"},
            {"id": "a0", "clockPartition": "A", "onboardTick": 0},
            {"id": "a99", "clockPartition": "A", "onboardTick": 99},
            # The shared boundary tick must map to the same TAI instant
            # through both segments.
            {"id": "a-boundary", "clockPartition": "A",
             "onboardTick": 100},
            {"id": "a150", "clockPartition": "A", "onboardTick": 150},
            {"id": "b7", "clockPartition": "B", "onboardTick": 7},
            {"id": "midnight-gps", "gpsWeek": 1930,
             "gpsSecondsInWeek": 18, "gpsNanoseconds": 0},
        ],
    })
    check(status == 200, f"correlation batch status 200 (got {status})")
    if status != 200:
        print(json.dumps(body, indent=2))
        return 1

    results = body["results"]
    check([r["id"] for r in results] == [
        "midnight-utc", "a0", "a99", "a-boundary", "a150", "b7",
        "midnight-gps",
    ], "mixed UTC/GPS/onboard result order matches input order")
    midnight = by_id(results, "midnight-utc")
    a0 = by_id(results, "a0")
    a99 = by_id(results, "a99")
    ab = by_id(results, "a-boundary")
    a150 = by_id(results, "a150")
    b7 = by_id(results, "b7")
    mgps = by_id(results, "midnight-gps")

    check(
        midnight["taiNanoseconds"] == a0["taiNanoseconds"]
        == mgps["taiNanoseconds"] == "1483228837000000000",
        "UTC anchor, partition-A tick 0 and GPS 1930:18 share one TAI"
        " instant",
    )
    check(
        a99["taiNanoseconds"] == "1483228837099000000"
        and ab["taiNanoseconds"] == "1483228837100000000"
        and a150["taiNanoseconds"] == "1483228837150000000",
        "ticks 99/100/150 land at 99 ms / 100 ms / 150 ms on the TAI axis",
    )
    check(
        ab["utc"] == "2017-01-01T00:00:00.1Z",
        "the boundary tick renders through the canonical UTC label",
    )
    check(
        int(b7["taiNanoseconds"]) - int(mgps["taiNanoseconds"]) == 7,
        "independent partition B advances 1 ns per tick",
    )
    for r in results:
        check(set(r) == {"id", "taiNanoseconds", "utc",
                         "utcTaiOffsetSeconds"},
              f"event {r['id']} carries exactly the three time fields")

    print("[smoke] correlation-specific invalid batches fail wholesale")

    def seg_a(start, end, anchor_tick, anchor_utc):
        return {"clockPartition": "A", "tickRangeStart": start,
                "tickRangeEnd": end, "anchorTick": anchor_tick,
                "anchorUtc": anchor_utc,
                "nanosecondsNumerator": 1000000,
                "nanosecondsDenominator": 1}

    status, body = post({
        "correlations": [
            seg_a(0, 100, 0, "2017-01-01T00:00:00Z"),
            seg_a(99, 200, 99, "2017-01-01T00:00:00.099Z"),
        ],
        "events": [{"id": "x", "clockPartition": "A", "onboardTick": 5}],
    })
    err = body.get("error", {})
    check(status == 400 and err.get("code") == "INVALID_CORRELATION"
          and err.get("correlationIndex") == 1,
          "overlapping segments -> 400 pinpointing segment index 1")
    check("results" not in body, "overlap error carries no results")

    status, body = post({
        "correlations": [
            seg_a(0, 100, 0, "2017-01-01T00:00:00Z"),
            seg_a(101, 200, 101, "2017-01-01T00:00:00.2Z"),
        ],
        "events": [{"id": "x", "clockPartition": "A", "onboardTick": 5}],
    })
    err = body.get("error", {})
    check(status == 400 and err.get("correlationIndex") == 1
          and "gap" in err.get("message", ""),
          "non-contiguous (gapped) segments -> 400 naming the gap")

    status, body = post({
        "correlations": [
            seg_a(0, 100, 0, "2017-01-01T00:00:00Z"),
            seg_a(100, 200, 100, "2017-01-01T00:00:01Z"),
        ],
        "events": [{"id": "x", "clockPartition": "A", "onboardTick": 5}],
    })
    err = body.get("error", {})
    check(status == 400 and err.get("correlationIndex") == 1
          and "boundary" in err.get("message", ""),
          "segments disagreeing at the common boundary -> 400")

    status, body = post({
        "correlations": [
            seg_a(0, 100, 0, "2018-01-01T00:00:00Z"),
        ],
        "events": [{"id": "x", "clockPartition": "A", "onboardTick": 5}],
    })
    err = body.get("error", {})
    check(status == 400 and err.get("correlationIndex") == 0
          and "2017-06-28" in err.get("message", ""),
          "illegal segment anchor beyond table expiry -> 400 at segment 0")

    status, body = post({
        "correlations": [
            {"clockPartition": "A", "tickRangeStart": 0,
             "tickRangeEnd": 100, "anchorTick": 0,
             "anchorUtc": "2017-01-01T00:00:00Z",
             "nanosecondsNumerator": 1, "nanosecondsDenominator": 2},
        ],
        "events": [{"id": "odd", "clockPartition": "A",
                    "onboardTick": 1}],
    })
    err = body.get("error", {})
    check(status == 400 and err.get("eventId") == "odd"
          and err.get("eventIndex") == 0,
          "sub-nanosecond onboard tick -> 400 pinpointing the event")

    status, body = post({
        "correlations": [seg_a(0, 100, 0, "2017-01-01T00:00:00Z")],
        "events": [{"id": "uncovered", "clockPartition": "A",
                    "onboardTick": 100}],
    })
    err = body.get("error", {})
    check(status == 400 and err.get("eventId") == "uncovered"
          and "covered" in err.get("message", ""),
          "tick outside every half-open range -> 400 pinpointing event")

    if failures:
        print(f"[smoke] {len(failures)} FAILURE(S)")
        return 1
    print("[smoke] ALL HTTP SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
