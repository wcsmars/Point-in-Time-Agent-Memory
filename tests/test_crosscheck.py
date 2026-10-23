"""Randomized cross-check of every point-in-time implementation against a brute-force rule.

The reference below is written separately from the package: it scans all rows
and applies the rule from docs/evaluation.md literally. DuckDB SQL, the
in-memory backend and ``select_latest`` must agree with it on status, value,
fiscal period and filing date for every query.
"""
import importlib.util
import random
from datetime import date, timedelta
from pathlib import Path

import pytest

from pit_memory.backends import BackendIndex
from pit_memory.models import Fact, Query
from pit_memory.store import DuckDBStore, select_latest

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample-data" / "sec-facts-sample.jsonl"
needs_duckdb = pytest.mark.skipif(importlib.util.find_spec("duckdb") is None, reason="DuckDB not installed")


def reference(rows, cik, concept, unit, as_of, period_end):
    eligible = [r for r in rows if r.cik == cik and r.concept == concept and r.unit == unit
                and r.available_on <= as_of and r.period_end <= as_of
                and (period_end is None or r.period_end == period_end)]
    if not eligible:
        return ("not_known", None, None, None)
    newest_end = max(r.period_end for r in eligible)
    newest_filed = max(r.filed for r in eligible if r.period_end == newest_end)
    winners = [r for r in eligible if r.period_end == newest_end and r.filed == newest_filed]
    if len({(r.value, r.period_start) for r in winners}) > 1:
        return ("ambiguous", None, newest_end, newest_filed)
    return ("known", winners[0].value, newest_end, newest_filed)


def outcome(result):
    if result.status == "known":
        return ("known", result.fact.value, result.fact.period_end, result.fact.filed)
    if result.status == "not_known":
        return ("not_known", None, None, None)
    # Ambiguous: the context may also list other retrieved rows; read the newest period.
    end = max(f.period_end for f in result.context)
    filed = max(f.filed for f in result.context if f.period_end == end)
    winners = [f for f in result.context if (f.period_end, f.filed) == (end, filed)]
    assert len({(f.value, f.period_start) for f in winners}) > 1, "ambiguity must show the conflict"
    return ("ambiguous", None, end, filed)


def stress_corpus(seed=11, issuers=12):
    """Restatements, same-day conflicts, shifted starts and repeated comparatives."""
    rng = random.Random(seed)
    rows = []
    for issuer in range(1, issuers + 1):
        for year in range(2012, 2020):
            end = date(year, 12, 31)
            value = float(rng.randint(50, 150))
            for n in range(rng.randint(1, 4)):
                filed = end + timedelta(days=rng.randint(20, 80) + 365 * n + rng.choice([0, 0, 1]))
                new_value = value if rng.random() < 0.7 else float(rng.randint(50, 150))
                start = date(year, 1, 1) if rng.random() < 0.9 else date(year - 1, 12, 31)
                available = filed + timedelta(days=rng.choice([1, 1, 3, 4]))
                rows.append(Fact(str(issuer), "us-gaap:Revenues", "USD", new_value, start.isoformat(),
                                 end.isoformat(), filed.isoformat(), f"{issuer}-{year}-{n}", "10-K",
                                 available.isoformat()))
                if rng.random() < 0.08:  # a conflicting row on the same filing date
                    rows.append(Fact(str(issuer), "us-gaap:Revenues", "USD", new_value + 1, start.isoformat(),
                                     end.isoformat(), filed.isoformat(), f"{issuer}-{year}-{n}b", "10-K/A",
                                     available.isoformat()))
    return rows


def random_queries(rows, count, seed):
    rng = random.Random(seed)
    scopes = sorted({(r.cik, r.concept, r.unit) for r in rows})
    by_scope = {scope: [r for r in rows if (r.cik, r.concept, r.unit) == scope] for scope in scopes}
    for n in range(count):
        scope = rng.choice(scopes)
        anchor = rng.choice(by_scope[scope])
        as_of = rng.choice([
            anchor.filed, anchor.available_on,
            (date.fromisoformat(anchor.available_on) - timedelta(days=1)).isoformat(),
            anchor.period_end,
            (date(2010, 1, 1) + timedelta(days=rng.randint(0, 6000))).isoformat(),
        ])
        period_end = rng.choice([None, anchor.period_end, rng.choice(by_scope[scope]).period_end])
        yield Query(f"x{n}", *scope, as_of, period_end)


def check(rows, queries, store):
    index = BackendIndex(rows)
    checked = ambiguous = 0
    for query in queries:
        expected = reference(rows, query.cik, query.concept, query.unit, query.as_of, query.period_end)
        for result in (store.retrieve(query), index.retrieve(query, "bitemporal"), select_latest(query, rows)):
            assert outcome(result) == expected, (query, result.backend)
            assert all(f.available_on <= query.as_of for f in result.context)
        checked += 1
        ambiguous += expected[0] == "ambiguous"
    return checked, ambiguous


@needs_duckdb
def test_stress_corpus_all_implementations_agree():
    rows = stress_corpus()
    with DuckDBStore() as store:
        store.append(rows)
        checked, ambiguous = check(rows, random_queries(rows, 3000, seed=5), store)
    assert checked == 3000 and ambiguous > 20


@needs_duckdb
def test_real_sec_sample_all_implementations_agree():
    if not SAMPLE.is_file():
        pytest.skip("sample data not present")
    from pit_memory.study import facts_from_jsonl
    rows = facts_from_jsonl(SAMPLE)
    with DuckDBStore() as store:
        store.append(rows)
        checked, _ = check(rows, random_queries(rows, 3000, seed=9), store)
    assert checked == 3000
