"""Real-data study helpers, checked on small hand-built filing histories."""
import gzip
import json
from dataclasses import replace

import pytest

from pit_memory.cli import main
from pit_memory.evaluation import EvaluationCase, evaluate, scheduled_cases
from pit_memory.models import Fact, Query
from pit_memory.study import (facts_from_jsonl, load_universe, month_starts, revision_profile,
                              run_study, signal_divergence, write_facts_jsonl)

CONCEPT = "us-gaap:Revenues"


def filing(cik, year, value, filed, available, accession):
    return Fact(str(cik), CONCEPT, "USD", value, f"{year}-01-01", f"{year}-12-31",
                filed, accession, "10-K", available)


def history(cik, scale=1.0, restated=None, year2020=110):
    """Three 10-Ks; each repeats the prior year as a comparative, as in SEC data."""
    rows = [
        filing(cik, 2019, 100 * scale, "2020-02-14", "2020-02-18", f"{cik}-20"),
        filing(cik, 2019, 100 * scale, "2021-02-12", "2021-02-16", f"{cik}-21"),
        filing(cik, 2020, year2020 * scale, "2021-02-12", "2021-02-16", f"{cik}-21"),
        filing(cik, 2020, (restated or year2020) * scale, "2022-02-15", "2022-02-16", f"{cik}-22"),
        filing(cik, 2021, 130 * scale, "2022-02-15", "2022-02-16", f"{cik}-22"),
    ]
    return rows


def test_month_starts_round_up_and_include_end():
    assert month_starts("2021-01-15", "2021-04-01") == ["2021-02-01", "2021-03-01", "2021-04-01"]
    assert month_starts("2021-12-01", "2022-01-31") == ["2021-12-01", "2022-01-01"]
    with pytest.raises(ValueError):
        month_starts("2022-01-01", "2021-01-01")


def test_revision_profile_separates_comparatives_from_restatements():
    facts = history(1) + history(2, restated=95)
    profile = revision_profile(facts)
    overall = profile["overall"]
    assert overall["periods"] == 6
    assert overall["periods_with_later_filings"] == 4
    assert overall["periods_revised"] == 1
    assert overall["revised_share_of_refiled_periods"] == pytest.approx(0.25)
    record = next(r for r in profile["records"] if r["revised"])
    assert (record["cik"], record["period_end"]) == ("0000000002", "2020-12-31")
    assert record["original_value"] == 110 and record["first_revision_value"] == 95
    assert record["relative_change"] == pytest.approx(-15 / 110)
    assert record["revision_lag_days"] == 368


def test_conflicting_original_filing_is_excluded_not_called_a_revision():
    base = filing(3, 2020, 50, "2021-02-12", "2021-02-16", "3-a")
    clash = replace(base, value=51, accession="3-b", fact_id="")
    profile = revision_profile([base, clash])
    assert profile["conflicting_original_periods_excluded"] == 1
    assert profile["overall"]["periods"] == 0


def test_scheduled_cases_start_at_first_availability_and_use_the_oracle():
    cases = scheduled_cases(history(1), month_starts("2019-06-01", "2022-03-01"))
    assert cases[0].query.as_of == "2020-03-01"
    by_date = {case.query.as_of: case for case in cases}
    assert by_date["2021-02-01"].expected_value == 100
    assert by_date["2021-03-01"].expected_value == 110
    assert by_date["2021-03-01"].expected_period_end == "2020-12-31"
    assert by_date["2022-03-01"].expected_value == 130
    assert all(case.query.period_end is None for case in cases)


def test_leaked_and_incorrect_ignores_an_equal_valued_future_comparative():
    rows = history(1)
    query = Query("q", "1", CONCEPT, "USD", "2020-06-01", "2019-12-31")
    case = EvaluationCase(query, 100.0, expected_period_end="2019-12-31")
    report = evaluate(rows, [case], bootstrap_samples=0)
    overwrite = report["backends"]["overwrite"]["metrics"]
    assert overwrite["answer_leakage_rate_all_queries"]["value"] == 1
    assert overwrite["leaked_and_incorrect_rate"]["value"] == 0


def test_signal_divergence_flags_restated_growth_only_for_naive_backends():
    facts = []
    for cik, restated in ((1, None), (2, 80), (3, None), (4, 140), (5, None)):
        facts += history(cik, scale=cik, restated=restated, year2020=100 + 7 * cik)
    # FY2021 has ended but is not filed yet: only the period-end backend uses it.
    result = signal_divergence(facts, ["1", "2", "3", "4", "5"], ["2022-01-15"], CONCEPT, min_entities=3)
    clean = result["summary"]["bitemporal"]
    assert clean["changed_vs_bitemporal"] == 0
    assert clean["signals_using_unavailable_facts"] == 0
    assert clean["mean_rank_correlation_with_bitemporal"] == pytest.approx(1)
    leaky = result["summary"]["period_end"]
    assert leaky["common_with_bitemporal"] == 5
    assert leaky["changed_vs_bitemporal"] == 5
    assert leaky["signals_using_unavailable_facts"] == 5


