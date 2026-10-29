from dataclasses import replace
import json

import pytest

from pit_memory.backends import BACKENDS, BackendIndex, retrieve
from pit_memory.demo import build_demo, run_demo
from pit_memory.evaluation import EvaluationCase, evaluate, generate_cases, write_evaluation
from pit_memory.models import Fact, Query, Retrieval
from pit_memory.store import DuckDBStore


def sample():
    old = Fact("1", "us-gaap:Revenues", "USD", 100, "2020-01-01", "2020-12-31",
               "2021-02-12", "original", "10-K", "2021-02-16")
    revised = replace(old, value=150, filed="2022-03-01", available_on="2022-03-02",
                      accession="amended", fact_id="")
    return old, revised


def query(as_of="2021-03-01", period_end="2020-12-31"):
    return Query("q", "1", "us-gaap:Revenues", "USD", as_of, period_end)


def test_restatement_and_period_filter_leak_are_distinct():
    old, revised = sample()
    facts = [old, revised]
    assert retrieve(facts, query(), "bitemporal").fact == old
    assert retrieve(facts, query(), "overwrite").fact == revised
    assert retrieve(facts, query(), "period_end").fact == revised
    assert retrieve(facts, query("2021-02-12"), "bitemporal").fact is None
    index = BackendIndex(facts)
    assert old not in index.overwritten
    assert revised in index.overwritten


def test_later_restatement_of_old_year_does_not_replace_latest_year():
    old, revised = sample()
    latest = replace(old, period_start="2021-01-01", period_end="2021-12-31", value=200,
                     filed="2022-02-01", available_on="2022-02-02", accession="next", fact_id="")
    assert retrieve([old, revised, latest], query("2022-03-03", None)).fact == latest


def test_context_leak_exposure_can_exist_without_answer_leak():
    old, revised = sample()
    q = query()
    case = EvaluationCase(q, 100, old.fact_id, expected_period_end=old.period_end)
    report = evaluate([old, revised], [case], bootstrap_samples=10)
    trace = next(row for row in report["traces"] if row["backend"] == "vector")
    assert trace["context_leaked"] is True
    assert len(trace["context"]) == 2
    pit = report["backends"]["bitemporal"]["metrics"]
    assert pit["accuracy"]["value"] == 1
    assert pit["answer_leakage_rate_answered"]["value"] == 0
    assert pit["correct_abstention_rate"]["value"] is None
    assert pit["accuracy"]["ci95"] is None  # One issuer is not independent replication.


def test_ambiguity_and_scope_filters():
    old, _ = sample()
    clash = replace(old, value=101, accession="clash", fact_id="")
    unrelated = replace(old, cik="2", value=999, fact_id="")
    result = retrieve([old, clash, unrelated], query(), "bitemporal", top_k=1)
    assert result.status == "ambiguous" and result.fact is None
    assert {item.value for item in result.context} == {100, 101}
    with pytest.raises(ValueError):
        retrieve([old], query(), top_k=0)


def test_case_generation_has_latest_and_exact_boundary_truth():
    old, revised = sample()
    cases = generate_cases([old, revised])
    same_day = [case for case in cases if case.query.as_of == old.filed]
    assert {case.query.period_end for case in same_day} == {None, "2020-12-31"}
    assert all(case.expected_status == "not_known" for case in same_day)
    assert any(case.expected_value == revised.value for case in cases)
    assert len({case.query.query_id for case in cases}) == len(cases)


def test_hand_labelled_demo_matches_duckdb_and_independent_retriever(tmp_path):
    data = build_demo(3)
    for case in data.cases:
        actual = retrieve(data.facts, case.query)
        assert actual.status == case.expected_status
        assert (actual.fact.fact_id if actual.fact else None) == case.expected_fact_id
    report = evaluate(data.facts, data.cases, bootstrap_samples=20)
    pit = report["backends"]["bitemporal"]["metrics"]
    assert pit["accuracy"]["value"] == 1
    assert pit["accuracy"]["ci95"] == [1, 1]
    assert pit["context_leakage_rate"]["value"] == 0
    assert pit["correct_abstention_rate"]["value"] == 1
    assert report["backends"]["overwrite"]["metrics"]["answer_leakage_rate_all_queries"]["value"] > 0
    paths = write_evaluation(report, tmp_path / "results")
    assert len([json.loads(line) for line in open(paths["retrievals"])]) == len(data.cases) * len(BACKENDS)
    assert "traces" not in json.loads(open(paths["evaluation"]).read())


