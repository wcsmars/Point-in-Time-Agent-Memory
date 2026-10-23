"""Deterministic boundary-query generation and evidence-based leakage scoring."""
from __future__ import annotations

import csv
import gzip
import json
import math
import random
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable

from .backends import BACKENDS, BackendIndex
from .models import Fact, Query, Retrieval


@dataclass(frozen=True)
class EvaluationCase:
    query: Query
    expected_value: float | None
    expected_fact_id: str | None = None
    expected_status: str = "known"
    category: str = "boundary"
    expected_filed: str | None = None
    expected_period_end: str | None = None

    def __post_init__(self):
        if self.expected_status not in {"known", "not_known", "ambiguous"}:
            raise ValueError("invalid expected_status")
        if self.expected_status == "known" and self.expected_value is None:
            raise ValueError("known cases require an expected value")
        if self.expected_value is not None and not math.isfinite(self.expected_value):
            raise ValueError("expected_value must be finite")
        if self.expected_status != "known" and self.expected_value is not None:
            raise ValueError("abstention cases must not have an expected value")


def _oracle(facts: tuple[Fact, ...], query: Query, category: str) -> EvaluationCase:
    """Small independent reference scan; never calls a backend or DuckDB."""
    eligible = []
    for fact in facts:
        if (fact.cik, fact.concept, fact.unit) != (query.cik, query.concept, query.unit):
            continue
        if fact.available_on > query.as_of or fact.period_end > query.as_of:
            continue
        if query.period_end is not None and query.period_end != fact.period_end:
            continue
        eligible.append(fact)
    if not eligible:
        return EvaluationCase(query, None, expected_status="not_known", category=category)
    period = sorted({fact.period_end for fact in eligible})[-1]
    filed = sorted({fact.filed for fact in eligible if fact.period_end == period})[-1]
    winners = [fact for fact in eligible if fact.period_end == period and fact.filed == filed]
    if len({(fact.value, fact.period_start) for fact in winners}) > 1:
        return EvaluationCase(query, None, expected_status="ambiguous", category=category)
    chosen = min(winners, key=lambda fact: fact.fact_id)
    return EvaluationCase(query, chosen.value, chosen.fact_id, "known", category,
                          chosen.filed, chosen.period_end)


def generate_cases(facts: Iterable[Fact]) -> list[EvaluationCase]:
    """Generate exact-period and latest-annual cases around every known boundary.

    Results measure a boundary-enriched test distribution, not the prevalence of
    leakage in natural user traffic. Historical SEC snapshot coverage limits the
    oracle: unknown means absent from this supplied dataset.
    """
    facts = tuple(facts)
    scopes: dict[tuple[str, str, str], list[Fact]] = defaultdict(list)
    for fact in facts:
        scopes[(fact.cik, fact.concept, fact.unit)].append(fact)
    cases = []
    seen = set()
    for fact in sorted(facts, key=lambda row: (row.cik, row.period_end, row.filed, row.fact_id)):
        for as_of, boundary in [(fact.filed, "filing_day"),
                                ((date.fromisoformat(fact.available_on) - timedelta(days=1)).isoformat(),
                                 "before_available"),
                                (fact.available_on, "available")]:
            for period in (fact.period_end, None):
                key = (fact.cik, fact.concept, fact.unit, as_of, period)
                if key in seen:
                    continue
                seen.add(key)
                kind = "exact" if period else "latest"
                query = Query(f"q-{len(cases):06d}", fact.cik, fact.concept,
                              fact.unit, as_of, period)
                cases.append(_oracle(tuple(scopes[key[:3]]), query, f"{kind}_{boundary}"))
    return cases


