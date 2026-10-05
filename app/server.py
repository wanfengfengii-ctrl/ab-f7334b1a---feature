"""HTTP service for batch time-scale normalization.

Stdlib only.  JSON numbers are parsed with ``parse_int=str`` so that no
numeric value ever passes through a Python float; fractional/non-integer
JSON numbers are rejected outright.
"""

from __future__ import annotations

import bisect
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .timecore import (
    TickSegment,
    TimeConversionError,
    normalize_gps_event,
    normalize_onboard_event,
    normalize_utc_event,
)

MAX_BODY_BYTES = 1 << 20  # 1 MiB is far more than 200 small events
MAX_EVENTS = 200
MIN_EVENTS = 1
MIN_CORRELATIONS = 1
MAX_CORRELATIONS = 32

GPS_FIELDS = ("gpsWeek", "gpsSecondsInWeek", "gpsNanoseconds")
ONBOARD_FIELDS = ("clockPartition", "onboardTick")
ALLOWED_FIELDS = {"id", "utc", *GPS_FIELDS, *ONBOARD_FIELDS}

# A correlation segment carries its own UTC/GPS anchor in the *same* wire
# format as an event, plus its tick geometry and rational ns/tick scale.
CORRELATION_REQUIRED = (
    "clockPartition",
    "tickStart",
    "tickEnd",
    "anchorTick",
    "nanosecondsPerTickNumerator",
    "nanosecondsPerTickDenominator",
)
CORRELATION_ALLOWED = {
    *CORRELATION_REQUIRED, "utc", *GPS_FIELDS,
}


class RequestError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "INVALID_REQUEST",
        status: int = 400,
        event_index: int | None = None,
        event_id: object | None = None,
        correlation_index: int | None = None,
        related_correlation_index: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.event_index = event_index
        self.event_id = event_id
        self.correlation_index = correlation_index
        self.related_correlation_index = related_correlation_index


class _RejectFloat:
    def __call__(self, val: str) -> None:
        raise RequestError(
            f"floating-point JSON numbers are not accepted: {val}"
        )


class _IntToken(str):
    """Marker subclass: a JSON number literal (never a JSON string)."""


def _reject_constant(name: str) -> None:
    raise RequestError(f"non-finite JSON constant is not accepted: {name}")


def _id_to_key(value: object) -> str:
    if isinstance(value, bool) or value is None:
        raise RequestError("event id must be a string or an integer")
    if isinstance(value, int) or isinstance(value, str):
        if isinstance(value, str) and not value.strip():
            raise RequestError("event id must not be empty or blank")
        return str(value)
    raise RequestError("event id must be a string or an integer")


def _gps_int(value: object, field: str) -> int:
    # JSON integer literals arrive as _IntToken; JSON strings are a
    # different (plain str) type and are rejected.
    if isinstance(value, _IntToken):
        return int(value)
    raise RequestError(
        f"{field} must be a JSON integer number (quoted strings are not"
        " accepted)"
    )


def _normalize_payload(raw: bytes) -> tuple[list[dict[str, object]], object]:
    try:
        payload = json.loads(
            raw,
            parse_int=lambda digits: _IntToken(digits),
            parse_float=_RejectFloat(),
            parse_constant=_reject_constant,
        )
    except RequestError:
        raise
    except json.JSONDecodeError as exc:
        raise RequestError(f"request body is not valid JSON: {exc.msg}") from None

    if not isinstance(payload, dict):
        raise RequestError("request body must be a JSON object")
    events = payload.get("events")
    if not isinstance(events, list):
        raise RequestError("request body must contain an 'events' array")
    if not (MIN_EVENTS <= len(events) <= MAX_EVENTS):
        raise RequestError(
            f"'events' must contain between {MIN_EVENTS} and {MAX_EVENTS}"
            f" items; got {len(events)}"
        )
    unknown = set(payload) - {"events", "correlations"}
    if unknown:
        raise RequestError(
            "unknown top-level field(s): " + ", ".join(sorted(unknown))
        )
    correlations = payload.get("correlations")
    if correlations is not None and not isinstance(correlations, list):
        raise RequestError("'correlations' must be an array")
    return events, correlations


# ---------------------------------------------------------------------------
# Correlation (onboard-clock segment) parsing and validation
# ---------------------------------------------------------------------------

def _corr_fail(index: int, reason: str, *,
               related: int | None = None) -> RequestError:
    return RequestError(
        reason, code="INVALID_CORRELATION",
        correlation_index=index, related_correlation_index=related,
    )


