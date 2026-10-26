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


def test_stale_ambiguous_period_is_excluded_consistently():
    rows = history(1)
    clash = replace(rows[-1], value=131, accession="clash", fact_id="")
    rows.append(clash)
    dates = ["2022-03-01", "2024-03-01"]
    unbounded = scheduled_cases(rows, dates)
    assert all(case.expected_status == "ambiguous" for case in unbounded)
    assert all(case.expected_period_end == "2021-12-31" for case in unbounded)
    assert len(scheduled_cases(rows, dates, max_answer_age_days=550)) == 1
    divergence = signal_divergence(rows, ["1"], dates, CONCEPT, max_age_days=550)
    assert divergence["stale_issuer_dates_excluded"] == 1


def test_conflicting_later_filing_does_not_report_an_arbitrary_revision():
    rows = history(1)
    # The same later filing date both repeats and changes FY2020. There is no
    # unique revision value, so fact hash order cannot resolve this period.
    rows.append(replace(rows[-2], value=95, accession="clash", fact_id=""))
    profile = revision_profile(rows)
    assert profile["conflicting_later_periods_excluded"] == 1
    assert profile["conflicting_original_periods_excluded"] == 0
    assert profile["overall"]["periods"] == 2
    assert profile["overall"]["periods_revised"] == 0


@pytest.mark.parametrize("content", ["null", "[]", "42", "{", '{"cik": "1"}'])
def test_jsonl_bad_shape_names_the_line(tmp_path, content):
    path = tmp_path / "bad-shape.jsonl"
    path.write_text("\n" + content + "\n")
    with pytest.raises(ValueError, match="bad-shape.jsonl:2"):
        facts_from_jsonl(path)


def test_invalid_study_does_not_overwrite_existing_evidence(tmp_path):
    revisions = tmp_path / "revisions.csv"
    revisions.write_text("previous verified report\n")
    with pytest.raises(ValueError, match="no scheduled question"):
        run_study(history(1), tmp_path, concepts=[CONCEPT], signal_concept=CONCEPT,
                  start="2018-01-01", end="2018-12-31")
    assert revisions.read_text() == "previous verified report\n"


def test_month_starts_supports_the_last_iso_year():
    assert month_starts("9999-12-01", "9999-12-31") == ["9999-12-01"]
    assert month_starts("9999-12-15", "9999-12-31") == []


@pytest.mark.parametrize("age", [-1, 1.5, True])
def test_invalid_age_bounds_are_rejected_even_with_empty_data(age):
    with pytest.raises(ValueError, match="nonnegative integer"):
        scheduled_cases([], [], max_answer_age_days=age)
    with pytest.raises(ValueError, match="nonnegative integer"):
        signal_divergence([], [], [], CONCEPT, max_age_days=age)


def test_leak_breakdown_distinguishes_holiday_wait_from_same_day_filing():
    from pit_memory.study import leak_breakdown
    rows = history(1)
    # The 2021 comparative repeats FY2019's already-known value, but is not
    # available until Tuesday after Presidents' Day.
    query = Query("holiday", "1", CONCEPT, "USD", "2021-02-14", "2019-12-31")
    case = EvaluationCase(query, 100, expected_period_end="2019-12-31")
    report = evaluate(rows, [case], bootstrap_samples=0)
    counts = leak_breakdown(report["traces"])["period_end"]
    assert counts["right_value_before_availability"] == 1
    assert counts["right_value_same_day_filing"] == 0
    assert sum(value for key, value in counts.items() if key != "leaked_answers") == counts["leaked_answers"]




