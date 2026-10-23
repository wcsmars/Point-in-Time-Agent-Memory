"""Independent checks of the audit's interpretation of evidence."""
from dataclasses import replace

from pit_memory.evaluation import EvaluationCase, evaluate
from pit_memory.models import Fact, Query, Retrieval


def original():
    return Fact("123", "us-gaap:Revenues", "USD", 100,
                "2020-01-01", "2020-12-31", "2021-02-12",
                "original", "10-K", "2021-02-16")


def test_equal_future_value_is_accurate_but_still_leaked():
    old = original()
    later = replace(old, filed="2022-02-15", available_on="2022-02-16",
                    accession="later-comparative", fact_id="")
    query = Query("same-value", old.cik, old.concept, old.unit,
                  "2021-03-01", old.period_end)
    case = EvaluationCase(query, old.value, old.fact_id,
                          expected_period_end=old.period_end)
    report = evaluate([old, later], [case], bootstrap_samples=0)
    trace = next(row for row in report["traces"] if row["backend"] == "overwrite")
    assert trace["correct"] is True
    assert trace["answer_leaked"] is True
    assert trace["future_filing_answer"] is True
    assert trace["context_leaked"] is True


def test_same_day_filing_is_unavailable_but_not_future_filed():
    fact = original()
    query = Query("same-day", fact.cik, fact.concept, fact.unit,
                  fact.filed, fact.period_end)
    case = EvaluationCase(query, None, expected_status="not_known")
    report = evaluate([fact], [case], bootstrap_samples=0)
    trace = next(row for row in report["traces"] if row["backend"] == "period_end")
    assert trace["answer_leaked"] is True
    assert trace["future_filing_answer"] is False
    assert trace["correct"] is False


def test_equal_number_for_wrong_fiscal_year_is_inaccurate():
    old = original()
    newer = replace(old, period_start="2021-01-01", period_end="2021-12-31",
                    filed="2022-02-15", available_on="2022-02-16",
                    accession="new-year", fact_id="")
    query = Query("latest-year", old.cik, old.concept, old.unit, "2022-03-01")
    case = EvaluationCase(query, newer.value, newer.fact_id,
                          expected_period_end=newer.period_end)
    # Deliberately broken injected retriever: value is right, period is wrong.
    report = evaluate([old, newer], [case], bootstrap_samples=0,
                      bitemporal_retriever=lambda q: Retrieval(q.query_id, "pit", old, (old,)))
    trace = next(row for row in report["traces"] if row["backend"] == "bitemporal")
    assert trace["correct"] is False
    assert trace["answer_leaked"] is False
