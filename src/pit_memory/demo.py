"""Entirely fictional regression fixture; never an empirical market claim."""
from __future__ import annotations

import csv
import importlib.util
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path

from .evaluation import EvaluationCase, evaluate, write_evaluation
from .factor import evaluate_factor
from .models import Fact, Query
from .store import DuckDBStore


@dataclass
class DemoData:
    facts: list[Fact]
    cases: list[EvaluationCase]
    returns: list[dict]


def build_demo(issuers: int = 24, seed: int = 19) -> DemoData:
    if issuers < 3:
        raise ValueError("demo requires at least three fictional issuers")
    rng = random.Random(seed)
    facts: list[Fact] = []
    cases: list[EvaluationCase] = []
    returns = []
    concept = "us-gaap:Revenues"
    for index in range(issuers):
        cik = str(9000000000 + index)
        baseline = 1000 + index * 20
        original21 = baseline * (1 + rng.uniform(-.08, .2))
        original22 = original21 * (1 + rng.uniform(-.08, .2))
        trend = -.05 + index / (issuers - 1) * .3
        revised21 = baseline * (1 + trend)
        revised22 = revised21 * (1 + trend)

        def make(year: int, value: float, filed: str, available: str, suffix: str) -> Fact:
            return Fact(cik, concept, "USD", value, f"{year}-01-01", f"{year}-12-31",
                        filed, f"SYNTHETIC-{cik}-{suffix}", "10-K/A" if "rev" in suffix else "10-K",
                        available, source_url=f"synthetic://demo/{cik}/{suffix}")

        original20 = make(2020, baseline, "2021-02-12", "2021-02-16", "2020-original")
        annual21 = make(2021, original21, "2022-02-15", "2022-02-16", "2021-original")
        annual22 = make(2022, original22, "2023-02-15", "2023-02-16", "2022-original")
        revision20 = make(2020, baseline, "2023-09-15", "2023-09-18", "2020-revised")
        revision21 = make(2021, revised21, "2023-09-15", "2023-09-18", "2021-revised")
        revision22 = make(2022, revised22, "2024-06-14", "2024-06-17", "2022-revised")
        facts.extend((original20, annual21, annual22, revision20, revision21, revision22))

        # These expectations are intentionally handwritten, not backend output.
        labels = [
            ("2021-02-11", "2020-12-31", None, "not_yet_filed"),
            ("2021-02-12", "2020-12-31", None, "filing_day"),
            ("2021-02-15", "2020-12-31", None, "exchange_holiday"),
            ("2021-02-16", "2020-12-31", original20, "first_available"),
            ("2021-03-15", None, original20, "latest_original_year"),
            ("2022-02-15", None, original20, "latest_before_new_year_available"),
            ("2022-02-16", None, annual21, "latest_new_year"),
            ("2023-08-01", "2021-12-31", annual21, "before_restatement"),
            ("2023-09-15", "2021-12-31", annual21, "restatement_filing_day"),
            ("2023-09-18", "2021-12-31", revision21, "restatement_available"),
            ("2023-09-18", None, annual22, "older_restatement_not_latest_period"),
            ("2024-06-14", None, annual22, "latest_amendment_filing_day"),
            ("2024-06-17", None, revision22, "latest_amendment_available"),
        ]
        for as_of, period, expected, category in labels:
            query = Query(f"demo-{index:03d}-{category}", cik, concept, "USD", as_of, period)
            cases.append(EvaluationCase(query, expected.value if expected else None,
                                        expected.fact_id if expected else None,
                                        "known" if expected else "not_known", category,
                                        expected.filed if expected else None,
                                        expected.period_end if expected else None))
        # Deliberately correlate returns with the future revisions. This checks
        # the inflation detector, not the size of leakage in real markets.
        for as_of, return_end in [("2022-07-01", "2022-08-01"), ("2022-10-03", "2022-11-03"),
                                  ("2023-03-01", "2023-04-03"), ("2023-06-01", "2023-07-03")]:
            returns.append({"cik": cik, "as_of": as_of, "forward_return": trend + rng.uniform(-.01, .01),
                            "return_end": return_end})
    return DemoData(facts, cases, returns)