def scheduled_cases(facts: Iterable[Fact], dates: Iterable[str], prefix: str = "s",
                    max_answer_age_days: int | None = None) -> list[EvaluationCase]:
    """Latest-annual questions on a fixed calendar schedule for every series.

    Each series (CIK, concept, unit) is queried from its first availability date
    onward, so the schedule measures how a backend behaves while the series is
    observable. With ``max_answer_age_days`` a question is kept only while the
    point-in-time answer's fiscal year ended at most that many days before the
    question date; this drops series the issuer has stopped reporting, which
    could never leak and would dilute every rate. The bound uses only
    information available on the question date. Rates depend on the schedule
    and are reported with it.
    """
    facts = tuple(facts)
    dates = sorted(set(dates))
    scopes: dict[tuple[str, str, str], list[Fact]] = defaultdict(list)
    for fact in facts:
        scopes[(fact.cik, fact.concept, fact.unit)].append(fact)
    cases = []
    for scope in sorted(scopes):
        rows = tuple(scopes[scope])
        first = min(fact.available_on for fact in rows)
        for as_of in dates:
            if as_of < first:
                continue
            query = Query(f"{prefix}-{len(cases):07d}", *scope, as_of)
            case = _oracle(rows, query, "scheduled_latest")
            if (max_answer_age_days is not None and case.expected_period_end is not None
                    and (date.fromisoformat(as_of) - date.fromisoformat(case.expected_period_end)).days
                    > max_answer_age_days):
                continue
            cases.append(case)
    return cases


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * quantile
    left = int(index)
    right = min(left + 1, len(values) - 1)
    return values[left] + (values[right] - values[left]) * (index - left)


def _metric_pairs(row: dict) -> dict[str, tuple[int, int]]:
    return {
        "accuracy": (int(row["correct"]), 1),
        "coverage": (int(row["answered"]), 1),
        "answer_leakage_rate_all_queries": (int(row["answer_leaked"]), 1),
        "answer_leakage_rate_answered": (int(row["answer_leaked"]), int(row["answered"])),
        "context_leakage_rate": (int(row["context_leaked"]), 1),
        "future_filing_answer_rate_all_queries": (int(row["future_filing_answer"]), 1),
        "leaked_and_incorrect_rate": (int(row["answer_leaked"] and not row["correct"]), 1),
        "correct_abstention_rate": (int(row["correct_abstention"]),
                                    int(row["expected_status"] == "not_known")),
    }


def _summarize(rows: list[dict], bootstrap_samples: int, seed: int) -> dict:
    names = list(_metric_pairs(rows[0])) if rows else [
        "accuracy", "coverage", "answer_leakage_rate_all_queries", "answer_leakage_rate_answered",
        "context_leakage_rate", "future_filing_answer_rate_all_queries", "leaked_and_incorrect_rate",
        "correct_abstention_rate"]
    clustered: dict[str, dict[str, list[int]]] = defaultdict(lambda: {name: [0, 0] for name in names})
    for row in rows:
        for name, (numerator, denominator) in _metric_pairs(row).items():
            counts = clustered[row["query"]["cik"]][name]
            counts[0] += numerator
            counts[1] += denominator
    groups = list(clustered.values())
    totals = {name: [sum(group[name][i] for group in groups) for i in (0, 1)] for name in names}
    draws: dict[str, list[float]] = {name: [] for name in names}
    rng = random.Random(seed)
    # All queries from an issuer move together; repeated filing boundaries are
    # not falsely treated as independent observations.
    if len(groups) >= 2:
        for _ in range(bootstrap_samples):
            sample = [groups[rng.randrange(len(groups))] for _ in groups]
            for name in names:
                n = sum(group[name][0] for group in sample)
                d = sum(group[name][1] for group in sample)
                if d:
                    draws[name].append(n / d)
    metrics = {}
    for name, (numerator, denominator) in totals.items():
        values = draws[name]
        metrics[name] = {
            "value": numerator / denominator if denominator else None,
            "numerator": numerator,
            "denominator": denominator,
            "ci95": [_percentile(values, .025), _percentile(values, .975)] if values else None,
            "bootstrap_valid_samples": len(values),
        }
    latencies = [row["latency_ms"] for row in rows]
    return {"queries": len(rows), "issuer_clusters": len(groups), "metrics": metrics,
            "latency_ms": {"mean": sum(latencies) / len(latencies) if latencies else None,
                           "p95": _percentile(latencies, .95)},
            "cost": {"llm_calls": 0, "embedding_api_calls": 0, "api_usd": 0.0,
                     "note": "Local retrieval only; machine and data costs are not estimated."}}


