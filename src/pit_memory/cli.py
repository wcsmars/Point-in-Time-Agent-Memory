"""Command-line entry points for reproducible local research runs."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


def _dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def _import_file(path: Path, store) -> dict:
    from .sec import parse_companyfacts

    raw = path.read_bytes()
    parsed = parse_companyfacts(json.loads(raw), source_sha256=hashlib.sha256(raw).hexdigest())
    inserted = store.append(parsed.facts)
    return {
        "source": str(path.resolve()), "source_sha256": parsed.source_sha256,
        "accepted": len(parsed.facts), "inserted": inserted,
        "skipped_out_of_scope": parsed.skipped, "rejected": parsed.rejected,
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Point-in-time financial memory and leakage audit")
    commands = p.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run the labelled, offline synthetic benchmark")
    demo.add_argument("--output", type=Path, default=Path("runs/demo"))

    imp = commands.add_parser("import-sec", help="Append annual facts from companyfacts JSON")
    imp.add_argument("files", nargs="+", type=Path)
    imp.add_argument("--db", type=Path, default=Path("data/sec.duckdb"))
    imp.add_argument("--report", type=Path, default=Path("runs/import-report.json"))

    fetch = commands.add_parser("fetch-sec", help="Download companyfacts with SEC_USER_AGENT")
    sources = fetch.add_mutually_exclusive_group(required=True)
    sources.add_argument("--cik", action="append", help="Repeat to download several issuers")
    sources.add_argument("--ciks-file", type=Path, help="One numeric CIK per line; # comments allowed")
    fetch.add_argument("--db", type=Path, default=Path("data/sec.duckdb"))
    fetch.add_argument("--cache", type=Path, default=Path("data/raw"))
    fetch.add_argument("--report", type=Path, default=Path("runs/fetch-report.json"))
    fetch.add_argument("--refresh", action="store_true")

    query = commands.add_parser("query", help="Retrieve a cited annual value as of a date")
    query.add_argument("--db", type=Path, required=True)
    query.add_argument("--cik", required=True)
    query.add_argument("--concept", required=True, help="Namespace-qualified SEC concept")
    query.add_argument("--unit", default="USD")
    query.add_argument("--as-of", required=True, help="YYYY-MM-DD; daily availability convention")
    query.add_argument("--period-end", help="Optional exact fiscal period end")
    query.add_argument("--backend", choices=("bitemporal", "vector", "overwrite", "period_end"),
                       default="bitemporal")

    audit = commands.add_parser("evaluate", help="Generate boundary cases and audit a fact database")
    audit.add_argument("--db", type=Path, required=True)
    audit.add_argument("--output", type=Path, default=Path("runs/audit"))
    audit.add_argument("--bootstrap-samples", type=int, default=500)
    audit.add_argument("--seed", type=int, default=7)
    audit.add_argument("--concept", help="Optional exact concept to restrict the audit")

    factor = commands.add_parser("factor", help="Compare annual-growth IC using an audited returns CSV")
    factor.add_argument("--db", type=Path, required=True)
    factor.add_argument("--returns", type=Path, required=True,
                        help="CSV columns: cik,as_of,forward_return,return_end (optional)")
    factor.add_argument("--concept", default="us-gaap:Revenues")
    factor.add_argument("--unit", default="USD")
    factor.add_argument("--output", type=Path, default=Path("runs/factor.json"))
    factor.add_argument("--min-entities", type=int, default=3)
    factor.add_argument("--bootstrap-samples", type=int, default=500)
    factor.add_argument("--seed", type=int, default=17)

    study = commands.add_parser("study", help="Revision profile, scheduled leakage audit and signal drift")
    source = study.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", type=Path, help="DuckDB fact database (bitemporal answers come from SQL)")
    source.add_argument("--facts", type=Path, help="Facts JSONL, e.g. examples/sample-data/sec-facts-sample.jsonl")
    study.add_argument("--concept", action="append", dest="concepts",
                       help="Namespace-qualified concept; repeat for several (default: revenue tags)")
    study.add_argument("--signal-concept", default="us-gaap:Revenues")
    study.add_argument("--unit", default="USD")
    study.add_argument("--start", default="2012-01-01")
    study.add_argument("--end", default="2025-12-31")
    study.add_argument("--universe", type=Path, help="CIK list with '# name' comments, for labels")
    study.add_argument("--output", type=Path, default=Path("runs/sec-study"))
    study.add_argument("--bootstrap-samples", type=int, default=500)
    study.add_argument("--seed", type=int, default=7)
    study.add_argument("--max-age-days", type=int, default=550,
                       help="Drop questions whose point-in-time answer is older than this (stale series)")

    plot = commands.add_parser("plot", help="Draw figures from a study and a demo run (needs matplotlib)")
    plot.add_argument("--study", type=Path, help="Output folder of the study command")
    plot.add_argument("--demo", type=Path, help="Output folder of the demo command")
    plot.add_argument("--output", type=Path, default=Path("figures"))

    export = commands.add_parser("export-facts", help="Write facts of selected issuers and concepts as JSONL")
    export.add_argument("--db", type=Path, required=True)
    export.add_argument("--cik", action="append", required=True)
    export.add_argument("--concept", action="append", required=True)
    export.add_argument("--unit", default="USD")
    export.add_argument("--output", type=Path, required=True)
    return p


DEFAULT_STUDY_CONCEPTS = (
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:SalesRevenueNet",
    "us-gaap:NetIncomeLoss",
)


def _run_study(args) -> dict:
    from .study import facts_from_jsonl, load_universe, run_study

    names = load_universe(args.universe) if args.universe else {}
    options = dict(concepts=args.concepts or list(DEFAULT_STUDY_CONCEPTS), signal_concept=args.signal_concept,
                   start=args.start, end=args.end, unit=args.unit, names=names,
                   bootstrap_samples=args.bootstrap_samples, seed=args.seed, max_age_days=args.max_age_days)
    if args.facts:
        return run_study(facts_from_jsonl(args.facts), args.output, **options)
    from .store import DuckDBStore
    if not args.db.is_file():
        raise ValueError(f"Database does not exist: {args.db}; import SEC facts first")
    with DuckDBStore(args.db) as store:
        return run_study(store.all_facts(), args.output, bitemporal_retriever=store.retrieve,
                         storage="duckdb", **options)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "demo":
            from .demo import run_demo
            result = run_demo(args.output)
            # Full metrics live in evaluation.json; keep terminal output reviewable.
            result["metrics"] = {
                backend: {key: summary["metrics"][key]["value"] for key in
                          ("accuracy", "answer_leakage_rate_all_queries", "correct_abstention_rate")}
                for backend, summary in result.pop("backends").items()
            }
            _print(result)
            return 0

        if args.command == "study":
            _print(_run_study(args))
            return 0
        if args.command == "plot":
            from .plots import plot_demo, plot_study
            if not args.study and not args.demo:
                raise ValueError("Pass --study and/or --demo")
            written = []
            if args.study:
                written += plot_study(args.study, args.output)
            if args.demo:
                written += plot_demo(args.demo, args.output)
            _print({"figures": written})
            return 0

        from .store import DuckDBStore
        if args.command in {"query", "evaluate", "factor", "export-facts"} and not args.db.is_file():
            raise ValueError(f"Database does not exist: {args.db}; import SEC facts first")
        args.db.parent.mkdir(parents=True, exist_ok=True)

        if args.command == "import-sec":
            reports = []
            with DuckDBStore(args.db) as store:
                for path in args.files:
                    reports.append(_import_file(path, store))
            _dump(args.report, {"imports": reports})
            _print({"database": str(args.db.resolve()), "report": str(args.report.resolve()),
                    "inserted": sum(report["inserted"] for report in reports)})
        elif args.command == "fetch-sec":
            from .models import normalize_cik
            from .sec import SecClient
            ciks = args.cik or [line.split("#", 1)[0].strip()
                                for line in args.ciks_file.read_text().splitlines()
                                if line.split("#", 1)[0].strip()]
            ciks = list(dict.fromkeys(normalize_cik(cik) for cik in ciks))
            if not ciks:
                raise ValueError("No CIKs supplied")
            client = SecClient(args.cache, user_agent=os.environ.get("SEC_USER_AGENT"))
            reports = []
            with DuckDBStore(args.db) as store:
                for cik in ciks:
                    path = client.download_companyfacts(cik, refresh=args.refresh)
                    reports.append(_import_file(Path(path), store))
                    # Retain completed issuer reports if a later download fails.
                    _dump(args.report, {"imports": reports, "requested_ciks": ciks})
            _print({"database": str(args.db.resolve()), "report": str(args.report.resolve()),
                    "companies": len(reports), "inserted": sum(r["inserted"] for r in reports)})
        elif args.command == "query":
            from .models import Query
            from .backends import retrieve
            q = Query("cli", args.cik, args.concept, args.unit, args.as_of, args.period_end)
            with DuckDBStore(args.db) as store:
                result = (store.retrieve(q) if args.backend == "bitemporal"
                          else retrieve(store.all_facts(), q, args.backend))
            _print(asdict(result))
        elif args.command == "evaluate":
            from .evaluation import evaluate, write_evaluation
            with DuckDBStore(args.db) as store:
                facts = store.all_facts()
                if args.concept:
                    facts = [fact for fact in facts if fact.concept == args.concept]
                if not facts:
                    raise ValueError("No matching facts to evaluate")
                report = evaluate(facts, bootstrap_samples=args.bootstrap_samples, seed=args.seed,
                                  bitemporal_retriever=store.retrieve)
            report["dataset_kind"] = "imported_sec_filing_records"
            report["bitemporal_storage"] = "DuckDB"
            report["source_database"] = str(args.db.resolve())
            report["source_snapshots"] = sorted({fact.source_sha256 for fact in facts})
            _print(write_evaluation(report, args.output))
        elif args.command == "export-facts":
            from .models import normalize_cik
            from .study import write_facts_jsonl
            ciks, concepts = {normalize_cik(c) for c in args.cik}, set(args.concept)
            with DuckDBStore(args.db) as store:
                facts = [f for f in store.all_facts()
                         if f.cik in ciks and f.concept in concepts and f.unit == args.unit]
            if not facts:
                raise ValueError("No matching facts to export")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _print({"facts": write_facts_jsonl(facts, args.output), "output": str(args.output)})
        elif args.command == "factor":
            from .factor import evaluate_factor, load_returns
            with DuckDBStore(args.db) as store:
                facts = store.all_facts()
            if not facts:
                raise ValueError("No facts to evaluate")
            report = evaluate_factor(facts, load_returns(args.returns), concept=args.concept,
                                     unit=args.unit, min_entities=args.min_entities,
                                     bootstrap_samples=args.bootstrap_samples, seed=args.seed)
            report["dataset_kind"] = "user_supplied_returns_not_independently_verified"
            report["returns_sha256"] = hashlib.sha256(args.returns.read_bytes()).hexdigest()
            _dump(args.output, report)
            _print({"factor_report": str(args.output.resolve())})
        return 0
    except ImportError as exc:
        print(f"pit-memory: {exc}. Install dependencies with python -m pip install -e '.[dev]'. "
              "The demo can run from source without them.", file=sys.stderr)
        return 2
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"pit-memory: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
