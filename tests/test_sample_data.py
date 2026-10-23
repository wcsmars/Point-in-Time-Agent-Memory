"""The bundled SEC sample: real filing records, audited offline."""
from pathlib import Path

import pytest

from pit_memory.evaluation import evaluate, scheduled_cases
from pit_memory.study import facts_from_jsonl, month_starts, revision_profile

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample-data" / "sec-facts-sample.jsonl"


@pytest.fixture(scope="module")
def facts():
    if not SAMPLE.is_file():
        pytest.skip("sample data not present")
    return facts_from_jsonl(SAMPLE)


def test_sample_rows_carry_sec_provenance(facts):
    assert len({fact.cik for fact in facts}) >= 5
    for fact in facts:
        assert fact.source_url.startswith("https://www.sec.gov/Archives/edgar/data/")
        assert len(fact.source_sha256) == 64
        assert fact.filed < fact.available_on
        assert fact.form in {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}


def test_sample_contains_real_restatements(facts):
    profile = revision_profile(facts)
    assert profile["overall"]["periods_revised"] > 0
    assert profile["overall"]["periods_with_later_filings"] > profile["overall"]["periods_revised"]


def test_bitemporal_reference_never_leaks_on_real_filings(facts):
    cases = scheduled_cases(facts, month_starts("2015-01-01", "2024-12-31"))
    report = evaluate(facts, cases, bootstrap_samples=0)
    clean = report["backends"]["bitemporal"]["metrics"]
    assert clean["accuracy"]["value"] == 1
    assert clean["answer_leakage_rate_all_queries"]["numerator"] == 0
    assert clean["context_leakage_rate"]["numerator"] == 0
    naive = report["backends"]["overwrite"]["metrics"]
    assert naive["answer_leakage_rate_all_queries"]["numerator"] > 0