# ---- proposed: revision definition and summary statistics
def test_any_reported_change_is_a_revision_but_float_noise_is_not():
    rows = []
    # 1e-15 relative is floating-point noise; 0.5 ppm (rounding in a later 10-K) is a change.
    for year, later_value in ((2018, 1_000_000.0 * (1 + 1e-15)), (2019, 1_000_000.5)):
        rows.append(filing(8, year, 1_000_000.0, f"{year + 1}-02-14", f"{year + 1}-02-19", f"8-{year}-a"))
        rows.append(filing(8, year, later_value, f"{year + 2}-02-14", f"{year + 2}-02-18", f"8-{year}-b"))
    profile = revision_profile(rows)
    assert profile["overall"]["periods"] == 2
    assert profile["overall"]["periods_revised"] == 1
    record = next(r for r in profile["records"] if r["revised"])
    assert record["period_end"] == "2019-12-31"
    assert record["relative_change"] == pytest.approx(5e-7)


def test_revision_summary_statistics_and_first_revision_fields():
    rows = [
        # Issuer 1, FY2018: two successive revisions; the first one is reported.
        filing(1, 2018, 100, "2019-02-14", "2019-02-15", "1-a"),
        filing(1, 2018, 90, "2020-02-14", "2020-02-18", "1-b"),
        filing(1, 2018, 80, "2021-02-12", "2021-02-16", "1-c"),
        # Issuer 1, FY2019: +0.5%; issuer 2: +50% two years later; issuer 3: +2%.
        filing(1, 2019, 100, "2020-02-14", "2020-02-18", "1-d"),
        filing(1, 2019, 100.5, "2021-02-12", "2021-02-16", "1-e"),
        filing(2, 2019, 100, "2020-02-14", "2020-02-18", "2-a"),
        filing(2, 2019, 150, "2022-02-14", "2022-02-15", "2-b"),
        filing(3, 2019, 100, "2020-02-14", "2020-02-18", "3-a"),
        filing(3, 2019, 102, "2021-02-12", "2021-02-16", "3-b"),
    ]
    profile = revision_profile(rows)
    first = next(r for r in profile["records"] if (r["cik"], r["period_end"]) == ("0000000001", "2018-12-31"))
    assert (first["first_revision_value"], first["first_revision_filed"]) == (90, "2020-02-14")
    assert first["revision_lag_days"] == 365
    overall = profile["overall"]
    assert overall["periods_revised"] == 4
    assert overall["issuers_with_a_revision"] == 3
    # Sorted |changes|: 0.005, 0.02, 0.1, 0.5; p90 interpolates at rank 2.7.
    assert overall["median_abs_relative_change"] == pytest.approx(0.06)
    assert overall["p90_abs_relative_change"] == pytest.approx(0.1 + 0.7 * 0.4)
    assert overall["share_of_revisions_above_1pct"] == pytest.approx(0.75)
    # Lags 365, 364, 731, 364 days.
    assert overall["median_revision_lag_days"] == pytest.approx(364.5)


# ---- proposed: signal-divergence bookkeeping
def test_signal_divergence_counts_invented_and_missing_signals():
    facts = []
    for cik, restated in ((1, None), (2, 80), (3, None), (4, 140), (5, None)):
        facts += history(cik, scale=cik, restated=restated, year2020=100 + 7 * cik)
    # Issuers 6 and 8: no FY2019, so no point-in-time growth on 2022-01-15, but the
    # period-end backend already uses the unfiled FY2021 and FY2020 as base.
    for cik in (6, 8):
        facts += [filing(cik, 2020, 100 * cik, "2021-02-12", "2021-02-16", f"{cik}-21"),
                  filing(cik, 2021, 110 * cik, "2022-02-15", "2022-02-16", f"{cik}-22")]
    # Issuer 7: point-in-time growth exists, but FY2021 has a same-day conflict.
    facts += history(7, scale=7)
    facts.append(replace(facts[-1], value=1, accession="7-clash", fact_id=""))
    result = signal_divergence(facts, [str(c) for c in range(1, 9)], ["2022-01-15"], CONCEPT, min_entities=6)
    leaky = result["summary"]["period_end"]
    assert leaky["common_with_bitemporal"] == 5
    assert leaky["signals_without_bitemporal_counterpart"] == 2
    assert leaky["bitemporal_signals_missing"] == 1
    assert leaky["changed_share"] == pytest.approx(leaky["changed_vs_bitemporal"] / 5)
    assert leaky["dates_with_rank_correlation"] == 0  # 5 common issuers < min_entities


