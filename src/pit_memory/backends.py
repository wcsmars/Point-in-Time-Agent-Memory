"""Deliberately naive baselines and an auditable temporal retriever.

The vector baseline is a deterministic signed hashing vector space, not a
semantic embedding model. All backends share explicit entity/tag/unit scoping.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from typing import Iterable

from .models import Fact, Query, Retrieval

BACKENDS = ("vector", "overwrite", "period_end", "bitemporal")


def _scope(fact: Fact, query: Query) -> bool:
    return (fact.cik == query.cik and fact.concept == query.concept
            and fact.unit == query.unit
            and (query.period_end is None or fact.period_end == query.period_end))


def _vector(text: str, dimensions: int = 512) -> dict[int, float]:
    values: dict[int, float] = defaultdict(float)
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        number = int.from_bytes(digest, "little")
        values[number % dimensions] += 1.0 if number & (1 << 63) else -1.0
    norm = math.sqrt(sum(value * value for value in values.values()))
    return {key: value / norm for key, value in values.items()} if norm else {}


def _document(fact: Fact) -> str:
    return (f"annual reported {fact.cik} {fact.concept} {fact.unit} "
            f"period {fact.period_start} {fact.period_end} "
            f"filed {fact.filed} value {fact.value}")


def _selection(query: Query, backend: str, candidates: list[Fact],
               context: tuple[Fact, ...]) -> Retrieval:
    if not candidates:
        return Retrieval(query.query_id, backend, None, context, "not_known",
                         "No eligible fact in this backend.")
    end = max(fact.period_end for fact in candidates)
    current = [fact for fact in candidates if fact.period_end == end]
    filed = max(fact.filed for fact in current)
    latest = [fact for fact in current if fact.filed == filed]
    if len({(fact.value, fact.period_start) for fact in latest}) != 1:
        # Expose every conflicting row; top-k must never hide ambiguity.
        merged = {fact.fact_id: fact for fact in (*context, *latest)}
        return Retrieval(query.query_id, backend, None, tuple(merged.values()),
                         "ambiguous", "Conflicting facts share the latest filing date.")
    chosen = min(latest, key=lambda fact: fact.fact_id)
    if chosen.fact_id not in {fact.fact_id for fact in context}:
        context = (chosen, *context)
    return Retrieval(query.query_id, backend, chosen, context)


class BackendIndex:
    """Build frozen snapshots once so overwritten rows cannot be recovered."""

    def __init__(self, facts: Iterable[Fact]):
        self.facts = tuple(facts)
        self.vectors = {fact.fact_id: _vector(_document(fact)) for fact in self.facts}
        snapshot: dict[tuple, list[Fact]] = {}
        for fact in self.facts:
            key = (fact.cik, fact.concept, fact.unit, fact.period_start, fact.period_end)
            rows = snapshot.get(key, [])
            if not rows or fact.filed > rows[0].filed:
                snapshot[key] = [fact]
            elif fact.filed == rows[0].filed:
                snapshot[key].append(fact)
        self.overwritten = tuple(fact for rows in snapshot.values() for fact in rows)
        self.scoped_facts: dict[tuple[str, str, str], list[Fact]] = defaultdict(list)
        self.scoped_overwritten: dict[tuple[str, str, str], list[Fact]] = defaultdict(list)
        for fact in self.facts:
            self.scoped_facts[(fact.cik, fact.concept, fact.unit)].append(fact)
        for fact in self.overwritten:
            self.scoped_overwritten[(fact.cik, fact.concept, fact.unit)].append(fact)

    def retrieve(self, query: Query, backend: str, top_k: int = 5) -> Retrieval:
        if backend not in BACKENDS:
            raise ValueError(f"backend must be one of {BACKENDS}")
        if top_k < 1:
            raise ValueError("top_k must be positive")
        groups = self.scoped_overwritten if backend == "overwrite" else self.scoped_facts
        source = groups.get((query.cik, query.concept, query.unit), ())
        candidates = [fact for fact in source
                      if query.period_end is None or fact.period_end == query.period_end]
        if backend == "vector":
            query_vector = _vector(
                f"annual reported {query.cik} {query.concept} {query.unit} "
                f"period {query.period_end or ''} as of {query.as_of}")
            def score(fact: Fact) -> float:
                vector = self.vectors[fact.fact_id]
                return sum(value * vector.get(key, 0.0)
                           for key, value in query_vector.items())
            # Deterministic tie order, no date exclusion of any kind.
            candidates.sort(key=lambda fact: (score(fact), fact.filed,
                                               fact.period_end, fact.fact_id), reverse=True)
            context = tuple(candidates[:top_k])
            return Retrieval(query.query_id, backend, context[0] if context else None,
                             context, "known" if context else "not_known",
                             "Signed hash vectors; no temporal filter.")
        candidates = [fact for fact in candidates if fact.period_end <= query.as_of]
        if backend == "bitemporal":
            candidates = [fact for fact in candidates if fact.available_on <= query.as_of]
        candidates.sort(key=lambda fact: (fact.period_end, fact.filed, fact.fact_id), reverse=True)
        return _selection(query, backend, candidates, tuple(candidates[:top_k]))


def retrieve(facts: Iterable[Fact], query: Query, backend: str = "bitemporal",
             top_k: int = 5) -> Retrieval:
    return BackendIndex(facts).retrieve(query, backend, top_k)
