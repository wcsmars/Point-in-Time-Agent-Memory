import importlib.util

import pytest

from pit_memory import DuckDBStore, Fact, Query
from pit_memory.store import select_latest
from pit_memory.backends import BackendIndex

needs_duckdb = pytest.mark.skipif(importlib.util.find_spec("duckdb") is None, reason="DuckDB dependency unavailable; install the package to exercise storage")


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


@pytest.mark.parametrize("changes", [
    {"value": 10**1000}, {"source_url": None}, {"source_sha256": None},
    {"source_sha256": "not-a-hash"}, {"cik": "00000000123"},
])
def test_invalid_numeric_and_provenance_fields_are_validation_errors(changes):
    with pytest.raises(ValueError):
        fact(**changes)


@pytest.mark.parametrize("top_k", [True, 1.5, "5", None, 0, -1])
def test_backend_rejects_invalid_context_size(top_k):
    with pytest.raises(ValueError, match="positive integer"):
        BackendIndex([fact()]).retrieve(query("2020-03-01"), "bitemporal", top_k=top_k)


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


@needs_duckdb
@pytest.mark.parametrize("name, content", [("t.json", '{"a": 1}\n'), ("t.csv", "a,b\n1,2\n"), ("t.bin", "x" * 64)])
def test_non_duckdb_files_are_refused_instead_of_silently_opened_in_memory(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content)
    with pytest.raises(ValueError, match="not a DuckDB database"):
        DuckDBStore(path)
    assert path.read_text() == content


@needs_duckdb
def test_directory_and_missing_read_only_database_are_clear_errors(tmp_path):
    with pytest.raises(ValueError, match="directory"):
        DuckDBStore(tmp_path)
    with pytest.raises(ValueError, match="does not exist"):
        DuckDBStore(tmp_path / "missing.duckdb", read_only=True)
    assert not (tmp_path / "missing.duckdb").exists()


@needs_duckdb
def test_read_only_readers_share_a_database_and_cannot_write(tmp_path):
    path = tmp_path / "facts.duckdb"
    with DuckDBStore(path) as store:
        store.append([fact()])
    with DuckDBStore(path, read_only=True) as first, DuckDBStore(path, read_only=True) as second:
        assert first.retrieve(query("2020-03-01")).fact == second.retrieve(query("2020-03-01")).fact
        with pytest.raises(Exception):
            first.append([fact(value=1, accession="0000000123-20-000009")])


@needs_duckdb
def test_incompatible_database_is_not_partially_modified_or_left_open(tmp_path):
    import duckdb
    path = tmp_path / "incompatible.duckdb"
    with duckdb.connect(str(path)) as connection:
        # Enough columns to create the lookup index, but not a fact table.
        connection.execute("CREATE TABLE facts (cik VARCHAR, concept VARCHAR, unit VARCHAR, available_on VARCHAR)")
    with pytest.raises(RuntimeError, match="Could not open") as error:
        DuckDBStore(path)
    # Keep the exception/traceback alive: failed initialization must explicitly
    # close its connection rather than depend on garbage collection for locks.
    assert error.value is not None
    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SHOW TABLES").fetchall() == [("facts",)]
    with pytest.raises(RuntimeError, match="Could not open"):
        DuckDBStore(path, read_only=True)


@needs_duckdb
def test_failed_source_link_write_rolls_back_facts_and_keeps_store_usable():
    import duckdb
    with DuckDBStore() as store:
        original = fact(source_sha256="a" * 64)
        store.append([original])
        # Simulate a damaged provenance schema after opening. The facts INSERT
        # succeeds first, then the source INSERT fails and must roll it back.
        store.connection.execute("ALTER TABLE fact_sources ADD COLUMN extra VARCHAR")
        with pytest.raises(duckdb.Error):
            store.append([fact(value=110, accession="new")])
        assert store.all_facts() == [original]
        store.connection.execute("ALTER TABLE fact_sources DROP COLUMN extra")
        assert store.append([fact(value=110, accession="new")]) == 1


@needs_duckdb
def test_scoped_facts_give_comparison_backends_the_same_answers_as_all_facts():
    facts = [fact(), fact(value=90, filed="2021-02-12", available_on="2021-02-16", accession="0000000123-21-000002"),
             fact(value=95, period_start="2020-01-01", period_end="2020-12-31", filed="2021-02-12",
                  available_on="2021-02-16", accession="0000000123-21-000002"),
             fact(cik="456"), fact(concept="us-gaap:NetIncomeLoss"), fact(unit="EUR")]
    with DuckDBStore() as store:
        store.append(facts)
        scoped = store.scoped_facts("123", "us-gaap:Revenues", "USD")
        assert scoped == [f for f in store.all_facts()
                          if (f.cik, f.concept, f.unit) == ("0000000123", "us-gaap:Revenues", "USD")]
        assert len(scoped) == 3
        whole, subset = BackendIndex(store.all_facts()), BackendIndex(scoped)
        for as_of in ("2020-02-14", "2020-03-01", "2021-02-16", "2022-01-01"):
            for period in (None, "2019-12-31"):
                q = query(as_of, period_end=period)
                for backend in ("vector", "overwrite", "period_end", "bitemporal"):
                    assert subset.retrieve(q, backend) == whole.retrieve(q, backend)