def test_unavailable_fact_count_ignores_context_only_exposure():
    facts = []
    for cik in range(1, 4):
        facts += [filing(cik, 2018, 90 * cik, "2019-02-14", "2019-02-15", f"{cik}-18"),
                  filing(cik, 2019, 100 * cik, "2020-02-14", "2020-02-18", f"{cik}-19"),
                  filing(cik, 2020, 110 * cik, "2021-02-12", "2021-02-16", f"{cik}-20"),
                  # FY2018 restated after the question date: retrieved as context only.
                  filing(cik, 2018, 95 * cik, "2021-06-01", "2021-06-02", f"{cik}-18r")]
    result = signal_divergence(facts, ["1", "2", "3"], ["2021-03-01"], CONCEPT, min_entities=3)
    leaky = result["summary"]["period_end"]
    assert leaky["signals"] == 3
    assert leaky["changed_vs_bitemporal"] == 0
    assert leaky["signals_using_unavailable_facts"] == 0


# ---- proposed: schedule boundaries
def test_schedule_skips_dates_between_filing_and_availability():
    # Filed Friday 2020-02-28; the first XNYS session after it is Monday 2020-03-02.
    rows = [filing(1, 2019, 100, "2020-02-28", "2020-03-02", "1-20")]
    cases = scheduled_cases(rows, ["2020-03-01", "2020-03-02", "2020-04-01"])
    assert [case.query.as_of for case in cases] == ["2020-03-02", "2020-04-01"]
    assert all(case.expected_status == "known" for case in cases)


def test_age_bound_keeps_answers_exactly_max_age_days_old():
    rows = history(1)  # FY2021 ends 2021-12-31; 2023-07-04 is 550 days later
    kept = scheduled_cases(rows, ["2023-07-04", "2023-07-05"], max_answer_age_days=550)
    assert [case.query.as_of for case in kept] == ["2023-07-04"]
    divergence = signal_divergence(rows, ["1"], ["2023-07-04", "2023-07-05"], CONCEPT, max_age_days=550)
    assert divergence["stale_issuer_dates_excluded"] == 1


# ---- proposed: run_study wiring
def test_run_study_filters_unit_applies_age_bound_and_charts_largest_revision(tmp_path):
    facts = []
    for cik, restated in ((1, None), (2, 80), (3, None), (4, 150), (5, 120), (6, None)):
        facts += history(cik, scale=cik, restated=restated)
    facts.append(replace(facts[0], unit="EUR", value=1.0, fact_id=""))
    run_study(facts, tmp_path, concepts=[CONCEPT], signal_concept=CONCEPT,
              start="2019-01-01", end="2024-12-31", bootstrap_samples=0)
    study = json.loads((tmp_path / "study.json").read_text())
    usd = [fact for fact in facts if fact.unit == "USD"]
    assert study["facts"] == len(usd)
    expected = scheduled_cases(usd, month_starts("2019-01-01", "2024-12-31"), max_answer_age_days=550)
    assert study["leakage_queries"] == len(expected)
    divergence = json.loads((tmp_path / "signal_divergence.json").read_text())
    assert divergence["stale_issuer_dates_excluded"] > 0
    # |150/110 - 1| = 0.36 is the largest revision (issuer 2: 0.27, issuer 5: 0.09).
    assert study["example_revision"]["cik"] == "0000000004"


# ---- proposed: leak breakdown same-day branch
def test_leak_breakdown_counts_same_day_filing():
    from pit_memory.study import leak_breakdown
    rows = history(1)
    # 2021-02-12 is the day the 2021 10-K repeats FY2019's value of 100.
    query = Query("same-day", "1", CONCEPT, "USD", "2021-02-12", "2019-12-31")
    report = evaluate(rows, [EvaluationCase(query, 100, expected_period_end="2019-12-31")], bootstrap_samples=0)
    counts = leak_breakdown(report["traces"])["period_end"]
    assert counts["right_value_same_day_filing"] == 1
    assert counts["right_value_later_filing"] == 0


