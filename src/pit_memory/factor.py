"""Annual-revenue-growth rank IC with explicit return provenance and pairing.

Forward returns must begin at the close of ``as_of``. This module measures
retrieval effects; it is not a trading strategy or a portfolio backtest.
"""

from __future__ import annotations

import csv
import math
import random
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence

from .backends import BACKENDS, BackendIndex
from .models import Fact, Query, normalize_cik


@dataclass(frozen=True)
class ForwardReturn:
    cik: str
    as_of: str
    forward_return: float
    return_end: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cik", normalize_cik(str(self.cik).strip()))
        if date.fromisoformat(self.as_of).isoformat() != self.as_of:
            raise ValueError("as_of must be YYYY-MM-DD")
        value = float(self.forward_return)
        if not math.isfinite(value):
            raise ValueError("forward_return must be finite")
        if value < -1:
            raise ValueError("simple forward_return cannot be below -1")
        object.__setattr__(self, "forward_return", value)
        if self.return_end:
            if date.fromisoformat(self.return_end).isoformat() != self.return_end:
                raise ValueError("return_end must be YYYY-MM-DD")
            if self.return_end <= self.as_of:
                raise ValueError("return_end must follow as_of")


def _validated_rows(rows: Iterable[ForwardReturn | Mapping[str, Any]]) -> list[ForwardReturn]:
    result: list[ForwardReturn] = []
    seen: set[tuple[str, str]] = set()
    horizon_by_date: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, ForwardReturn):
            try:
                row = ForwardReturn(
                    cik=str(row["cik"]), as_of=str(row["as_of"]),
                    forward_return=row["forward_return"],
                    return_end=str(row["return_end"]) if row.get("return_end") else None,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid forward-return row: {exc}") from exc
        key = (row.cik, row.as_of)
        if key in seen:
            raise ValueError(f"duplicate forward return for {row.cik} on {row.as_of}")
        if row.return_end:
            other_end = horizon_by_date.setdefault(row.as_of, row.return_end)
            if other_end != row.return_end:
                raise ValueError(f"inconsistent return_end values on {row.as_of}")
        seen.add(key)
        result.append(row)
    return sorted(result, key=lambda r: (r.as_of, r.cik))


def load_returns(path: str | Path) -> list[ForwardReturn]:
    """Read simple decimal returns (0.05 = 5%); reject missing/duplicate rows."""
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"cik", "as_of", "forward_return"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("returns CSV requires cik,as_of,forward_return columns")
        rows = _validated_rows(reader)
    if not rows:
        raise ValueError("returns CSV contains no data rows")
    return rows


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + 1 + end) / 2
        for index in order[start:end]:
            result[index] = rank
        start = end
    return result


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Tie-aware Spearman rank correlation; constant/short inputs are undefined."""
    if len(x) != len(y):
        raise ValueError("rank-correlation inputs must have equal length")
    if any(not math.isfinite(float(v)) for v in [*x, *y]):
        raise ValueError("rank-correlation inputs must be finite")
    if len(x) < 2:
        return None
    rx, ry = _ranks(x), _ranks(y)
    mx, my = mean(rx), mean(ry)
    dx, dy = [v - mx for v in rx], [v - my for v in ry]
    denominator = math.sqrt(sum(v * v for v in dx) * sum(v * v for v in dy))
    if denominator == 0:
        return None
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(dx, dy)) / denominator))


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    at = (len(ordered) - 1) * quantile
    low = math.floor(at)
    high = math.ceil(at)
    return ordered[low] + (ordered[high] - ordered[low]) * (at - low)


def _bootstrap_mean(values: Sequence[float], samples: int, seed: int) -> list[float] | None:
    if len(values) < 2 or samples == 0:
        return None
    rng = random.Random(seed)
    sampled = [mean(rng.choices(values, k=len(values))) for _ in range(samples)]
    return [_percentile(sampled, 0.025), _percentile(sampled, 0.975)]


def revenue_growth(
    facts: Sequence[Fact], row: ForwardReturn, backend: str,
    concept: str = "us-gaap:Revenues", unit: str = "USD",
    *, index: BackendIndex | None = None,
) -> dict[str, Any]:
    """Retrieve annual observations one fiscal year apart, preserving both citations."""
    query = Query(
        query_id=f"factor:{row.cik}:{row.as_of}:current",
        cik=row.cik, concept=concept, unit=unit, as_of=row.as_of,
    )
    index = index or BackendIndex(facts)
    current_result = index.retrieve(query, backend)
    current = current_result.answer
    output: dict[str, Any] = {
        "cik": row.cik, "as_of": row.as_of, "forward_return": row.forward_return,
        "return_end": row.return_end, "backend": backend, "signal": None,
        "status": current_result.status, "current_fact_id": None,
        "previous_fact_id": None, "answer_leakage": False,
        "current_fact": asdict(current) if current else None, "previous_fact": None,
        "current_context": [asdict(f) for f in current_result.context], "previous_context": [],
        "context_leakage": any(f.available_on > row.as_of for f in current_result.context),
    }
    if current is None:
        return output
    output["current_fact_id"] = current.fact_id
    output["current_period_end"] = current.period_end
    output["answer_leakage"] = current.available_on > row.as_of
    ends = {
        f.period_end for f in index.scoped_facts.get((row.cik, concept, unit), ())
        if (backend != "bitemporal" or f.available_on <= row.as_of)
        and 300 <= (date.fromisoformat(current.period_end) - date.fromisoformat(f.period_end)).days <= 430
    }
    if not ends:
        output["status"] = "missing_previous_year"
        return output
    previous_query = Query(
        query_id=f"factor:{row.cik}:{row.as_of}:previous",
        cik=row.cik, concept=concept, unit=unit, as_of=row.as_of, period_end=max(ends),
    )
    previous_result = index.retrieve(previous_query, backend)
    output["context_leakage"] |= any(f.available_on > row.as_of for f in previous_result.context)
    output["previous_context"] = [asdict(f) for f in previous_result.context]
    previous = previous_result.answer
    if previous is None:
        output["status"] = f"previous_{previous_result.status}"
        return output
    output["previous_fact_id"] = previous.fact_id
    output["previous_fact"] = asdict(previous)
    output["previous_period_end"] = previous.period_end
    output["answer_leakage"] |= previous.available_on > row.as_of
    current_value, previous_value = float(current.value), float(previous.value)
    if not math.isfinite(current_value) or not math.isfinite(previous_value) or previous_value <= 0:
        output["status"] = "invalid_denominator_or_value"
        return output
    signal = current_value / previous_value - 1
    if not math.isfinite(signal):
        output["status"] = "nonfinite_signal"
        return output
    output["signal"] = signal
    output["status"] = "known"
    return output


def evaluate_factor(
    facts: Iterable[Fact], rows: Iterable[ForwardReturn | Mapping[str, Any]],
    concept: str = "us-gaap:Revenues", unit: str = "USD", min_entities: int = 3,
    bootstrap_samples: int = 500, seed: int = 17,
) -> dict[str, Any]:
    """Evaluate mean daily rank IC and same-universe paired IC differences.

    Date-bootstrap intervals are descriptive and assume dates are independent;
    overlapping return horizons require a block-bootstrap extension.
    """
    if min_entities < 3:
        raise ValueError("min_entities must be at least 3")
    if bootstrap_samples < 0:
        raise ValueError("bootstrap_samples must be nonnegative")
    observations = _validated_rows(rows)
    facts_list = list(facts)
    index = BackendIndex(facts_list)
    traces = [revenue_growth(facts_list, row, backend, concept, unit, index=index)
              for backend in BACKENDS for row in observations]
    by_backend: dict[str, dict[str, dict[str, dict[str, Any]]]] = {b: {} for b in BACKENDS}
    for trace in traces:
        if trace["signal"] is not None:
            by_backend[trace["backend"]].setdefault(trace["as_of"], {})[trace["cik"]] = trace
    per_date: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}
    dates = sorted({r.as_of for r in observations})
    for index, backend in enumerate(BACKENDS):
        values: list[float] = []
        paired_deltas: list[float] = []
        for as_of in dates:
            available = by_backend[backend].get(as_of, {})
            records = list(available.values())
            ic = spearman([r["signal"] for r in records], [r["forward_return"] for r in records]) if len(records) >= min_entities else None
            reference = by_backend["bitemporal"].get(as_of, {})
            common = sorted(available.keys() & reference.keys())
            paired_ic = paired_reference = delta = None
            if len(common) >= min_entities:
                returns = [available[c]["forward_return"] for c in common]
                paired_ic = spearman([available[c]["signal"] for c in common], returns)
                paired_reference = spearman([reference[c]["signal"] for c in common], returns)
                if paired_ic is not None and paired_reference is not None:
                    delta = paired_ic - paired_reference
                    paired_deltas.append(delta)
            if ic is not None:
                values.append(ic)
            per_date.append({
                "backend": backend, "as_of": as_of, "entities": len(records), "ic": ic,
                "paired_entities": len(common), "paired_ic": paired_ic,
                "paired_bitemporal_ic": paired_reference, "paired_delta_ic": delta,
            })
        selected = [t for t in traces if t["backend"] == backend]
        successful = [t for t in selected if t["signal"] is not None]
        summaries[backend] = {
            "input_rows": len(observations), "signal_rows": len(successful),
            "signal_coverage": len(successful) / len(observations) if observations else None,
            "signal_rows_with_future_facts": sum(t["answer_leakage"] for t in successful),
            "dates_with_defined_ic": len(values), "mean_ic": mean(values) if values else None,
            "mean_ic_ci95": _bootstrap_mean(values, bootstrap_samples, seed + index),
            "paired_dates": len(paired_deltas),
            "paired_mean_delta_ic_vs_bitemporal": mean(paired_deltas) if paired_deltas else None,
            "paired_delta_ci95": _bootstrap_mean(paired_deltas, bootstrap_samples, seed + index),
        }
    return {
        "factor": "annual_revenue_growth", "concept": concept, "unit": unit,
        "bitemporal_implementation": "in_memory_reference",
        "return_convention": "simple decimal forward return beginning at the close of as_of",
        "minimum_entities_per_date": min_entities,
        "interval_method": "percentile bootstrap over dates, independent-date assumption; null for fewer than two dates",
        "bootstrap_samples": bootstrap_samples, "seed": seed,
        "limitations": [
            "IC measures cross-sectional association, not strategy profit or Sharpe.",
            "Caller supplies returns, investable universe and horizon; survivorship and price adjustments are not audited here.",
            "Date bootstrap does not account for serial dependence or overlapping return horizons.",
            "Optional return_end is validated when supplied; prices and horizon provenance cannot be verified from this CSV alone.",
        ],
        "summary": summaries, "per_date": per_date, "observations": traces,
    }