def test_repeated_input_rows_count_once_in_every_in_memory_retriever():
    rows = [fact(), fact(value=90, filed="2021-02-12", available_on="2021-02-16", accession="0000000123-21-000002")]
    doubled = rows + [Fact(**row.to_dict()) for row in rows]
    once, twice = BackendIndex(rows), BackendIndex(doubled)
    assert len(twice.facts) == 2
    for as_of in ("2020-03-01", "2021-03-01"):
        q = query(as_of)
        assert select_latest(q, doubled) == select_latest(q, rows)
        for backend in ("vector", "overwrite", "period_end", "bitemporal"):
            result = twice.retrieve(q, backend)
            assert result == once.retrieve(q, backend)
            assert len({item.fact_id for item in result.context}) == len(result.context)


@needs_duckdb
def test_a_changed_exchange_calendar_cannot_add_a_second_row_for_one_filing():
    original = fact()
    recomputed = fact(available_on="2020-02-19")  # same filing value, different session
    with DuckDBStore() as store:
        store.append([original])
        with pytest.raises(ValueError, match="calendar changed"):
            store.append([recomputed])
        assert store.all_facts() == [original]
        assert store.append([original]) == 0  # an unchanged re-import stays idempotent
    with DuckDBStore() as store:
        with pytest.raises(ValueError, match="one exchange calendar"):
            store.append([original, recomputed])
        assert store.all_facts() == []




# ---- proposed: every in-memory implementation applies the documented same-day rules
def test_conflicting_starts_on_one_filing_date_are_ambiguous_in_memory_too():
    from pit_memory.evaluation import generate_cases
    rows = [fact(), fact(period_start="2019-01-02", accession="0000000123-20-000002")]
    q = query("2020-03-01")
    assert select_latest(q, rows).status == "ambiguous"
    index = BackendIndex(rows)
    for backend in ("bitemporal", "period_end", "overwrite"):
        assert index.retrieve(q, backend).status == "ambiguous", backend
    oracle = [case for case in generate_cases(rows) if case.query.as_of == "2020-02-18"]
    assert oracle and all(case.expected_status == "ambiguous" for case in oracle)


def test_identical_same_day_rows_cite_the_same_fact_everywhere():
    from pit_memory.evaluation import generate_cases
    rows = [fact(), fact(accession="0000000123-20-000002")]
    q = query("2020-03-01")
    expected = min(rows, key=lambda row: row.fact_id)
    assert select_latest(q, rows).fact == expected
    index = BackendIndex(rows)
    for backend in ("bitemporal", "period_end", "overwrite"):
        assert index.retrieve(q, backend).fact == expected, backend
    oracle = [case for case in generate_cases(rows) if case.query.as_of == "2020-02-18"]
    assert oracle and {case.expected_fact_id for case in oracle} == {expected.fact_id}


# ---- proposed: Fact/Query reject impossible or non-canonical inputs
@pytest.mark.parametrize("changes", [
    {"period_end": "2020-02-15", "period_start": "2019-02-16"},  # fiscal year ends after filing
    {"period_start": "2020-01-01"},                              # starts after it ends
    {"cik": "0"}, {"value": True}, {"form": " "},
])
def test_fact_rejects_impossible_order_and_empty_fields(changes):
    with pytest.raises(ValueError):
        fact(**changes)


def test_basic_iso_dates_are_rejected_because_dates_compare_as_text():
    # Python 3.11 date.fromisoformat accepts "20210301", which sorts after "2021-12-31".
    with pytest.raises(ValueError):
        query("20210301")


def test_selected_answer_is_always_in_the_returned_context():
    # Six identical same-day copies: top-5 context ranks by descending fact_id,
    # while the citation is the smallest fact_id, so it must be added explicitly.
    rows = [fact(accession=f"0000000123-20-00000{n}") for n in range(6)]
    for backend in ("period_end", "overwrite", "bitemporal"):
        result = BackendIndex(rows).retrieve(query("2020-03-01"), backend)
        assert result.status == "known" and result.fact in result.context, backend


def test_context_lists_newest_period_before_a_later_filed_older_period():
    fy2019 = fact()
    fy2020 = fact(value=120, period_start="2020-01-01", period_end="2020-12-31", filed="2021-02-12",
                  available_on="2021-02-16", accession="0000000123-21-000001")
    restated = fact(value=95, filed="2021-06-01", available_on="2021-06-02", accession="0000000123-21-000009")
    for backend in ("period_end", "bitemporal"):
        result = BackendIndex([fy2019, fy2020, restated]).retrieve(query("2021-07-01"), backend)
        assert list(result.context) == [fy2020, restated, fy2019], backend