def _corr_int(value: object, field: str, index: int) -> int:
    if isinstance(value, _IntToken):
        return int(value)
    raise _corr_fail(
        index,
        f"{field} must be a JSON integer number (quoted strings, booleans"
        " and floating-point numbers are not accepted)",
    )


def _anchor_to_tai(seg: dict[str, object], index: int) -> int:
    """Normalize a correlation's UTC-or-GPS anchor to TAI nanoseconds."""
    has_utc = "utc" in seg
    gps_present = [f for f in GPS_FIELDS if f in seg]
    if has_utc and gps_present:
        raise _corr_fail(
            index, "correlation specifies both a 'utc' anchor and GPS"
            " fields; the anchor must be exactly one of UTC or GPS"
        )
    if not has_utc and not gps_present:
        raise _corr_fail(
            index, "correlation is missing its anchor: provide either a"
            " 'utc' string or GPS fields (gpsWeek, gpsSecondsInWeek[,"
            " gpsNanoseconds])"
        )
    try:
        if has_utc:
            return normalize_utc_event(seg["utc"]).tai_ns
        if "gpsWeek" not in seg:
            raise _corr_fail(index, "GPS anchor is missing 'gpsWeek'")
        if "gpsSecondsInWeek" not in seg:
            raise _corr_fail(
                index, "GPS anchor is missing 'gpsSecondsInWeek'"
            )
        week = _corr_int(seg["gpsWeek"], "gpsWeek", index)
        sow = _corr_int(
            seg["gpsSecondsInWeek"], "gpsSecondsInWeek", index
        )
        nanos = 0
        if "gpsNanoseconds" in seg:
            nanos = _corr_int(
                seg["gpsNanoseconds"], "gpsNanoseconds", index
            )
        return normalize_gps_event(week, sow, nanos).tai_ns
    except TimeConversionError as exc:
        raise _corr_fail(index, f"illegal correlation anchor: {exc}") from None


def _parse_correlation(item: object, index: int) -> tuple[object, TickSegment]:
    if not isinstance(item, dict):
        raise _corr_fail(index, "each correlation must be a JSON object")
    unknown = set(item) - CORRELATION_ALLOWED
    if unknown:
        raise _corr_fail(
            index, "unknown correlation field(s): "
            + ", ".join(sorted(unknown))
        )
    missing = [f for f in CORRELATION_REQUIRED if f not in item]
    if missing:
        raise _corr_fail(
            index, "correlation is missing required field(s): "
            + ", ".join(missing)
        )

    partition = _corr_int(item["clockPartition"], "clockPartition", index)
    if partition < 0:
        raise _corr_fail(
            index, f"clockPartition must be a non-negative integer,"
            f" got {partition}"
        )
    start = _corr_int(item["tickStart"], "tickStart", index)
    end = _corr_int(item["tickEnd"], "tickEnd", index)
    anchor_tick = _corr_int(item["anchorTick"], "anchorTick", index)
    num = _corr_int(
        item["nanosecondsPerTickNumerator"],
        "nanosecondsPerTickNumerator", index,
    )
    den = _corr_int(
        item["nanosecondsPerTickDenominator"],
        "nanosecondsPerTickDenominator", index,
    )

    if not start < end:
        raise _corr_fail(
            index, f"half-open tick range invalid: require tickStart <"
            f" tickEnd, got [{start}, {end})"
        )
    if not start <= anchor_tick < end:
        raise _corr_fail(
            index, f"anchorTick {anchor_tick} is outside the segment's"
            f" half-open range [{start}, {end})"
        )
    if num <= 0:
        raise _corr_fail(
            index, f"nanosecondsPerTickNumerator must be a positive"
            f" integer, got {num}"
        )
    if den <= 0:
        raise _corr_fail(
            index, f"nanosecondsPerTickDenominator must be a positive"
            f" integer, got {den}"
        )

    anchor_tai_ns = _anchor_to_tai(item, index)
    return partition, TickSegment(
        start=start, end=end, anchor_tick=anchor_tick,
        anchor_tai_ns=anchor_tai_ns, ns_per_tick_num=num,
        ns_per_tick_den=den,
    )


def _try_map(seg: TickSegment, tick: int) -> int | None:
    """Map a tick, returning None when the result is sub-nanosecond."""
    unscaled = (
        seg.anchor_tai_ns * seg.ns_per_tick_den
        + (tick - seg.anchor_tick) * seg.ns_per_tick_num
    )
    value, remainder = divmod(unscaled, seg.ns_per_tick_den)
    return None if remainder else value