def test_empty_report_denominators_and_invalid_cases():
    report = evaluate([], [], bootstrap_samples=3)
    assert all(row["metrics"]["accuracy"]["value"] is None for row in report["backends"].values())
    with pytest.raises(ValueError):
        EvaluationCase(query(), None)
    case = EvaluationCase(query(), None, expected_status="not_known")
    with pytest.raises(ValueError):
        evaluate([], [case, case])


def test_demo_artifacts_are_labeled_and_refuse_dataset_mixing(tmp_path):
    report = run_demo(tmp_path, issuers=3, bootstrap_samples=3)
    assert json.loads(open(report["paths"]["factor"]).read())["synthetic"] is True
    assert json.loads(open(report["paths"]["evaluation"]).read())["dataset"]["synthetic"] is True
    with pytest.raises(ValueError, match="different dataset"):
        run_demo(tmp_path, issuers=4, bootstrap_samples=3)


def test_duckdb_evaluation_uses_injected_retriever(tmp_path):
    pytest.importorskip("duckdb")
    data = build_demo(3)
    with DuckDBStore(tmp_path / "audit.duckdb") as store:
        store.append(data.facts)
        calls = []
        def retrieve_from_db(query):
            calls.append(query.query_id)
            return store.retrieve(query)
        report = evaluate(data.facts, data.cases, bootstrap_samples=0,
                          bitemporal_retriever=retrieve_from_db)
        assert len(calls) == len(data.cases)
        assert report["bitemporal_implementation"] == "provided_retriever"
        assert report["backends"]["bitemporal"]["metrics"]["accuracy"]["value"] == 1
        for case in data.cases:
            assert store.retrieve(case.query).fact == retrieve(data.facts, case.query).fact


@pytest.mark.parametrize("change", [
    {"cik": "2"}, {"concept": "us-gaap:NetIncomeLoss"}, {"unit": "EUR"},
    {"period_start": "2019-01-01", "period_end": "2019-12-31"},
])
def test_equal_number_outside_query_scope_is_inaccurate(change):
    old, _ = sample()
    wrong = replace(old, **change, fact_id="")
    # An exact-period query supplies the period even without optional labels.
    case = EvaluationCase(query(), old.value)
    report = evaluate([old], [case], bootstrap_samples=0,
                      bitemporal_retriever=lambda q: Retrieval(q.query_id, "bitemporal", wrong, (wrong,)))
    assert report["backends"]["bitemporal"]["metrics"]["accuracy"]["value"] == 0




# ---- proposed: overwrite snapshot and vector ranking are pinned
def test_overwrite_retrieval_cannot_see_discarded_versions():
    old, revised = sample()
    result = retrieve([old, revised], query(), "overwrite")
    assert result.fact == revised
    assert old not in result.context  # period_end still has it; overwrite must not
    assert old in retrieve([old, revised], query(), "period_end").context


def test_overwrite_snapshot_keeps_same_day_conflicts_ambiguous():
    old, _ = sample()
    clash = replace(old, value=101, accession="clash", fact_id="")
    result = retrieve([old, clash], query(), "overwrite")
    assert result.status == "ambiguous" and result.fact is None
    assert {item.value for item in result.context} == {100, 101}