def evaluate(facts: Iterable[Fact], cases: Iterable[EvaluationCase] | None = None,
             bootstrap_samples: int = 500, seed: int = 7,
             bitemporal_retriever: Callable[[Query], Retrieval] | None = None) -> dict:
    if bootstrap_samples < 0:
        raise ValueError("bootstrap_samples must be non-negative")
    facts = tuple(facts)
    cases = list(cases) if cases is not None else generate_cases(facts)
    if len({case.query.query_id for case in cases}) != len(cases):
        raise ValueError("query IDs must be unique")
    build_started = time.perf_counter()
    index = BackendIndex(facts)
    build_ms = (time.perf_counter() - build_started) * 1000
    traces = []
    summaries = {}
    for backend in BACKENDS:
        rows = []
        for case in cases:
            started = time.perf_counter()
            result = (bitemporal_retriever(case.query)
                      if backend == "bitemporal" and bitemporal_retriever is not None
                      else index.retrieve(case.query, backend))
            latency = (time.perf_counter() - started) * 1000
            fact = result.fact
            correct = result.status == case.expected_status
            if case.expected_status == "known":
                correct = bool(correct and fact is not None
                               and math.isclose(fact.value, case.expected_value, rel_tol=1e-10, abs_tol=1e-9)
                               and (case.expected_period_end is None
                                    or fact.period_end == case.expected_period_end))
            row = {
                "backend": backend,
                "query": asdict(case.query),
                "category": case.category,
                "expected_status": case.expected_status,
                "expected_value": case.expected_value,
                "expected_fact_id": case.expected_fact_id,
                "expected_period_end": case.expected_period_end,
                "expected_filed": case.expected_filed,
                "status": result.status,
                "reason": result.reason,
                "answer": asdict(fact) if fact else None,
                "context": [asdict(item) for item in result.context],
                "correct": correct,
                "answered": fact is not None,
                "answer_leaked": bool(fact and fact.available_on > case.query.as_of),
                "future_filing_answer": bool(fact and fact.filed > case.query.as_of),
                "context_leaked": any(item.available_on > case.query.as_of for item in result.context),
                "correct_abstention": case.expected_status == "not_known" and result.status == "not_known",
                "latency_ms": latency,
            }
            rows.append(row)
        summaries[backend] = _summarize(rows, bootstrap_samples, seed)
        traces.extend(rows)
    return {
        "schema_version": 1,
        "experiment": "retrieval_only",
        "dataset": {"facts": len(facts), "queries": len(cases),
                    "issuers": len({fact.cik for fact in facts}),
                    "query_distribution": "boundary-enriched; not a prevalence estimate"},
        "bootstrap": {"unit": "issuer", "samples": bootstrap_samples, "seed": seed,
                      "method": "percentile cluster bootstrap; CI unavailable below two issuers"},
        "index_build_ms": build_ms,
        "bitemporal_implementation": "provided_retriever" if bitemporal_retriever is not None else "in_memory_reference",
        "backends": summaries,
        "attribution": "Future facts in logged context prove retrieval exposure. This experiment runs no LLM and makes no claim about model-training leakage.",
        "traces": traces,
    }


def write_evaluation(report: dict, output_dir: str | Path, compress: bool = False) -> dict[str, str]:
    """Write aggregate metrics, one trace per query and backend, and a flat summary.

    ``compress`` gzips the trace file, which is useful for large real-data runs.
    """
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / "evaluation.json"
    traces_path = directory / ("retrievals.jsonl.gz" if compress else "retrievals.jsonl")
    summary_path = directory / "summary.csv"
    report_path.write_text(json.dumps({key: value for key, value in report.items() if key != "traces"},
                                      indent=2, allow_nan=False) + "\n")
    opener = (lambda: gzip.open(traces_path, "wt", encoding="utf-8")) if compress else (lambda: traces_path.open("w"))
    with opener() as handle:
        for trace in report["traces"]:
            handle.write(json.dumps(trace, allow_nan=False) + "\n")
    with summary_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["backend", "metric", "value", "numerator",
                                                   "denominator", "ci95_low", "ci95_high"])
        writer.writeheader()
        for backend, summary in report["backends"].items():
            for name, metric in summary["metrics"].items():
                interval = metric["ci95"] or [None, None]
                writer.writerow({"backend": backend, "metric": name, "value": metric["value"],
                                 "numerator": metric["numerator"], "denominator": metric["denominator"],
                                 "ci95_low": interval[0], "ci95_high": interval[1]})
    return {"evaluation": str(report_path), "retrievals": str(traces_path), "summary": str(summary_path)}
