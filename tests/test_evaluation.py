from dataclasses import replace
import json

import pytest

from pit_memory.backends import BACKENDS, BackendIndex, retrieve
from pit_memory.demo import build_demo, run_demo
from pit_memory.evaluation import EvaluationCase, evaluate, generate_cases, write_evaluation
from pit_memory.models import Fact, Query
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
