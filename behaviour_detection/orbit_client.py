from __future__ import annotations

import logging
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx

PAYOUT_SELECTOR = ".payouts-block .payout"
_LOCAL_HTTP_HOSTS = {
    "localhost", "127.0.0.1", "::1", "host.containers.internal", "host.docker.internal",
}
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_CURSOR = re.compile(r"^\d{1,19}$")
_PAYOUT = re.compile(r"^\s*((?:\d{1,3}(?:,\d{3})+|\d{1,7}))(?:\.(\d{1,24}))?\s*[xX×]\s*$")
log = logging.getLogger("behaviour.orbit")


class OrbitContractError(RuntimeError):
    def __init__(self, code: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class ParsedPayout:
    item_index: int
    multiplier_raw: str
    multiplier: Decimal
    visible: bool
    tag_name: str | None = None


@dataclass(frozen=True)
class ParsedReading:
    reading_id: str
    checked_at: datetime
    items: tuple[ParsedPayout, ...]
    match_count: int
    returned_count: int
    truncated: bool


def parse_reading(raw: Any) -> ParsedReading:
    if not isinstance(raw, dict):
        raise OrbitContractError("INVALID_READING")
    reading_id = str(raw.get("id", ""))
    checked_at_raw = raw.get("checkedAt")
    count = raw.get("count")
    items = raw.get("items")
    if (not _CURSOR.fullmatch(reading_id) or reading_id == "0"
            or not isinstance(checked_at_raw, str) or not isinstance(count, int) or count < 0
            or not isinstance(items, list) or len(items) > count):
        raise OrbitContractError("INVALID_READING")
    try:
        checked_at = datetime.fromisoformat(checked_at_raw.replace("Z", "+00:00"))
    except ValueError:
        raise OrbitContractError("INVALID_CHECKED_AT") from None
    if checked_at.tzinfo is None:
        raise OrbitContractError("NAIVE_CHECKED_AT")
    parsed = []
    for index, item in enumerate(items):
        if (not isinstance(item, dict) or item.get("index") != index
                or not isinstance(item.get("visible"), bool) or not isinstance(item.get("value"), str)):
            raise OrbitContractError("INVALID_READING_ITEM")
        match = _PAYOUT.fullmatch(item["value"])
        if not match:
            raise OrbitContractError("INVALID_MULTIPLIER")
        integer = match.group(1).replace(",", "")
        decimals = match.group(2) or ""
        multiplier_raw = integer + (f".{decimals}" if decimals else "")
        try:
            multiplier = Decimal(multiplier_raw)
        except InvalidOperation:
            raise OrbitContractError("INVALID_MULTIPLIER") from None
        if not multiplier.is_finite() or multiplier < Decimal("1"):
            raise OrbitContractError("INVALID_MULTIPLIER")
        tag_name = item.get("tagName")
        if not isinstance(tag_name, str):
            tag_name = None
        parsed.append(ParsedPayout(index, multiplier_raw, multiplier, item["visible"], tag_name))
    return ParsedReading(reading_id, checked_at.astimezone(timezone.utc), tuple(parsed),
                         count, len(items), count > len(items))


def select_watch(response: Any, preferred_watch_id: str | None = None, *,
                 allow_fallback: bool = True) -> dict:
    if not isinstance(response, dict) or not isinstance(response.get("watches"), list):
        raise OrbitContractError("INVALID_WATCH_LIST")
    watches = [item for item in response["watches"] if isinstance(item, dict)]
    preferred = None
    if preferred_watch_id:
        preferred = next((item for item in watches if item.get("id") == preferred_watch_id), None)
        if (preferred is not None and
                (not isinstance(preferred.get("selector"), str)
                 or preferred["selector"].strip() != PAYOUT_SELECTOR)):
            raise OrbitContractError("SAVED_WATCH_SELECTOR_MISMATCH")
    matches = [item for item in watches
               if isinstance(item.get("selector"), str)
               and item["selector"].strip() == PAYOUT_SELECTOR]
    if not matches:
        if preferred_watch_id and preferred is None:
            raise OrbitContractError("SAVED_WATCH_NOT_FOUND")
        raise OrbitContractError("AVIATOR_PAYOUT_WATCH_UNAVAILABLE")

    # A pinned live watch remains preferred. Otherwise, callers without a
    # durable cursor can choose the first running payout watch from Orbit's
    # list. Callers with an established cursor disable fallback to preserve
    # source identity and history continuity.
    watch = preferred if preferred in matches and preferred.get("status") == "running" else None
    if watch is None and allow_fallback:
        watch = next((item for item in matches if item.get("status") == "running"), None)
    if watch is None:
        raise OrbitContractError("WATCH_NOT_RUNNING_OR_INVALID")
    if (not isinstance(watch.get("id"), str) or not watch["id"]
            or not isinstance(watch.get("selector"), str)
            or watch["selector"].strip() != PAYOUT_SELECTOR):
        raise OrbitContractError("WATCH_NOT_RUNNING_OR_INVALID")
    return watch


class OrbitDataWatchClient:
    """Independent read-only client for the Orbit Data Watch contract in this repo."""

    def __init__(self, base_url: str, api_key: str, *, connect_timeout: float = 5,
                 request_timeout: float = 15, attempts: int = 3):
        parsed = httpx.URL(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.host
                or parsed.userinfo or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.host not in _LOCAL_HTTP_HOSTS)):
            raise OrbitContractError("INVALID_BASE_URL")
        if not isinstance(api_key, str) or not api_key.strip() or "\n" in api_key or "\r" in api_key:
            raise OrbitContractError("API_KEY_REQUIRED")
        self.base_url = str(parsed).rstrip("/")
        self._api_key = api_key.strip()
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.attempts = max(1, min(10, attempts))
        self._client = httpx.Client(timeout=httpx.Timeout(request_timeout, connect=connect_timeout),
                                    headers={"Authorization": f"Bearer {self._api_key}",
                                             "Accept": "application/json"})

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str) -> dict:
        for attempt in range(self.attempts):
            try:
                request = self._client.build_request("GET", f"{self.base_url}{path}")
                response = self._client.send(request, stream=True)
                try:
                    if response.status_code != 200:
                        retryable = response.status_code in (408, 429) or response.status_code >= 500
                        if retryable and attempt + 1 < self.attempts:
                            time.sleep(min(8, 0.25 * (2 ** attempt)))
                            continue
                        raise OrbitContractError("HTTP_ERROR", status=response.status_code, retryable=retryable)
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise OrbitContractError("RESPONSE_TOO_LARGE")
                        content.extend(chunk)
                finally:
                    response.close()
                try:
                    payload = json.loads(content)
                except (ValueError, UnicodeDecodeError):
                    raise OrbitContractError("INVALID_JSON") from None
                if not isinstance(payload, dict):
                    raise OrbitContractError("INVALID_RESPONSE_SHAPE")
                return payload
            except httpx.TimeoutException:
                if attempt + 1 >= self.attempts:
                    raise OrbitContractError("REQUEST_TIMEOUT", retryable=True) from None
            except httpx.HTTPError:
                if attempt + 1 >= self.attempts:
                    raise OrbitContractError("NETWORK_ERROR", retryable=True) from None
            time.sleep(min(8, 0.25 * (2 ** attempt)))
        raise OrbitContractError("NETWORK_ERROR", retryable=True)

    def list_watches(self) -> dict:
        return self._get("/api/data-watches")

    def latest(self, watch_id: str) -> dict:
        return self._get(f"/api/data-watches/{quote(watch_id, safe='')}/latest")

    def history(self, watch_id: str, after: str, limit: int = 100) -> dict:
        if not _CURSOR.fullmatch(after) or int(after) > 9223372036854775807:
            raise OrbitContractError("INVALID_CURSOR")
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise OrbitContractError("INVALID_PAGE_SIZE")
        return self._get(f"/api/data-watches/{quote(watch_id, safe='')}/history?after={after}&limit={limit}")


