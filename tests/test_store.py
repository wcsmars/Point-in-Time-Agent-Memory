import importlib.util

import pytest

from pit_memory import DuckDBStore, Fact, Query
from pit_memory.store import select_latest

needs_duckdb = pytest.mark.skipif(importlib.util.find_spec("duckdb") is None, reason="DuckDB dependency unavailable; install .[dev] to exercise storage")


def fact(**changes):
    values = dict(cik="123", concept="us-gaap:Revenues", unit="USD", value=100,
                  period_start="2019-01-01", period_end="2019-12-31", filed="2020-02-14",
                  accession="0000000123-20-000001", form="10-K", available_on="2020-02-18")
    values.update(changes)
    return Fact(**values)


def query(as_of, **changes):
    values = dict(query_id="q1", cik="123", concept="us-gaap:Revenues", unit="USD", as_of=as_of)
    values.update(changes)
    return Query(**values)


def test_date_and_content_validation():
    with pytest.raises(ValueError):
        fact(value=float("inf"))
    with pytest.raises(ValueError):
        fact(available_on="2020-02-14")
    with pytest.raises(ValueError):
        fact(fact_id="arbitrary")
    with pytest.raises(ValueError):
        query("2020-2-14")


@needs_duckdb
def test_immutable_and_idempotent_with_multiple_snapshot_sources(tmp_path):
    original = fact(source_sha256="a" * 64)
    same = fact(source_sha256="b" * 64)
    amendment = fact(value=90, filed="2020-04-01", available_on="2020-04-02", form="10-K/A", accession="0000000123-20-000002")
    with DuckDBStore(tmp_path / "facts.duckdb") as store:
        assert store.append([original, original]) == 1
        assert store.append([same]) == 0
        assert store.append([amendment]) == 1
        assert len(store.all_facts()) == 2
        assert len(store.sources(original.fact_id)) == 2
        assert store.retrieve(query("2020-03-01")).fact.value == 100
        assert store.retrieve(query("2020-04-02")).fact.value == 90


@needs_duckdb
def test_same_day_and_holiday_unavailable():
    with DuckDBStore() as store:
        store.append([fact()])
        assert store.retrieve(query("2020-02-14")).status == "not_known"
        assert store.retrieve(query("2020-02-17")).status == "not_known"
        assert store.retrieve(query("2020-02-18")).fact.value == 100


@needs_duckdb
def test_latest_period_precedes_latest_filing_order():
    with DuckDBStore() as store:
        store.append([fact(), fact(value=120, period_start="2020-01-01", period_end="2020-12-31", filed="2021-02-15", available_on="2021-02-16", accession="0000000123-21-000001"),
                      fact(value=95, filed="2021-03-01", available_on="2021-03-02", accession="0000000123-21-000002")])
        assert store.retrieve(query("2021-03-03")).fact.value == 120
        assert store.retrieve(query("2021-03-03", period_end="2019-12-31")).fact.value == 95


@needs_duckdb
def test_cik_tag_unit_and_requested_period_scope():
    with DuckDBStore() as store:
        store.append([fact(), fact(cik="456", value=999), fact(concept="ifrs-full:Revenue", value=888), fact(unit="EUR", value=777)])
        assert store.retrieve(query("2020-03-01")).fact.value == 100
        assert store.retrieve(query("2020-03-01", period_end="2020-12-31")).status == "not_known"


@needs_duckdb
def test_same_day_conflicts_abstain_without_fabricated_order():
    with DuckDBStore() as store:
        store.append([fact(), fact(value=90, accession="0000000123-20-000002")])
        result = store.retrieve(query("2020-03-01"))
        assert result.status == "ambiguous"
        assert result.fact is None
        assert len(result.context) == 2


@needs_duckdb
def test_same_date_same_value_different_start_also_ambiguous():
    with DuckDBStore() as store:
        store.append([fact(), fact(period_start="2019-01-02", accession="0000000123-20-000002")])
        assert store.retrieve(query("2020-03-01")).status == "ambiguous"


@needs_duckdb
def test_same_date_identical_value_has_stable_citation():
    with DuckDBStore() as store:
        store.append([fact(), fact(accession="0000000123-20-000002")])
        result = store.retrieve(query("2020-03-01"))
        assert result.status == "known"
        assert result.fact.fact_id == min(row.fact_id for row in result.context)


@needs_duckdb
def test_reopen_preserves_history(tmp_path):
    path = tmp_path / "facts.duckdb"
    with DuckDBStore(path) as store:
        store.append([fact()])
    with DuckDBStore(path) as store:
        assert store.retrieve(query("2020-03-01")).fact == fact()


def test_reference_original_amendment_and_availability_boundary():
    facts = [fact(), fact(value=90, filed="2020-04-01", available_on="2020-04-02", accession="0000000123-20-000002")]
    assert select_latest(query("2020-02-17"), facts).status == "not_known"
    assert select_latest(query("2020-02-18"), facts).fact.value == 100
    assert select_latest(query("2020-04-01"), facts).fact.value == 100
    assert select_latest(query("2020-04-02"), facts).fact.value == 90


def test_reference_ambiguous_cutoff_and_scope():
    facts = [fact(), fact(value=90, accession="0000000123-20-000002"), fact(value=999, cik="456")]
    result = select_latest(query("2020-03-01"), facts)
    assert result.status == "ambiguous"
    assert {f.value for f in result.context} == {90, 100}
    assert select_latest(query("2020-03-01", unit="EUR"), facts).status == "not_known"


def test_reference_latest_period_first_then_latest_filed():
    facts = [fact(value=95, filed="2021-03-01", available_on="2021-03-02", accession="0000000123-21-000002"),
             fact(value=120, period_start="2020-01-01", period_end="2020-12-31", filed="2021-02-15", available_on="2021-02-16", accession="0000000123-21-000001")]
    assert select_latest(query("2021-03-03"), facts).fact.value == 120
    assert select_latest(query("2021-03-03", period_end="2019-12-31"), facts).fact.value == 95