def test_run_study_writes_evidence_and_bitemporal_never_leaks(tmp_path):
    facts = []
    for cik, restated in ((1, None), (2, 80), (3, None), (4, 140), (5, 120), (6, None)):
        facts += history(cik, scale=cik, restated=restated)
    paths = run_study(facts, tmp_path, concepts=[CONCEPT], signal_concept=CONCEPT,
                      start="2019-01-01", end="2022-12-31", names={"0000000002": "Two Corp"},
                      bootstrap_samples=20)
    study = json.loads((tmp_path / "study.json").read_text())
    assert study["universe"]["issuers"] == 6
    assert study["revisions"]["overall"]["periods_revised"] == 3
    clean = study["leakage"]["bitemporal"]
    assert clean["accuracy"]["value"] == 1
    assert clean["answer_leakage_rate_all_queries"]["value"] == 0
    assert study["leakage"]["period_end"]["answer_leakage_rate_all_queries"]["value"] > 0
    assert study["example_revision"]["cik"] in {"0000000002", "0000000004", "0000000005"}
    with gzip.open(paths["retrievals"], "rt") as handle:
        assert sum(1 for _ in handle) == 4 * study["leakage_queries"]
    assert "Two Corp" in (tmp_path / "revisions.csv").read_text()


def test_facts_jsonl_round_trip_and_cli_study(tmp_path, capsys):
    facts = []
    for cik in range(1, 7):
        facts += history(cik, scale=cik, restated=90 if cik % 2 else None)
    path = tmp_path / "facts.jsonl"
    assert write_facts_jsonl(facts, path) == len(facts)
    assert {f.fact_id for f in facts_from_jsonl(path)} == {f.fact_id for f in facts}
    universe = tmp_path / "universe.txt"
    universe.write_text("# header\n1  # One\n2 # Two\n")
    assert load_universe(universe) == {"0000000001": "One", "0000000002": "Two"}
    out = tmp_path / "study"
    assert main(["study", "--facts", str(path), "--concept", CONCEPT, "--signal-concept", CONCEPT,
                 "--start", "2019-01-01", "--end", "2022-12-31", "--universe", str(universe),
                 "--output", str(out), "--bootstrap-samples", "10"]) == 0
    assert json.loads(capsys.readouterr().out)["study"].endswith("study.json")
    pytest.importorskip("matplotlib")
    assert main(["plot", "--study", str(out), "--output", str(tmp_path / "fig")]) == 0
    written = json.loads(capsys.readouterr().out)["figures"]
    assert any(name.endswith("sec_leakage_by_backend.png") for name in written)


def test_invalid_jsonl_names_the_line(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"cik": "1"}\n')
    with pytest.raises(ValueError, match="bad.jsonl:1"):
        facts_from_jsonl(path)


def test_stale_series_are_dropped_from_schedule_and_signals():
    rows = history(1)  # last fiscal year ends 2021-12-31
    dates = month_starts("2022-03-01", "2024-03-01")
    kept = scheduled_cases(rows, dates, max_answer_age_days=550)
    assert max(case.query.as_of for case in kept) == "2023-07-01"
    assert len(scheduled_cases(rows, dates)) == len(dates)
    facts = []
    for cik in range(1, 7):
        facts += history(cik, scale=cik, year2020=100 + 7 * cik)
    bounded = signal_divergence(facts, [str(c) for c in range(1, 7)], dates, CONCEPT,
                                min_entities=3, max_age_days=550)
    assert bounded["stale_issuer_dates_excluded"] == 6 * 8  # 2023-08-01 .. 2024-03-01
    assert bounded["summary"]["bitemporal"]["signals"] == 6 * 17


def test_empty_schedule_is_a_clear_error(tmp_path):
    with pytest.raises(ValueError, match="no first-of-month"):
        run_study(history(1), tmp_path, concepts=[CONCEPT], signal_concept=CONCEPT,
                  start="2021-01-15", end="2021-01-31")


def test_revision_record_names_both_filings():
    record = next(r for r in revision_profile(history(2, restated=95))["records"] if r["revised"])
    assert record["original_accession"] == "2-21"
    assert record["first_revision_accession"] == "2-22"
    assert record["days_period_end_to_first_filing"] == 43


def test_start_date_conventions_of_one_fiscal_year_are_one_period():
    a = filing(7, 2020, 50, "2021-02-12", "2021-02-16", "7-a")
    b = replace(a, period_start="2019-12-31", filed="2022-02-15", available_on="2022-02-16",
                accession="7-b", fact_id="")
    profile = revision_profile([a, b])
    assert profile["overall"]["periods"] == 1
    assert profile["overall"]["periods_revised"] == 0
    assert profile["records"][0]["start_dates_seen"] == 2


def test_leak_breakdown_classifies_each_leaked_answer():
    from pit_memory.study import leak_breakdown
    rows = history(1, restated=95)
    cases = scheduled_cases(rows, ["2021-02-01", "2021-03-01", "2022-01-15"])
    report = evaluate(rows, cases, bootstrap_samples=0)
    counts = leak_breakdown(report["traces"])["period_end"]
    # 2021-02-01: FY2020 not filed yet; 2021-03-01: FY2020 restated value from 2022;
    # 2022-01-15: FY2021 not filed yet.
    assert counts["leaked_answers"] == 3
    assert counts["different_fiscal_period"] == 2
    assert counts["restated_value_same_period"] == 1
    assert leak_breakdown(report["traces"])["bitemporal"]["leaked_answers"] == 0