def test_vector_ranking_follows_hashed_similarity_not_filing_recency():
    old, revised = sample()
    latest = replace(old, period_start="2021-01-01", period_end="2021-12-31", value=200,
                     filed="2022-02-01", available_on="2022-02-02", accession="next", fact_id="")
    facts = [old, revised, latest]
    # Golden order of the deterministic signed-hash baseline. The most recent
    # filing (revised, 2022-03-01) is not ranked first for the 2021 question.
    assert [f.value for f in retrieve(facts, query("2021-03-01", None), "vector").context] == [200, 150, 100]
    assert [f.value for f in retrieve(facts, query("2020-06-01", None), "vector").context] == [150, 100, 200]


# ---- proposed: scoring and denominators
def test_answered_leakage_rate_divides_by_answered_queries():
    old, revised = sample()
    cases = [EvaluationCase(query(), 100, old.fact_id, expected_period_end=old.period_end),
             # The fiscal year has not ended yet: every dated backend abstains.
             EvaluationCase(replace(query("2020-06-01"), query_id="q2"), None, expected_status="not_known")]
    metric = evaluate([old, revised], cases, bootstrap_samples=0)["backends"]["overwrite"]["metrics"]
    assert metric["coverage"]["numerator"] == 1
    assert (metric["answer_leakage_rate_answered"]["numerator"],
            metric["answer_leakage_rate_answered"]["denominator"]) == (1, 1)
    assert metric["answer_leakage_rate_all_queries"]["value"] == 0.5


def test_same_day_filing_in_context_is_context_leakage():
    old, _ = sample()
    case = EvaluationCase(query(old.filed), None, expected_status="not_known")
    report = evaluate([old], [case], bootstrap_samples=0)
    trace = next(row for row in report["traces"] if row["backend"] == "period_end")
    assert trace["context_leaked"] is True  # filed on as_of, available only on the next session
    assert report["backends"]["period_end"]["metrics"]["context_leakage_rate"]["value"] == 1


def test_ambiguous_answer_is_wrong_when_nothing_was_available():
    old, _ = sample()
    clash = replace(old, value=101, accession="clash", fact_id="")
    # On the filing day nothing is available; period_end sees the same-day conflict.
    case = EvaluationCase(query(old.filed), None, expected_status="not_known")
    report = evaluate([old, clash], [case], bootstrap_samples=0)
    trace = next(row for row in report["traces"] if row["backend"] == "period_end")
    assert trace["status"] == "ambiguous"
    assert report["backends"]["period_end"]["metrics"]["accuracy"]["value"] == 0
    assert report["backends"]["bitemporal"]["metrics"]["accuracy"]["value"] == 1


def test_unexpected_abstention_is_not_a_correct_abstention():
    old, _ = sample()
    case = EvaluationCase(query(), 100, old.fact_id, expected_period_end=old.period_end)
    report = evaluate([old], [case], bootstrap_samples=0, bitemporal_retriever=lambda q: Retrieval(
        q.query_id, "bitemporal", None, (), "not_known"))
    metric = report["backends"]["bitemporal"]["metrics"]["correct_abstention_rate"]
    assert (metric["numerator"], metric["denominator"], metric["value"]) == (0, 0, None)


# ---- proposed: issuer-cluster bootstrap and percentile
def _ten_issuer_report(samples=400):
    facts, cases = [], []
    for cik in range(1, 11):
        fact = replace(sample()[0], cik=str(cik), fact_id="")
        facts.append(fact)
        for n, as_of in enumerate(("2021-03-01", "2021-04-01", "2021-05-03")):
            q = Query(f"c{cik}-{n}", fact.cik, fact.concept, fact.unit, as_of, fact.period_end)
            # Half the issuers carry a deliberately wrong label: per-issuer accuracy is 1 or 0.
            cases.append(EvaluationCase(q, 100 if cik <= 5 else 999, expected_period_end=fact.period_end))
    return evaluate(facts, cases, bootstrap_samples=samples, seed=3)


