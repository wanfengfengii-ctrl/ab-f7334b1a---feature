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

    print("[smoke] onboard clock correlations across a segment boundary")
    # Partition 7, two abutting segments meeting at tick 1_000_000:
    #   seg 0: [0, 1_000_000),     anchor tick 0     = 2017-01-01T00:00:00Z,
    #                               1000 ns/tick;
    #   seg 1: [1_000_000, 2_000_000), anchor tick 1_000_000 anchored via GPS
    #                               at 2017-01-01T00:00:01Z, scale written
    #                               as 2000/2 ns per tick (still 1000).
    # The boundary tick must map to the same TAI instant from both sides.
    correlations = [
        {
            "clockPartition": 7, "tickStart": 0, "tickEnd": 1_000_000,
            "anchorTick": 0,
            "nanosecondsPerTickNumerator": 1000,
            "nanosecondsPerTickDenominator": 1,
            "utc": "2017-01-01T00:00:00Z",
        },
        {
            "clockPartition": 7, "tickStart": 1_000_000,
            "tickEnd": 2_000_000, "anchorTick": 1_000_000,
            "nanosecondsPerTickNumerator": 2000,
            "nanosecondsPerTickDenominator": 2,
            "gpsWeek": 1930, "gpsSecondsInWeek": 19,
        },
    ]
    status, body = post({
        "correlations": correlations,
        "events": [
            {"id": "last-before", "clockPartition": 7,
             "onboardTick": 999_999},
            {"id": "boundary", "clockPartition": 7, "onboardTick": 1_000_000},
            {"id": "first-after", "clockPartition": 7,
             "onboardTick": 1_000_001},
            # Same physical instants submitted as UTC/GPS must match.
            {"id": "boundary-gps", "gpsWeek": 1930,
             "gpsSecondsInWeek": 19, "gpsNanoseconds": 0},
            {"id": "leap-utc-2", "utc": "2016-12-31T23:59:60Z"},
        ],
    })
    check(status == 200, f"correlation batch status 200 (got {status})")
    if status != 200:
        print(json.dumps(body, indent=2))
        return 1
    results = body["results"]
    check([r["id"] for r in results] == [
        "last-before", "boundary", "first-after",
        "boundary-gps", "leap-utc-2",
    ], "correlation result order matches input order")
    last_before, boundary, first_after, boundary_gps, leap_utc = (
        by_id(results, k) for k in (
            "last-before", "boundary", "first-after",
            "boundary-gps", "leap-utc-2")
    )
    check(
        boundary["taiNanoseconds"] == boundary_gps["taiNanoseconds"]
        == "1483228838000000000",
        "shared boundary tick maps to the GPS-anchored TAI instant",
    )
    check(
        int(boundary["taiNanoseconds"])
        - int(last_before["taiNanoseconds"]) == 1000
        and int(first_after["taiNanoseconds"])
        - int(boundary["taiNanoseconds"]) == 1000,
        "1000 ns/tick scale holds on both sides of the segment boundary",
    )
    check(
        boundary["utc"] == "2017-01-01T00:00:01Z"
        and boundary["utcTaiOffsetSeconds"] == "-37"
        and leap_utc["utcTaiOffsetSeconds"] == "-36",
        "onboard instants interleave correctly with UTC/GPS events",
    )

    print("[smoke] invalid correlation batches fail wholesale")
    bad = json.loads(json.dumps(correlations))
    bad[1]["gpsSecondsInWeek"] = 20  # boundary would map to T0 + 2 s
    status, body = post({"correlations": bad, "events": [
        {"id": "t", "clockPartition": 7, "onboardTick": 0},
    ]})
    err = body.get("error", {})
    check(status == 400, "boundary disagreement -> 400")
    check(err.get("correlationIndex") == 0
          and err.get("relatedCorrelationIndex") == 1,
          "error locates both abutting correlations")
    check("results" not in body, "no partial results on correlation error")

    status, body = post({"correlations": correlations, "events": [
        {"id": "uncovered", "clockPartition": 7, "onboardTick": 2_000_000},
    ]})
    err = body.get("error", {})
    check(status == 400 and err.get("eventId") == "uncovered"
          and err.get("eventIndex") == 0
          and "not covered" in err.get("message", ""),
          "tick outside every segment -> 400 located to the event")

    # Sub-nanosecond rejection with a 1 ns / 2 ticks scale: an odd tick
    # offset from the anchor leaves a half-nanosecond remainder.
    half = [{
        "clockPartition": 7, "tickStart": 0, "tickEnd": 1_000_000,
        "anchorTick": 0,
        "nanosecondsPerTickNumerator": 1,
        "nanosecondsPerTickDenominator": 2,
        "utc": "2017-01-01T00:00:00Z",
    }]
    status, body = post({"correlations": half, "events": [
        {"id": "halftick", "clockPartition": 7, "onboardTick": 1},
    ]})
    err = body.get("error", {})
    check(status == 400 and err.get("eventId") == "halftick"
          and err.get("correlationIndex") == 0
          and "nanosecond" in err.get("message", ""),
          "sub-nanosecond mapping -> 400 located to event and correlation")

    if failures:
        print(f"[smoke] {len(failures)} FAILURE(S)")
        return 1
    print("[smoke] ALL HTTP SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
