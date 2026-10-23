"""Real-data study: revisions in the source, scheduled leakage, and signal drift.

Three questions are answered from one set of filing-level facts:

1. How often does a later filing report a different value for a fiscal period
   that was already published? (``revision_profile``)
2. On a plain monthly query schedule, how often does each retrieval backend
   serve information that was not yet available? (``scheduled_cases`` plus
   ``evaluate``)
3. How much does look-ahead change a code-computed revenue-growth signal and
   its cross-sectional ranking? (``signal_divergence``). This needs no price
   data, so it measures signal contamination, not trading performance.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Callable, Iterable, Sequence

from .backends import BACKENDS, BackendIndex
from .evaluation import evaluate, scheduled_cases, write_evaluation
from .factor import ForwardReturn, revenue_growth, spearman
from .models import Fact, Query, Retrieval, iso_date, normalize_cik

# Relative difference below which two filings of one period count as equal.
# SEC values are reported in whole units, so this only absorbs float noise.
REVISION_TOLERANCE = 1e-6


def month_starts(start: str, end: str) -> list[str]:
    """First calendar day of every month from ``start`` through ``end``."""
    first, last = date.fromisoformat(iso_date(start)), date.fromisoformat(iso_date(end))
    if last < first:
        raise ValueError("schedule end precedes start")
    year, month = first.year, first.month
    if first.day != 1:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    result = []
    while date(year, month, 1) <= last:
        result.append(date(year, month, 1).isoformat())
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return result


def load_universe(path: str | Path) -> dict[str, str]:
    """Read ``CIK  # name`` lines; return normalized CIK to display name."""
    universe: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        body, _, comment = line.partition("#")
        if body.strip():
            universe[normalize_cik(body.strip())] = comment.strip()
    if not universe:
        raise ValueError("universe file lists no CIKs")
    return universe


def _differs(a: float, b: float) -> bool:
    return abs(a - b) > REVISION_TOLERANCE * max(1.0, abs(a), abs(b))


def revision_profile(facts: Iterable[Fact]) -> dict:
    """Describe how later filings restate fiscal periods that were already public.

    A period is identified by CIK, concept, unit, start and end. Its original
    value comes from the earliest filing date. A period is *revised* when any
    later filing reports a different value. Periods whose earliest filing date
    already carries conflicting values are counted separately and excluded
    from the revision statistics.
    """
    groups: dict[tuple, list[Fact]] = defaultdict(list)
    for fact in facts:
        groups[(fact.cik, fact.concept, fact.unit, fact.period_start, fact.period_end)].append(fact)
    records = []
    conflicting = 0
    for key, rows in groups.items():
        rows.sort(key=lambda fact: (fact.filed, fact.fact_id))
        first_filed = rows[0].filed
        originals = [fact for fact in rows if fact.filed == first_filed]
        if len({fact.value for fact in originals}) > 1:
            conflicting += 1
            continue
        original = originals[0]
        later = [fact for fact in rows if fact.filed > first_filed]
        changed = [fact for fact in later if _differs(fact.value, original.value)]
        final = rows[-1]
        record = {
            "cik": key[0], "concept": key[1], "unit": key[2],
            "period_start": key[3], "period_end": key[4],
            "filings": len({fact.filed for fact in rows}),
            "original_value": original.value, "original_filed": original.filed,
            "original_accession": original.accession,
            "final_value": final.value, "final_filed": final.filed,
            "revised": bool(changed),
            "first_revision_filed": changed[0].filed if changed else None,
            "first_revision_value": changed[0].value if changed else None,
            "first_revision_form": changed[0].form if changed else None,
            "first_revision_accession": changed[0].accession if changed else None,
            "days_period_end_to_first_filing": (date.fromisoformat(original.filed)
                                                - date.fromisoformat(original.period_end)).days,
            "revision_lag_days": ((date.fromisoformat(changed[0].filed)
                                   - date.fromisoformat(original.filed)).days if changed else None),
            "relative_change": ((changed[0].value - original.value) / abs(original.value)
                                if changed and original.value != 0 else None),
        }
        records.append(record)
    records.sort(key=lambda r: (r["cik"], r["concept"], r["unit"], r["period_end"], r["period_start"]))

    def summarize(rows: list[dict]) -> dict:
        repeated = [r for r in rows if r["filings"] > 1]
        revised = [r for r in rows if r["revised"]]
        sizes = sorted(abs(r["relative_change"]) for r in revised if r["relative_change"] is not None)
        lags = sorted(r["revision_lag_days"] for r in revised)
        reporting = sorted(r["days_period_end_to_first_filing"] for r in rows)
        return {
            "periods": len(rows),
            "periods_with_later_filings": len(repeated),
            "periods_revised": len(revised),
            "revised_share_of_periods": len(revised) / len(rows) if rows else None,
            "revised_share_of_refiled_periods": len(revised) / len(repeated) if repeated else None,
            "issuers_with_a_revision": len({r["cik"] for r in revised}),
            "median_abs_relative_change": median(sizes) if sizes else None,
            "p90_abs_relative_change": sizes[int(0.9 * (len(sizes) - 1))] if sizes else None,
            "share_of_revisions_above_1pct": (sum(size > 0.01 for size in sizes) / len(sizes)) if sizes else None,
            "median_revision_lag_days": median(lags) if lags else None,
            "median_days_period_end_to_first_filing": median(reporting) if reporting else None,
        }

    by_concept = defaultdict(list)
    for record in records:
        by_concept[record["concept"]].append(record)
    return {
        "tolerance": REVISION_TOLERANCE,
        "conflicting_original_periods_excluded": conflicting,
        "overall": summarize(records),
        "by_concept": {concept: summarize(rows) for concept, rows in sorted(by_concept.items())},
        "records": records,
    }


