"""Command-line entry points for reproducible local research runs."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys

from . import __version__


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


def _dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


DEFAULT_STUDY_CONCEPTS = (
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:SalesRevenueNet",
    "us-gaap:NetIncomeLoss",
)


def _parse_file(path: Path):
    """Read and validate one companyfacts file before anything is written."""
    from .sec import parse_companyfacts

    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: not a companyfacts JSON document ({exc})") from exc
    try:
        return parse_companyfacts(payload, source_sha256=hashlib.sha256(raw).hexdigest())
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _import_parsed(path: Path, parsed, store) -> dict:
    inserted = store.append(parsed.facts)
    return {
        "source": str(path.resolve()), "source_sha256": parsed.source_sha256,
        "accepted": len(parsed.facts), "inserted": inserted,
        "skipped_out_of_scope": parsed.skipped, "rejected": parsed.rejected,
    }


def _import_file(path: Path, store) -> dict:
    return _import_parsed(path, _parse_file(path), store)


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """Show defaults only where there is one."""

    def _get_help_string(self, action):
        if action.default in (None, False) or action.help is None:
            return action.help
        return super()._get_help_string(action)


def parser() -> argparse.ArgumentParser:
    fmt = _HelpFormatter
    p = argparse.ArgumentParser(
        prog="pit-memory", formatter_class=fmt,
        description="Point-in-time SEC fact store and look-ahead leakage audit for research agents")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = p.add_subparsers(dest="command", required=True, metavar="command")

    def command(name: str, text: str) -> argparse.ArgumentParser:
        return commands.add_parser(name, help=text, description=text, formatter_class=fmt)

    demo = command("demo", "Run the labelled, offline synthetic benchmark and factor check")
    demo.add_argument("--output", type=Path, default=Path("runs/demo"), help="output folder")

    imp = command("import-sec", "Append annual facts from downloaded SEC companyfacts JSON files")
    imp.add_argument("files", nargs="+", type=Path, help="companyfacts JSON files (CIK##########.json)")
    imp.add_argument("--db", type=Path, default=Path("data/sec.duckdb"), help="DuckDB database to append to")
    imp.add_argument("--report", type=Path, default=Path("runs/import-report.json"),
                     help="where to write accepted/rejected row counts")

    load = command("import-facts", "Append facts from a JSONL file (e.g. the bundled SEC sample) to DuckDB")
    load.add_argument("files", nargs="+", type=Path, help="facts JSONL files written by export-facts")
    load.add_argument("--db", type=Path, default=Path("data/sec.duckdb"), help="DuckDB database to append to")

    fetch = command("fetch-sec", "Download companyfacts from SEC (needs SEC_USER_AGENT) and import them")
    sources = fetch.add_mutually_exclusive_group(required=True)
    sources.add_argument("--cik", action="append", help="CIK to download; repeat for several issuers")
    sources.add_argument("--ciks-file", type=Path, help="one numeric CIK per line; text after # is ignored")
    fetch.add_argument("--db", type=Path, default=Path("data/sec.duckdb"), help="DuckDB database to append to")
    fetch.add_argument("--cache", type=Path, default=Path("data/raw"), help="folder for raw downloads")
    fetch.add_argument("--report", type=Path, default=Path("runs/fetch-report.json"), help="import report")
    fetch.add_argument("--refresh", action="store_true", help="download again even if a cached copy exists")

    query = command("query", "Retrieve one cited annual value as it was known on a date")
    query.add_argument("--db", type=Path, required=True, help="DuckDB fact database")
    query.add_argument("--cik", required=True, help="SEC Central Index Key, digits only")
    query.add_argument("--concept", required=True, help="namespace-qualified concept, e.g. us-gaap:Revenues")
    query.add_argument("--unit", default="USD", help="reporting unit")
    query.add_argument("--as-of", required=True, help="knowledge date YYYY-MM-DD")
    query.add_argument("--period-end", help="exact fiscal period end YYYY-MM-DD (default: latest annual)")
    query.add_argument("--backend", choices=("bitemporal", "vector", "overwrite", "period_end"),
                       default="bitemporal", help="retrieval design to use")

    audit = command("evaluate", "Audit all backends on boundary questions around every filing date")
    audit.add_argument("--db", type=Path, required=True, help="DuckDB fact database")
    audit.add_argument("--output", type=Path, default=Path("runs/audit"), help="output folder")
    audit.add_argument("--bootstrap-samples", type=int, default=500, help="issuer-cluster bootstrap draws")
    audit.add_argument("--seed", type=int, default=7, help="bootstrap seed")
    audit.add_argument("--concept", help="restrict the audit to one exact concept (recommended)")

    factor = command("factor", "Compare revenue-growth rank IC across backends using a returns CSV you supply")
    factor.add_argument("--db", type=Path, required=True, help="DuckDB fact database")
    factor.add_argument("--returns", type=Path, required=True,
                        help="CSV with columns cik,as_of,forward_return and optional return_end")
    factor.add_argument("--concept", default="us-gaap:Revenues", help="revenue concept for the signal")
    factor.add_argument("--unit", default="USD", help="reporting unit")
    factor.add_argument("--output", type=Path, default=Path("runs/factor.json"), help="report path")
    factor.add_argument("--min-entities", type=int, default=3, help="minimum issuers per date for an IC")
    factor.add_argument("--bootstrap-samples", type=int, default=500, help="date bootstrap draws")
    factor.add_argument("--seed", type=int, default=17, help="bootstrap seed")

    study = command("study", "Revision profile, scheduled leakage audit and signal divergence")
    source = study.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", type=Path, help="DuckDB fact database (point-in-time answers come from SQL)")
    source.add_argument("--facts", type=Path, help="facts JSONL (in-memory reference implementation)")
    study.add_argument("--concept", action="append", dest="concepts",
                       help="concept to include; repeat for several (default: "
                            + ", ".join(DEFAULT_STUDY_CONCEPTS) + ")")
    study.add_argument("--signal-concept", default="us-gaap:Revenues", help="concept for the growth signal")
    study.add_argument("--unit", default="USD", help="reporting unit")
    study.add_argument("--start", default="2010-01-01", help="first question month (YYYY-MM-DD)")
    study.add_argument("--end", default="2026-10-01", help="last question month (YYYY-MM-DD)")
    study.add_argument("--universe", type=Path, help="CIK list with '# name' comments, for labels")
    study.add_argument("--output", type=Path, default=Path("runs/sec-study"), help="output folder")
    study.add_argument("--bootstrap-samples", type=int, default=500, help="issuer-cluster bootstrap draws")
    study.add_argument("--seed", type=int, default=7, help="bootstrap seed")
    study.add_argument("--max-age-days", type=int, default=550,
                       help="skip a question when the point-in-time answer's fiscal year ended longer ago")

    plot = command("plot", "Draw the README figures from study and demo outputs (needs .[plots])")
    plot.add_argument("--study", type=Path, help="output folder of the study command")
    plot.add_argument("--demo", type=Path, help="output folder of the demo command")
    plot.add_argument("--output", type=Path, default=Path("runs/figures"), help="folder for PNG files")

    export = command("export-facts", "Write facts of selected issuers and concepts as JSONL")
    export.add_argument("--db", type=Path, required=True, help="DuckDB fact database")
    export.add_argument("--cik", action="append", required=True, help="CIK to include; repeat for several")
    export.add_argument("--concept", action="append", required=True, help="concept to include; repeat")
    export.add_argument("--unit", default="USD", help="reporting unit")
    export.add_argument("--output", type=Path, required=True, help="JSONL file to write")
    return p


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
    with DuckDBStore(args.db, read_only=True) as store:
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
            parsed = [(path, _parse_file(path)) for path in args.files]  # validate before writing
            reports = []
            with DuckDBStore(args.db) as store:
                for path, result in parsed:
                    reports.append(_import_parsed(path, result, store))
                    _dump(args.report, {"imports": reports})  # kept even if a later file fails
            _print({"database": str(args.db.resolve()), "report": str(args.report.resolve()),
                    "inserted": sum(report["inserted"] for report in reports)})
        elif args.command == "import-facts":
            from .study import facts_from_jsonl
            batches = [facts_from_jsonl(path) for path in args.files]  # validate before writing
            with DuckDBStore(args.db) as store:
                inserted = sum(store.append(batch) for batch in batches)
                total = len(store.all_facts())
            _print({"database": str(args.db), "inserted": inserted, "facts_in_database": total})
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
            with DuckDBStore(args.db, read_only=True) as store:
                result = (store.retrieve(q) if args.backend == "bitemporal"
                          else retrieve(store.all_facts(), q, args.backend))
            _print(asdict(result))
        elif args.command == "evaluate":
            from .evaluation import evaluate, write_evaluation
            with DuckDBStore(args.db, read_only=True) as store:
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
            with DuckDBStore(args.db, read_only=True) as store:
                facts = [f for f in store.all_facts()
                         if f.cik in ciks and f.concept in concepts and f.unit == args.unit]
            if not facts:
                raise ValueError("No matching facts to export")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _print({"facts": write_facts_jsonl(facts, args.output), "output": str(args.output)})
        elif args.command == "factor":
            from .factor import evaluate_factor, load_returns
            with DuckDBStore(args.db, read_only=True) as store:
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
        extra = "plots" if "matplotlib" in str(exc) else "dev"
        print(f"pit-memory: {exc}. Install it with python -m pip install -e '.[{extra}]'. "
              "The demo also runs from source without DuckDB.", file=sys.stderr)
        return 2
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"pit-memory: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # database engine errors are not OSError subclasses
        if type(exc).__module__.split(".")[0].lstrip("_") != "duckdb":
            raise
        print(f"pit-memory: database error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