def run_demo(output_dir: str | Path, issuers: int = 24, bootstrap_samples: int = 500,
             seed: int = 19) -> dict:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    data = build_demo(issuers, seed)
    # Avoid mixing real imports or a differently seeded fixture into the demo.
    facts_path = directory / "synthetic-facts.jsonl"
    if facts_path.exists():
        try:
            old_ids = {json.loads(line)["fact_id"] for line in facts_path.read_text().splitlines() if line}
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{facts_path} is not a demo output; use a new output directory") from exc
        if old_ids != {fact.fact_id for fact in data.facts}:
            raise ValueError("output contains a different dataset; use a new output directory")
    # evaluate and factor write the same report names; never replace a real audit.
    for name, is_demo in (("evaluation.json", lambda report: report.get("dataset", {}).get("synthetic") is True),
                          ("factor.json", lambda report: report.get("synthetic") is True)):
        existing = directory / name
        if existing.exists():
            try:
                ours = is_demo(json.loads(existing.read_text()))
            except (ValueError, AttributeError):
                ours = False
            if not ours:
                raise ValueError(f"{existing} is not a demo output; use a new output directory")
    db_path = directory / "demo.duckdb"
    database_paths = {}
    if importlib.util.find_spec("duckdb") is not None:
        with DuckDBStore(db_path) as store:
            existing = store.all_facts()
            if existing and {fact.fact_id for fact in existing} != {fact.fact_id for fact in data.facts}:
                raise ValueError("demo.duckdb contains a different dataset; use a new output directory")
            store.append(data.facts)
            report = evaluate(data.facts, data.cases, bootstrap_samples=bootstrap_samples,
                              seed=seed, bitemporal_retriever=store.retrieve)
        storage_mode = "duckdb"
        database_paths["database"] = str(db_path)
    else:
        if db_path.exists():
            raise ValueError("DuckDB is needed to inspect the existing demo database; install it or use a new directory")
        report = evaluate(data.facts, data.cases, bootstrap_samples=bootstrap_samples, seed=seed)
        storage_mode = "in_memory_reference; DuckDB unavailable (no database written)"
    report["storage_mode"] = storage_mode
    report["dataset"]["synthetic"] = True
    report["dataset"]["description"] = "Fictional issuers and deliberately planted restatements; no prevalence or performance claim."
    paths = write_evaluation(report, directory)
    factor_report = evaluate_factor(data.facts, data.returns, bootstrap_samples=bootstrap_samples, seed=seed)
    factor_report["synthetic"] = True
    factor_report["warning"] = "Returns were deliberately correlated with future revisions. IC inflation is a fixture check, not an empirical financial result."
    factor_path = directory / "factor.json"
    factor_path.write_text(json.dumps(factor_report, indent=2, allow_nan=False) + "\n")
    facts_path = directory / "synthetic-facts.jsonl"
    facts_path.write_text("".join(json.dumps(asdict(fact)) + "\n" for fact in data.facts))
    cases_path = directory / "synthetic-cases.jsonl"
    cases_path.write_text("".join(json.dumps(asdict(case)) + "\n" for case in data.cases))
    returns_path = directory / "synthetic-returns.csv"
    with returns_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["cik", "as_of", "forward_return", "return_end"])
        writer.writeheader()
        writer.writerows(data.returns)
    return {"synthetic": True, "issuers": issuers, "facts": len(data.facts),
            "queries": len(data.cases), "storage_mode": storage_mode, "paths": {**paths, **database_paths,
            "factor": str(factor_path), "facts": str(facts_path), "cases": str(cases_path),
            "returns": str(returns_path)}, "backends": report["backends"]}