def test_signal_concept_outside_the_study_concepts_is_refused(tmp_path, capsys):
    rows = history(1) + history(2) + history(3)
    with pytest.raises(ValueError, match="signal concept"):
        run_study(rows, tmp_path / "lib", concepts=["us-gaap:NetIncomeLoss"], signal_concept=CONCEPT,
                  start="2019-01-01", end="2024-12-31", bootstrap_samples=0)
    path = tmp_path / "facts.jsonl"
    write_facts_jsonl(rows, path)
    assert main(["study", "--facts", str(path), "--concept", "us-gaap:NetIncomeLoss",
                 "--output", str(tmp_path / "cli")]) == 2
    assert "--signal-concept" in capsys.readouterr().err
    assert not (tmp_path / "cli").exists()


def test_unbounded_schedule_is_described_without_a_bound(tmp_path):
    rows = history(1) + history(2) + history(3)
    run_study(rows, tmp_path, concepts=[CONCEPT], signal_concept=CONCEPT, start="2019-01-01",
              end="2024-12-31", bootstrap_samples=0, max_age_days=None)
    audit = json.loads((tmp_path / "leakage" / "evaluation.json").read_text())
    assert "None" not in audit["dataset"]["query_distribution"]
    assert "no bound" in audit["dataset"]["query_distribution"]


def test_p90_of_a_small_group_is_never_below_its_median():
    rows = [filing(1, 2019, 100, "2020-02-14", "2020-02-18", "1-a"),
            filing(1, 2019, 110, "2021-02-12", "2021-02-16", "1-b"),
            filing(2, 2019, 100, "2020-02-14", "2020-02-18", "2-a"),
            filing(2, 2019, 150, "2021-02-12", "2021-02-16", "2-b")]
    overall = revision_profile(rows)["overall"]
    assert overall["median_abs_relative_change"] == pytest.approx(0.3)
    assert overall["p90_abs_relative_change"] == pytest.approx(0.46)


def test_revision_record_reports_the_final_filing():
    rows = [filing(1, 2018, 100, "2019-02-14", "2019-02-15", "1-a"),
            filing(1, 2018, 90, "2020-02-14", "2020-02-18", "1-b"),
            filing(1, 2018, 80, "2021-02-12", "2021-02-16", "1-c")]
    record = revision_profile(rows)["records"][0]
    assert (record["first_revision_value"], record["final_value"], record["final_filed"]) == (90, 80, "2021-02-12")


def test_leak_breakdown_keeps_unexpected_answers_in_other():
    from pit_memory.study import leak_breakdown

    def trace(expected_status, expected_period_end):
        return {"backend": "period_end", "answer_leaked": True, "correct": False,
                "expected_status": expected_status, "expected_period_end": expected_period_end,
                "query": {"as_of": "2021-02-01"},
                "answer": {"period_end": "2020-12-31", "filed": "2021-02-12"}}

    # Nothing was available yet, or the right year was ambiguous: neither is a
    # different fiscal year nor a restatement of a known value.
    counts = leak_breakdown([trace("not_known", None), trace("ambiguous", "2020-12-31")])["period_end"]
    assert counts["other"] == 2
    assert counts["different_fiscal_period"] == counts["restated_value_same_period"] == 0


def test_timeline_shows_no_value_on_an_ambiguous_date():
    from pit_memory.study import knowledge_timeline
    rows = history(1)
    rows.append(replace(rows[0], value=101, accession="1-20-clash", fact_id=""))  # same-day conflict
    timeline = knowledge_timeline(rows, "1", CONCEPT, "USD", "2019-12-31", ["2020-03-01"])
    assert timeline[0]["bitemporal"] is None  # ambiguous: no value, not the first candidate
    assert timeline[0]["period_end"] == 100  # the later comparative settles it (a leak)