def signal_divergence(facts: Sequence[Fact], ciks: Iterable[str], dates: Iterable[str],
                      concept: str, unit: str = "USD", min_entities: int = 5,
                      max_age_days: int | None = None) -> dict:
    """Compare each backend's revenue-growth signal with the point-in-time signal.

    For every CIK and date the signal is computed by every backend with the same
    code. Reported per backend: how many point-in-time signals it reproduces
    exactly, how many it changes, how many it invents where no point-in-time
    signal existed, and the per-date rank correlation with the point-in-time
    cross-section. Forward returns are not used.

    With ``max_age_days``, an issuer-date is dropped from every backend when the
    point-in-time current fiscal year ended more than that many days earlier
    (the issuer stopped reporting the concept). Issuer-dates without any
    point-in-time value are kept, because a naive signal there is look-ahead.
    """
    facts = list(facts)
    index = BackendIndex(facts)
    ciks = sorted({normalize_cik(cik) for cik in ciks})
    dates = sorted(set(dates))
    stale: set[tuple[str, str]] = set()
    if max_age_days is not None:
        for as_of in dates:
            for cik in ciks:
                current = index.retrieve(Query("age", cik, concept, unit, as_of), "bitemporal").fact
                if current is not None and (date.fromisoformat(as_of)
                                            - date.fromisoformat(current.period_end)).days > max_age_days:
                    stale.add((cik, as_of))
    signals: dict[str, dict[tuple[str, str], dict]] = {backend: {} for backend in BACKENDS}
    for backend in BACKENDS:
        for as_of in dates:
            for cik in ciks:
                if (cik, as_of) in stale:
                    continue
                row = ForwardReturn(cik, as_of, 0.0)
                trace = revenue_growth(facts, row, backend, concept, unit, index=index)
                if trace["signal"] is not None:
                    signals[backend][(cik, as_of)] = trace
    reference = signals["bitemporal"]
    summary = {}
    per_date = []
    for backend in BACKENDS:
        mine = signals[backend]
        common = sorted(mine.keys() & reference.keys())
        changed = [key for key in common if _differs(mine[key]["signal"], reference[key]["signal"])]
        abs_diffs = [abs(mine[key]["signal"] - reference[key]["signal"]) for key in changed]
        correlations = []
        for as_of in dates:
            keys = [key for key in common if key[1] == as_of]
            rho = None
            if len(keys) >= min_entities:
                rho = spearman([mine[k]["signal"] for k in keys], [reference[k]["signal"] for k in keys])
            if rho is not None:
                correlations.append(rho)
            per_date.append({"backend": backend, "as_of": as_of, "common_entities": len(keys),
                             "changed_entities": sum(1 for k in changed if k[1] == as_of),
                             "rank_correlation_with_bitemporal": rho})
        summary[backend] = {
            "signals": len(mine),
            "signals_using_unavailable_facts": sum(
                1 for trace in mine.values() if trace["answer_leakage"]),
            "common_with_bitemporal": len(common),
            "changed_vs_bitemporal": len(changed),
            "changed_share": len(changed) / len(common) if common else None,
            "median_abs_change_when_changed": median(abs_diffs) if abs_diffs else None,
            "signals_without_bitemporal_counterpart": len(mine.keys() - reference.keys()),
            "bitemporal_signals_missing": len(reference.keys() - mine.keys()),
            "dates_with_rank_correlation": len(correlations),
            "mean_rank_correlation_with_bitemporal": mean(correlations) if correlations else None,
            "min_rank_correlation_with_bitemporal": min(correlations) if correlations else None,
        }
    return {
        "signal": "annual revenue growth (current / previous annual value - 1)",
        "concept": concept, "unit": unit, "issuers": len(ciks), "dates": len(dates),
        "minimum_entities_per_date": min_entities,
        "max_age_days": max_age_days, "stale_issuer_dates_excluded": len(stale),
        "engine": "in_memory_reference",
        "summary": summary, "per_date": per_date,
    }


