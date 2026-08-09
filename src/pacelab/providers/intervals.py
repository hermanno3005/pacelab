"""intervals.icu Provider — list activities and download original files (ADR-0008).

Knows the network and credentials, never file formats. Downloads land in the account-keyed
FIT cache, where the existing FitAdapter/GPX pipeline takes over.
"""

import base64
import gzip
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from pacelab.account import Account
from pacelab.providers.http import Http

log = logging.getLogger(__name__)

_BASE = "https://intervals.icu/api/v1"
_GZIP_MAGIC = b"\x1f\x8b"


class RateLimited(RuntimeError):
    """intervals.icu returned 429 — stop the sync and retry after the given delay.

    Raised (not folded into skip-and-warn) so a sync aborts instead of hammering the
    remaining activities; see docs/research/activity-api-ingestion.md, rate limits.
    """

    def __init__(self, retry_after_s: int | None):
        super().__init__(
            "intervals.icu rate limit hit (HTTP 429)"
            + (f" — retry after {retry_after_s}s" if retry_after_s else "")
        )
        self.retry_after_s = retry_after_s


def _retry_after(resp) -> int | None:
    value = resp.headers.get("Retry-After")
    return int(value) if value and value.isdigit() else None


def _sniff_extension(raw: bytes) -> str:
    """Detect the activity file format from its bytes (original may be FIT/GPX/TCX)."""
    if len(raw) >= 12 and raw[8:12] == b".FIT":
        return "fit"
    head = raw[:512].lstrip()
    if b"<gpx" in head:
        return "gpx"
    if b"TrainingCenterDatabase" in head:
        return "tcx"
    return "fit"  # default: COROS originals are FIT


@dataclass(frozen=True)
class ActivityRef:
    """A remote activity's identity from a listing, before its file is downloaded."""

    id: str
    start_date: str | None
    type: str | None
    name: str | None


class IntervalsProvider:
    def __init__(self, account: Account, http: Http, cache_dir: Path):
        self._account = account
        self._http = http
        self._cache_dir = Path(cache_dir)
        token = base64.b64encode(f"API_KEY:{account.api_key}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}"}

    def list_activities(self, oldest: str, newest: str) -> list[ActivityRef]:
        url = f"{_BASE}/athlete/{self._account.athlete_id}/activities?oldest={oldest}&newest={newest}"
        resp = self._http.get(url, self._headers)
        if resp.status == 429:
            raise RateLimited(_retry_after(resp))
        if resp.status != 200:
            raise RuntimeError(f"intervals.icu activity listing failed (HTTP {resp.status})")
        activities = json.loads(resp.content)
        return [
            ActivityRef(
                id=a["id"],
                start_date=a.get("start_date_local"),
                type=a.get("type"),
                name=a.get("name"),
            )
            for a in activities
        ]

    def _check(self, resp, what: str):
        if resp.status == 429:
            raise RateLimited(_retry_after(resp))
        if resp.status != 200:
            raise RuntimeError(f"intervals.icu {what} failed (HTTP {resp.status})")
        return resp

    def fetch_description(self, activity_id: str) -> str | None:
        """The activity's current description — read before splicing (never clobber)."""
        resp = self._check(self._http.get(f"{_BASE}/activity/{activity_id}", self._headers),
                           f"read of {activity_id}")
        return json.loads(resp.content).get("description")

    def update_description(self, activity_id: str, text: str) -> None:
        """Write the merged description back; intervals.icu's bridge carries it to Strava."""
        body = json.dumps({"description": text}).encode()
        headers = {**self._headers, "Content-Type": "application/json"}
        self._check(self._http.put(f"{_BASE}/activity/{activity_id}", headers, body),
                    f"description update of {activity_id}")

    def download(self, activity_id: str) -> Path | None:
        """Download an activity's original file into the account-keyed cache.

        Returns the cached path, or ``None`` — skip-and-log at INFO — when no original is
        available, e.g. Strava-synced activities intervals.icu can't serve (ADR-0008).

        Originals are immutable, so a cache hit never refetches — re-analysis passes
        (model bumps, ADR-0012 finalization) run network-free.
        """
        cached = sorted((self._cache_dir / self._account.storage_id).glob(f"{activity_id}.*"))
        if cached:
            return cached[0]
        url = f"{_BASE}/activity/{activity_id}/file"
        resp = self._http.get(url, self._headers)
        if resp.status == 429:
            raise RateLimited(_retry_after(resp))
        if resp.status >= 500:
            raise RuntimeError(f"intervals.icu download failed for {activity_id} (HTTP {resp.status})")
        if resp.status != 200:
            # 4xx: intervals.icu has no original for this activity (e.g. Strava-synced).
            # INFO, not WARNING: it recurs on every listing pass and is an expected skip,
            # so flagging it would make a steady state look like a fault (ADR-0017).
            log.info("no original file for %s (HTTP %s) — skipping", activity_id, resp.status)
            return None
        raw = resp.content
        if raw[:2] == _GZIP_MAGIC:
            raw = gzip.decompress(raw)
        dest = self._cache_dir / self._account.storage_id / f"{activity_id}.{_sniff_extension(raw)}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        return dest
