"""SEC companyfacts ingestion preserving every dated annual filing version.

Only annual duration facts are supported. Taxonomy-qualified tags
and units are kept separate; no unsupported tag equivalence is inferred.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .calendar import next_trading_session
from .models import Fact, iso_date, normalize_cik

ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"})


def _json_safe(value):
    """Copy of a raw row with non-finite numbers as text, so reports stay valid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


@dataclass
class ParseResult:
    facts: list[Fact]
    rejected: list[dict]
    source_sha256: str
    skipped: int = 0


def parse_companyfacts(
    payload: dict,
    *,
    calendar: Callable[[str], str] = next_trading_session,
    source_sha256: str | None = None,
) -> ParseResult:
    """Parse annual flows; retain original and later comparative/restated values.

    `source_sha256` should be the original file byte hash. If omitted, a hash
    of canonical JSON is used and is therefore NOT an exact-byte checksum.
    Valid out-of-scope quarterly/instant facts are counted as skipped. Malformed
    annual facts (including missing dates) are quarantined with their raw row.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("facts"), dict):
        raise ValueError("Expected SEC companyfacts object with a facts mapping")
    cik = normalize_cik(payload.get("cik", ""))
    if source_sha256 is None:
        source_sha256 = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    elif not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("source_sha256 must be a lowercase SHA-256 hex digest")
    result = ParseResult([], [], source_sha256)
    seen: set[str] = set()
    for taxonomy, concepts in payload["facts"].items():
        if not isinstance(concepts, dict):
            result.rejected.append({"taxonomy": taxonomy, "reason": "Concept mapping is malformed"})
            continue
        for tag, definition in concepts.items():
            concept = f"{taxonomy}:{tag}"
            if not isinstance(definition, dict) or not isinstance(definition.get("units"), dict):
                result.rejected.append({"concept": concept, "reason": "Unit mapping is malformed"})
                continue
            for unit, entries in definition["units"].items():
                if not isinstance(entries, list):
                    result.rejected.append({"concept": concept, "unit": unit, "reason": "Unit entries must be a list"})
                    continue
                for entry in entries:
                    try:
                        if not isinstance(entry, dict):
                            raise ValueError("Fact entry must be an object")
                        form = entry.get("form")
                        if not isinstance(form, str) or not form:
                            raise ValueError("Missing filing form")
                        if form not in ANNUAL_FORMS:
                            result.skipped += 1
                            continue
                        # Instant balance-sheet facts have no start; deliberately
                        # outside the annual-flow scope, not an inferred annual period.
                        if "start" not in entry:
                            result.skipped += 1
                            continue
                        start = iso_date(entry.get("start"))
                        end = iso_date(entry.get("end"))
                        filed = iso_date(entry.get("filed"))
                        duration = (date.fromisoformat(end) - date.fromisoformat(start)).days
                        if duration < 0:
                            raise ValueError("Fiscal period ends before it starts")
                        if not 330 <= duration <= 400:
                            result.skipped += 1
                            continue
                        val = entry.get("val")
                        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
                            raise ValueError("Fact value must be finite numeric data")
                        accession = entry.get("accn", "")
                        if not isinstance(accession, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
                            raise ValueError("Missing or malformed accession number")
                        if end > filed:
                            raise ValueError("Fiscal period ends after filing date")
                        available = calendar(filed)
                        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}-index.html"
                        fact = Fact(cik, concept, unit, val, start, end, filed, accession, form, available,
                                    source_url=url, source_sha256=source_sha256)
                        if fact.fact_id not in seen:
                            result.facts.append(fact)
                            seen.add(fact.fact_id)
                    except (ValueError, TypeError, OverflowError) as exc:
                        result.rejected.append({"concept": concept, "unit": unit, "reason": str(exc),
                                                "row": _json_safe(entry)})
    return result


class SecClient:
    """Small rate-limited SEC client with immutable raw snapshots.

    User agent must identify a real contact supplied by the user; the package
    never invents contact details. At most five requests/second per instance.
    Network downloads only occur when a CLI caller explicitly requests fetch.
    """
    def __init__(self, cache_dir: str | Path, user_agent: str | None = None,
                 min_interval: float = 0.2, retries: int = 3, timeout: float = 30.0):
        self.cache_dir = Path(cache_dir)
        self.user_agent = user_agent or os.environ.get("SEC_USER_AGENT", "")
        if not isinstance(self.user_agent, str) or not re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.user_agent) or len(self.user_agent.split()) < 2:
            raise ValueError("Set SEC_USER_AGENT to a descriptive application/contact name and your real email address")
        if min_interval < 0.2 or retries < 0 or retries > 8 or timeout <= 0:
            raise ValueError("Use min_interval>=0.2 seconds, retries 0..8, and positive timeout")
        self.min_interval, self.retries, self.timeout = min_interval, retries, timeout
        self._last_request = 0.0

    def download_companyfacts(self, cik: str | int, refresh: bool = False) -> Path:
        cik = normalize_cik(cik)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        pointer = self.cache_dir / f"CIK{cik}.latest.json"
        if pointer.is_file() and not refresh:
            cached = self._cached_snapshot(cik, pointer)
            if cached is not None:
                return cached
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            remaining = self.min_interval - (time.monotonic() - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()
            req = Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
            try:
                with urlopen(req, timeout=self.timeout) as response:
                    raw = response.read()
                payload = json.loads(raw)
                if normalize_cik(payload.get("cik", "")) != cik or not isinstance(payload.get("facts"), dict):
                    raise ValueError("SEC response does not match requested companyfacts CIK")
                sha = hashlib.sha256(raw).hexdigest()
                destination = self.cache_dir / f"CIK{cik}.{sha}.json"
                if not self._intact(destination, sha):
                    # Write beside the target and rename, so an interrupted write
                    # never leaves a truncated file under the snapshot's name. A
                    # file whose bytes do not match its digest name is replaced.
                    partial = destination.with_name(f"{destination.name}.{os.getpid()}.part")
                    partial.write_bytes(raw)
                    os.replace(partial, destination)
                temp = pointer.with_suffix(".tmp")
                temp.write_text(json.dumps({"cik": cik, "sha256": sha, "url": url}, indent=2) + "\n")
                temp.replace(pointer)
                return destination
            except HTTPError as exc:
                last_error = exc
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                delay = min(float(retry_after), 30.0) if retry_after.isdigit() else min(2**attempt, 16)
            except (URLError, TimeoutError, ConnectionError, http.client.HTTPException,
                    json.JSONDecodeError) as exc:
                # Includes dropped connections and truncated bodies.
                last_error = exc
                delay = min(2**attempt, 16)
            if attempt < self.retries:
                time.sleep(delay)
        assert last_error is not None
        raise RuntimeError(f"SEC download for CIK {cik} failed after {self.retries + 1} attempt(s): "
                           f"{type(last_error).__name__}: {last_error}") from last_error

    @staticmethod
    def _intact(path: Path, sha: str) -> bool:
        return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == sha

    def _cached_snapshot(self, cik: str, pointer: Path) -> Path | None:
        """The cached file the pointer names, or None if anything is missing or damaged."""
        try:
            metadata = json.loads(pointer.read_text())
        except (OSError, ValueError):
            return None
        sha = metadata.get("sha256") if isinstance(metadata, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            return None
        cached = self.cache_dir / f"CIK{cik}.{sha}.json"
        return cached if self._intact(cached, sha) else None

    def fetch_companyfacts(self, cik: str | int, refresh: bool = False) -> dict:
        return json.loads(self.download_companyfacts(cik, refresh=refresh).read_bytes())