def knowledge_timeline(facts: Iterable[Fact], cik: str, concept: str, unit: str,
                       period_end: str, dates: Iterable[str]) -> list[dict]:
    """What each backend reports for one fiscal period on each date (chart input)."""
    facts = list(facts)
    index = BackendIndex(facts)
    rows = []
    for as_of in sorted(set(dates)):
        query = Query(f"timeline-{as_of}", cik, concept, unit, as_of, period_end)
        row = {"as_of": as_of}
        for backend in BACKENDS:
            result = index.retrieve(query, backend)
            row[backend] = result.fact.value if result.fact else None
        rows.append(row)
    return rows


def _write_csv(path: Path, rows: list[dict], fields: Sequence[str]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_study(facts: Sequence[Fact], output_dir: str | Path, *, concepts: Sequence[str],
              signal_concept: str, start: str, end: str, unit: str = "USD",
              names: dict[str, str] | None = None, bootstrap_samples: int = 500, seed: int = 7,
              bitemporal_retriever: Callable[[Query], Retrieval] | None = None,
              storage: str = "in_memory_reference", max_age_days: int | None = 550) -> dict:
    """Run the three analyses on the given concepts and write their evidence.

    ``max_age_days`` bounds how old a "latest annual" answer may be before the
    series counts as no longer reported (default 550: one annual cycle plus
    the longest regular 10-K filing deadline and a margin).
    """
    if not concepts:
        raise ValueError("name at least one concept")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    selected = [fact for fact in facts if fact.concept in set(concepts) and fact.unit == unit]
    if not selected:
        raise ValueError("no facts match the requested concepts and unit")
    names = names or {}
    schedule = month_starts(start, end)
    if not schedule:
        raise ValueError("the schedule contains no first-of-month date; widen --start/--end")

    profile = revision_profile(selected)
    revised = [r for r in profile["records"] if r["revised"]]
    for record in revised:
        record["issuer"] = names.get(record["cik"], "")
    _write_csv(directory / "revisions.csv", revised,
               ["cik", "issuer", "concept", "unit", "period_start", "period_end", "original_value",
                "original_filed", "original_accession", "first_revision_value", "first_revision_filed",
                "first_revision_form", "first_revision_accession", "revision_lag_days", "relative_change",
                "filings"])

    cases = scheduled_cases(selected, schedule, max_answer_age_days=max_age_days)
    if not cases:
        raise ValueError("no scheduled question falls inside the data's availability window")
    report = evaluate(selected, cases, bootstrap_samples=bootstrap_samples, seed=seed,
                      bitemporal_retriever=bitemporal_retriever)
    report["dataset"]["query_distribution"] = (
        "latest-annual question on the first day of every month for every series, "
        f"{schedule[0]} to {schedule[-1]}, from the series' first availability date, "
        f"while the point-in-time answer is at most {max_age_days} days past fiscal year end")
    report["dataset"]["concepts"] = list(concepts)
    report["storage_mode"] = storage
    paths = write_evaluation(report, directory / "leakage", compress=True)

    signal_ciks = sorted({fact.cik for fact in selected if fact.concept == signal_concept})
    divergence = signal_divergence(selected, signal_ciks, schedule, signal_concept, unit,
                                   max_age_days=max_age_days)
    (directory / "signal_divergence.json").write_text(json.dumps(divergence, indent=2, allow_nan=False) + "\n")

    example = max((r for r in revised if r["relative_change"] is not None and r["concept"] == signal_concept),
                  key=lambda r: abs(r["relative_change"]), default=None)
    timeline = None
    if example is not None:
        lo = date.fromisoformat(example["period_end"]).replace(day=1).isoformat()
        hi_year = date.fromisoformat(example["first_revision_filed"]).year + 1
        timeline = {
            "cik": example["cik"], "issuer": names.get(example["cik"], ""),
            "concept": example["concept"], "unit": unit,
            "period_start": example["period_start"], "period_end": example["period_end"],
            "original_filed": example["original_filed"],
            "first_revision_filed": example["first_revision_filed"],
            "rows": knowledge_timeline(selected, example["cik"], example["concept"], unit,
                                       example["period_end"], month_starts(lo, f"{hi_year}-12-31")),
        }

    result = {
        "schema_version": 1,
        "kind": "sec_companyfacts_study",
        "universe": {"issuers": len({fact.cik for fact in selected}),
                     "names": {cik: names.get(cik, "") for cik in sorted({f.cik for f in selected})}},
        "concepts": list(concepts), "unit": unit, "signal_concept": signal_concept,
        "schedule": {"frequency": "monthly", "start": schedule[0], "end": schedule[-1],
                     "dates": len(schedule), "max_answer_age_days": max_age_days},
        "engines": {"leakage_audit_bitemporal": storage,
                    "baselines_signal_divergence_and_timeline": "in_memory_reference"},
        "facts": len(selected),
        "source_snapshots": sorted({fact.source_sha256 for fact in selected}),
        "storage_mode": storage,
        "revisions": {k: v for k, v in profile.items() if k != "records"},
        "leakage": {backend: {name: metric for name, metric in summary["metrics"].items()}
                    for backend, summary in report["backends"].items()},
        "leakage_queries": len(cases),
        "signal_divergence": divergence["summary"],
        "example_revision": timeline,
        "caveats": [
            "Convenience universe of today's large issuers; not survivorship-free.",
            "Companyfacts is a retrospective aggregation, not an archive of historical API snapshots.",
            "Rates depend on the stated monthly schedule; they are not market-wide prevalence estimates.",
            "Signal divergence uses no returns and makes no performance claim.",
        ],
    }
    (directory / "study.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    _write_csv(directory / "leakage_summary.csv",
               [{"backend": b, "metric": m, **{k: v for k, v in metric.items() if k != "ci95"},
                 "ci95_low": (metric["ci95"] or [None, None])[0],
                 "ci95_high": (metric["ci95"] or [None, None])[1]}
                for b, metrics in result["leakage"].items() for m, metric in metrics.items()],
               ["backend", "metric", "value", "numerator", "denominator", "ci95_low", "ci95_high"])
    return {"study": str(directory / "study.json"), "revisions": str(directory / "revisions.csv"),
            "signal_divergence": str(directory / "signal_divergence.json"), **paths}


def facts_from_jsonl(path: str | Path) -> list[Fact]:
    """Load facts written one JSON object per line (for example the sample data)."""
    facts = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                facts.append(Fact(**json.loads(line)))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{number}: invalid fact: {exc}") from exc
    return facts


def write_facts_jsonl(facts: Iterable[Fact], path: str | Path) -> int:
    rows = sorted(facts, key=lambda f: (f.cik, f.concept, f.unit, f.period_end, f.filed, f.fact_id))
    Path(path).write_text("".join(json.dumps(asdict(fact), sort_keys=True) + "\n" for fact in rows))
    return len(rows)