def build_partitions(
    raw: object,
) -> dict[object, list[tuple[TickSegment, int]]]:
    """Parse every correlation and cross-validate same-partition segments.

    Maps clockPartition -> segments sorted by tickStart, each paired with
    its original index in the correlations array.  Same-partition segments
    must be non-overlapping and abutting, and abutting segments must map
    their shared boundary tick to the very same TAI instant.
    """
    if raw is None:
        return {}
    if not (MIN_CORRELATIONS <= len(raw) <= MAX_CORRELATIONS):
        raise RequestError(
            f"'correlations' must contain between {MIN_CORRELATIONS} and"
            f" {MAX_CORRELATIONS} segments; got {len(raw)}"
        )

    grouped: dict[object, list[tuple[TickSegment, int]]] = {}
    for index, item in enumerate(raw):
        partition, segment = _parse_correlation(item, index)
        grouped.setdefault(partition, []).append((segment, index))

    for partition, entries in grouped.items():
        entries.sort(key=lambda pair: (pair[0].start, pair[0].end))
        for (seg_a, idx_a), (seg_b, idx_b) in zip(entries, entries[1:]):
            if seg_b.start < seg_a.end:
                raise _corr_fail(
                    idx_a,
                    f"segments {idx_a} and {idx_b} of clockPartition"
                    f" {partition} overlap: [{seg_a.start}, {seg_a.end})"
                    f" and [{seg_b.start}, {seg_b.end})",
                    related=idx_b,
                )
            if seg_b.start != seg_a.end:
                raise _corr_fail(
                    idx_b,
                    f"segment {idx_b} of clockPartition {partition} does"
                    f" not continue segment {idx_a}: previous segment ends"
                    f" at {seg_a.end} but this one starts at"
                    f" {seg_b.start} (boundary gap or overshoot)",
                    related=idx_a,
                )
            # Abutting: the shared boundary tick is seg_a.end ==
            # seg_b.start; both scales must resolve it to the same TAI
            # instant, and each to an integral number of nanoseconds.
            at_a = _try_map(seg_a, seg_a.end)
            at_b = _try_map(seg_b, seg_b.start)
            if at_a is None or at_b is None or at_a != at_b:
                detail = (
                    "one side is sub-nanosecond at the boundary"
                    if at_a is None or at_b is None
                    else f"they map to TAI {at_a} vs {at_b}"
                )
                raise _corr_fail(
                    idx_a,
                    f"segments {idx_a} and {idx_b} of clockPartition"
                    f" {partition} disagree at their shared boundary tick"
                    f" {seg_a.end}: {detail}",
                    related=idx_b,
                )
    return grouped


def _normalize_one(
    item: object,
    index: int,
    partitions: dict[object, list[tuple[TickSegment, int]]],
) -> dict[str, str]:
    if not isinstance(item, dict):
        raise RequestError(
            "each event must be a JSON object", event_index=index
        )
    if "id" not in item:
        raise RequestError("event is missing required 'id'", event_index=index)
    try:
        event_id = item["id"]
        _id_to_key(event_id)
    except RequestError as exc:
        raise RequestError(
            str(exc), event_index=index
        ) from None

    def fail(reason: str) -> None:
        raise RequestError(
            reason, code="INVALID_EVENT", event_index=index,
            event_id=str(event_id),
        )

    unknown = set(item) - ALLOWED_FIELDS
    if unknown:
        fail(f"unknown field(s): {', '.join(sorted(unknown))}")

    has_utc = "utc" in item
    gps_present = [f for f in GPS_FIELDS if f in item]
    has_gps = bool(gps_present)
    onboard_present = [f for f in ONBOARD_FIELDS if f in item]
    has_onboard = bool(onboard_present)
    kinds = int(has_utc) + int(has_gps) + int(has_onboard)
    if kinds > 1:
        fail("event mixes UTC, GPS and/or onboard-clock fields; choose"
             " exactly one kind")
    if kinds == 0:
        fail("event must contain either 'utc', GPS fields"
             " (gpsWeek, gpsSecondsInWeek[, gpsNanoseconds]) or onboard"
             " fields (clockPartition, onboardTick)")

    try:
        if has_utc:
            result = normalize_utc_event(item["utc"])
        elif has_gps:
            if "gpsWeek" not in item:
                fail("GPS event is missing 'gpsWeek'")
            if "gpsSecondsInWeek" not in item:
                fail("GPS event is missing 'gpsSecondsInWeek'")
            week = _gps_int(item["gpsWeek"], "gpsWeek")
            sow = _gps_int(item["gpsSecondsInWeek"], "gpsSecondsInWeek")
            nanos = 0
            if "gpsNanoseconds" in item:
                nanos = _gps_int(item["gpsNanoseconds"], "gpsNanoseconds")
            result = normalize_gps_event(week, sow, nanos)
        else:
            if "clockPartition" not in item:
                fail("onboard event is missing 'clockPartition'")
            if "onboardTick" not in item:
                fail("onboard event is missing 'onboardTick'")
            partition = _gps_int(item["clockPartition"], "clockPartition")
            tick = _gps_int(item["onboardTick"], "onboardTick")
            entries = partitions.get(partition)
            segment: TickSegment | None = None
            seg_index: int | None = None
            if entries:
                starts = [seg.start for seg, _ in entries]
                pos = bisect.bisect_right(starts, tick) - 1
                if pos >= 0:
                    candidate, candidate_index = entries[pos]
                    if candidate.contains(tick):
                        segment, seg_index = candidate, candidate_index
            if segment is None:
                fail(
                    f"onboard tick {tick} of clockPartition {partition} is"
                    " not covered by any correlation segment"
                )
            assert segment is not None and seg_index is not None
            try:
                result = normalize_onboard_event(segment, tick)
            except TimeConversionError as exc:
                # Locate both the event and the segment that rejected it;
                # raised past the generic fail() wrapper below on purpose.
                raise RequestError(
                    str(exc), code="INVALID_EVENT", event_index=index,
                    event_id=str(event_id),
                    correlation_index=seg_index,
                ) from None
    except RequestError as exc:
        if exc.event_index == index and exc.code == "INVALID_EVENT":
            raise  # already fully located (e.g. the onboard error above)
        fail(str(exc))
    except TimeConversionError as exc:
        fail(str(exc))

    body = result.to_response()
    body["id"] = str(event_id)
    return body  # type: ignore[return-value]