def test_bootstrap_resamples_whole_issuers():
    pit = _ten_issuer_report()["backends"]["bitemporal"]
    assert pit["issuer_clusters"] == 10
    low, high = pit["metrics"]["accuracy"]["ci95"]
    # Mean of 10 fair issuer-level coin flips: 2.5%/97.5% points are 0.2/0.8.
    assert low == pytest.approx(0.2) and high == pytest.approx(0.8)
    # No ground-truth not_known questions: every draw has a zero denominator.
    abstention = pit["metrics"]["correct_abstention_rate"]
    assert abstention["ci95"] is None and abstention["bootstrap_valid_samples"] == 0


def test_percentile_interpolates_between_order_statistics():
    from pit_memory.evaluation import _percentile
    assert _percentile([4, 1, 3, 2], .25) == pytest.approx(1.75)
    assert _percentile([0, 10], .975) == pytest.approx(9.75)
    assert _percentile([], .5) is None


def test_summary_csv_matches_report_intervals(tmp_path):
    import csv
    report = _ten_issuer_report(samples=50)
    paths = write_evaluation(report, tmp_path)
    rows = list(csv.DictReader(open(paths["summary"])))
    row = next(r for r in rows if (r["backend"], r["metric"]) == ("bitemporal", "accuracy"))
    low, high = report["backends"]["bitemporal"]["metrics"]["accuracy"]["ci95"]
    assert low < high
    assert (float(row["ci95_low"]), float(row["ci95_high"])) == (low, high)


# ---- proposed: generated boundary cases
def test_generated_cases_ask_on_the_day_before_availability():
    old, _ = sample()  # filed Friday 2021-02-12; Monday 2021-02-15 is Presidents' Day
    cases = generate_cases([old])
    before = [case for case in cases if case.category.endswith("before_available")]
    assert {case.query.as_of for case in before} == {"2021-02-15"}
    assert all(case.expected_status == "not_known" for case in before)


def test_ambiguity_about_a_year_not_yet_filed_is_not_credited():
    old, _ = sample()
    clash_2020 = replace(old, value=111, accession="clash-2020", fact_id="")
    fy2021 = replace(old, period_start="2021-01-01", period_end="2021-12-31", value=130,
                     filed="2022-02-15", available_on="2022-02-16", accession="fy21", fact_id="")
    clash_2021 = replace(fy2021, value=131, accession="clash-2021", fact_id="")
    # Point in time on 2022-01-03: FY2020 is ambiguous (two values filed 2021-02-12).
    # The naive stores are ambiguous about FY2021, whose 10-K was not filed yet.
    cases = generate_cases([old, clash_2020, fy2021, clash_2021])
    latest = next(c for c in cases if c.query.as_of == "2021-02-16" and c.query.period_end is None)
    assert latest.expected_status == "ambiguous"
    probe = EvaluationCase(Query("probe", "1", "us-gaap:Revenues", "USD", "2022-01-03"),
                           None, expected_status="ambiguous", expected_filed="2021-02-12",
                           expected_period_end="2020-12-31")
    report = evaluate([old, clash_2020, fy2021, clash_2021], [probe], bootstrap_samples=0)
    accuracy = {backend: report["backends"][backend]["metrics"]["accuracy"]["value"] for backend in BACKENDS}
    assert accuracy["bitemporal"] == 1
    assert accuracy["period_end"] == accuracy["overwrite"] == 0


def test_overwrite_snapshot_replaces_a_year_whose_start_date_convention_changed():
    old, _ = sample()
    # The later 10-K dates the same fiscal year from 2019-12-31 instead of 2020-01-01.
    later = replace(old, period_start="2019-12-31", value=120, filed="2022-03-01",
                    available_on="2022-03-02", accession="later", fact_id="")
    index = BackendIndex([old, later])
    assert index.overwritten == (later,)
    result = index.retrieve(query("2023-01-03"), "overwrite")
    assert result.fact == later and old not in result.context
    assert old in index.retrieve(query("2023-01-03"), "period_end").context


@pytest.mark.parametrize("samples", [-1, 1.5, True])
def test_bootstrap_samples_must_be_a_nonnegative_integer_even_with_no_queries(samples):
    with pytest.raises(ValueError, match="nonnegative integer"):
        evaluate([], [], bootstrap_samples=samples)