def validate_latest_snapshot(snapshot: Any, watch: dict) -> tuple[str, dict | None]:
    if not isinstance(snapshot, dict):
        raise OrbitContractError("INVALID_LATEST_SNAPSHOT")
    snapshot_watch = snapshot.get("watch")
    if not isinstance(snapshot_watch, dict):
        raise OrbitContractError("INVALID_LATEST_WATCH")
    if snapshot_watch.get("id") != watch.get("id"):
        raise OrbitContractError("LATEST_WATCH_ID_MISMATCH")
    if snapshot_watch.get("selector") != PAYOUT_SELECTOR:
        raise OrbitContractError("LATEST_SELECTOR_MISMATCH")
    cursor = str(snapshot.get("cursor", ""))
    latest = snapshot.get("latest")
    if not _CURSOR.fullmatch(cursor) or int(cursor) > 9223372036854775807:
        raise OrbitContractError("INVALID_LATEST_CURSOR")
    if latest is None:
        if cursor != "0":
            raise OrbitContractError("LATEST_CURSOR_WITHOUT_READING")
        return cursor, None
    parsed = parse_reading(latest)
    if parsed.reading_id != cursor:
        raise OrbitContractError("LATEST_READING_CURSOR_MISMATCH")
    return cursor, latest


def validate_history_page(page: Any, watch_id: str, after: str) -> dict:
    if (not isinstance(page, dict) or page.get("watchId") != watch_id
            or page.get("selector") != PAYOUT_SELECTOR
            or not isinstance(page.get("readings"), list)
            or not _CURSOR.fullmatch(str(page.get("nextCursor", "")))
            or not isinstance(page.get("hasMore"), bool)):
        raise OrbitContractError("INVALID_HISTORY_PAGE")
    previous = int(after)
    if previous > 9223372036854775807 or int(page["nextCursor"]) > 9223372036854775807:
        raise OrbitContractError("INVALID_HISTORY_CURSOR")
    for reading in page["readings"]:
        reading_id = str(reading.get("id", "")) if isinstance(reading, dict) else ""
        if not _CURSOR.fullmatch(reading_id) or int(reading_id) <= previous:
            raise OrbitContractError("INVALID_HISTORY_ORDER")
        previous = int(reading_id)
    if int(page["nextCursor"]) != previous or (page["hasMore"] and not page["readings"]):
        raise OrbitContractError("INVALID_HISTORY_CURSOR")
    return page