def normalize_batch(raw: bytes) -> list[dict[str, str]]:
    events, raw_correlations = _normalize_payload(raw)
    # Segments are fully validated before any event is normalized, so a
    # bad segment is reported as such (never as a downstream event error).
    partitions = build_partitions(raw_correlations)
    seen: set[str] = set()
    results: list[dict[str, str]] = []
    for index, item in enumerate(events):
        # Unique ids are checked before conversion so a duplicate id is
        # reported as such rather than as a conversion error.
        if isinstance(item, dict) and "id" in item:
            try:
                key = _id_to_key(item["id"])
            except RequestError:
                key = None  # type: ignore[assignment]
            if key is not None:
                if key in seen:
                    raise RequestError(
                        f"duplicate event id {key!r}; ids must be unique",
                        code="INVALID_EVENT",
                        event_index=index,
                        event_id=key,
                    )
                seen.add(key)
    # Nothing is returned until the whole batch validates, so no partial
    # results can ever leak out of an error response.
    for index, item in enumerate(events):
        results.append(_normalize_one(item, index, partitions))
    return results


class Handler(BaseHTTPRequestHandler):
    server_version = "DeepSpaceTime/1.0"
    # Every response below carries Content-Length, so keep-alive is safe.
    protocol_version = "HTTP/1.1"

    def _send_json(self, status: int, obj: object) -> None:
        data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path.split("?", 1)[0] == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if self.path.split("?", 1)[0] == "/":
            self._send_json(200, {
                "service": "deep-space-time-normalizer",
                "endpoint": "POST /api/times/normalize",
            })
            return
        self._send_json(404, {"error": {"code": "NOT_FOUND",
                                        "message": self.path}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/times/normalize":
            self._send_json(404, {"error": {"code": "NOT_FOUND",
                                            "message": self.path}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": {"code": "INVALID_REQUEST",
                                            "message": "bad Content-Length"}})
            return
        if length <= 0:
            self._send_json(400, {"error": {"code": "INVALID_REQUEST",
                                            "message": "empty request body"}})
            return
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": {"code": "PAYLOAD_TOO_LARGE",
                                            "message": "body too large"}})
            return
        raw = self.rfile.read(length)
        try:
            results = normalize_batch(raw)
        except RequestError as exc:
            err: dict[str, object] = {"code": exc.code, "message": exc.message}
            if exc.correlation_index is not None:
                err["correlationIndex"] = exc.correlation_index
            if exc.related_correlation_index is not None:
                err["relatedCorrelationIndex"] = \
                    exc.related_correlation_index
            if exc.event_index is not None:
                err["eventIndex"] = exc.event_index
            if exc.event_id is not None:
                err["eventId"] = exc.event_id
            self._send_json(exc.status, {"error": err})
            return
        self._send_json(200, {"results": results})

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), fmt % args)
        )


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    httpd = ThreadingHTTPServer((host, port), Handler)
    sys.stderr.write(f"time-normalizer listening on {host}:{port}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
