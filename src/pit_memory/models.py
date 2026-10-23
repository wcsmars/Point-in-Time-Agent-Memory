"""Typed, validated public interfaces shared by storage and evaluation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
import math
from typing import Literal


def iso_date(value: str) -> str:
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError(f"Expected an ISO YYYY-MM-DD date, got {value!r}")
    return value


def normalize_cik(value: str | int) -> str:
    raw = str(value)
    if not raw.isascii() or not raw.isdigit() or not 0 < int(raw) < 10**10:
        raise ValueError("CIK must be a positive integer of at most ten digits")
    return str(int(raw)).zfill(10)


@dataclass(frozen=True)
class Fact:
    cik: str
    concept: str
    unit: str
    value: float
    period_start: str
    period_end: str
    filed: str
    accession: str
    form: str
    available_on: str
    fact_id: str = ""
    source_url: str = ""
    source_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "cik", normalize_cik(self.cik))
        for key in ("period_start", "period_end", "filed", "available_on"):
            iso_date(getattr(self, key))
        if not self.period_start <= self.period_end <= self.filed < self.available_on:
            raise ValueError("Expected period_start <= period_end <= filed < available_on")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)) or not math.isfinite(self.value):
            raise ValueError("Fact value must be a finite number")
        object.__setattr__(self, "value", float(self.value))
        for key in ("concept", "unit", "accession", "form"):
            if not isinstance(getattr(self, key), str) or not getattr(self, key).strip():
                raise ValueError(f"Missing fact field {key}")
        # Source snapshot hashes belong in provenance rather than identity: an
        # unchanged fact in a later downloaded companyfacts file is not a new fact.
        identity = {k: v for k, v in asdict(self).items() if k not in {"fact_id", "source_url", "source_sha256"}}
        expected = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if self.fact_id and self.fact_id != expected:
            raise ValueError("fact_id does not match immutable fact contents")
        object.__setattr__(self, "fact_id", expected)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Query:
    query_id: str
    cik: str
    concept: str
    unit: str
    as_of: str
    period_end: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cik", normalize_cik(self.cik))
        iso_date(self.as_of)
        if self.period_end is not None:
            iso_date(self.period_end)
        if not all(isinstance(v, str) and v.strip() for v in (self.query_id, self.concept, self.unit)):
            raise ValueError("query_id, concept and unit must be nonempty")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Retrieval:
    query_id: str
    backend: str
    fact: Fact | None
    context: tuple[Fact, ...]
    status: Literal["known", "not_known", "ambiguous"] = "known"
    reason: str = ""

    @property
    def answer(self) -> Fact | None:
        return self.fact

    def to_dict(self) -> dict:
        return asdict(self)
