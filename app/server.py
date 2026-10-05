"""HTTP service for batch time-scale normalization.

Stdlib only.  JSON numbers are parsed with ``parse_int=str`` so that no
numeric value ever passes through a Python float; fractional/non-integer
JSON numbers are rejected outright.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .timecore import (
    TimeConversionError,
    normalize_gps_event,
    normalize_utc_event,
)

MAX_BODY_BYTES = 1 << 20  # 1 MiB is far more than 200 small events
MAX_EVENTS = 200
MIN_EVENTS = 1

GPS_FIELDS = ("gpsWeek", "gpsSecondsInWeek", "gpsNanoseconds")
ALLOWED_FIELDS = {"id", "utc", *GPS_FIELDS}


class RequestError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "INVALID_REQUEST",
        status: int = 400,
        event_index: int | None = None,
        event_id: object | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.event_index = event_index
        self.event_id = event_id


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


def _normalize_payload(raw: bytes) -> list[dict[str, object]]:
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
    if len(payload) != 1:
        raise RequestError("request body may only contain the 'events' field")
    return events


def _normalize_one(item: object, index: int) -> dict[str, str]:
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
    if has_utc and has_gps:
        fail("event specifies both 'utc' and GPS fields; choose exactly one")
    if not has_utc and not has_gps:
        fail("event must contain either 'utc' or GPS fields"
             " (gpsWeek, gpsSecondsInWeek[, gpsNanoseconds])")

    try:
        if has_utc:
            result = normalize_utc_event(item["utc"])
        else:
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
    except TimeConversionError as exc:
        fail(str(exc))
    except RequestError as exc:
        fail(str(exc))

    body = result.to_response()
    body["id"] = str(event_id)
    return body  # type: ignore[return-value]


def normalize_batch(raw: bytes) -> list[dict[str, str]]:
    events = _normalize_payload(raw)
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
        results.append(_normalize_one(item, index))
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
