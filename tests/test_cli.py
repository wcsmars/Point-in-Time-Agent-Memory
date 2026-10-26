import json

import pytest

from pit_memory.cli import main


def test_demo_writes_auditable_artifacts(tmp_path, capsys):
    assert main(["demo", "--output", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["synthetic"] is True
    assert result["queries"] == 312
    assert result["metrics"]["bitemporal"]["accuracy"] == 1.0
    report = json.loads((tmp_path / "evaluation.json").read_text())
    assert report["dataset"]["synthetic"] is True
    assert len((tmp_path / "retrievals.jsonl").read_text().splitlines()) == 312 * 4


def test_query_does_not_create_missing_database(tmp_path, capsys):
    path = tmp_path / "missing.duckdb"
    assert main(["query", "--db", str(path), "--cik", "320193", "--concept", "us-gaap:Revenues",
                 "--as-of", "2021-03-15"]) == 2
    assert "does not exist" in capsys.readouterr().err
    assert not path.exists()


def test_import_sec_rejects_non_companyfacts_file_without_creating_database(tmp_path, capsys):
    bad = tmp_path / "facts.jsonl"
    bad.write_text('{"a": 1}\n{"b": 2}\n')
    db = tmp_path / "x.duckdb"
    assert main(["import-sec", str(bad), "--db", str(db), "--report", str(tmp_path / "r.json")]) == 2
    assert "facts.jsonl" in capsys.readouterr().err
    assert not db.exists()


def test_sample_imports_offline_and_answers_point_in_time(tmp_path, capsys):
    import pytest
    pytest.importorskip("duckdb")
    from pathlib import Path
    sample = Path(__file__).resolve().parents[1] / "examples" / "sample-data" / "sec-facts-sample.jsonl"
    if not sample.is_file():
        pytest.skip("sample data not present")
    db = tmp_path / "sample.duckdb"
    assert main(["import-facts", str(sample), "--db", str(db)]) == 0
    loaded = json.loads(capsys.readouterr().out)
    assert loaded["inserted"] == loaded["facts_in_database"] > 0
    assert main(["query", "--db", str(db), "--cik", "51143", "--concept", "us-gaap:Revenues",
                 "--as-of", "2021-03-15", "--period-end", "2019-12-31"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["backend"] == "bitemporal"
    assert answer["fact"]["value"] == 77147000000.0
    assert main(["query", "--db", str(db), "--cik", "51143", "--concept", "us-gaap:Revenues",
                 "--as-of", "2021-03-15", "--period-end", "2019-12-31", "--backend", "overwrite"]) == 0
    assert json.loads(capsys.readouterr().out)["fact"]["value"] == 57714000000.0


def test_import_into_a_non_duckdb_path_fails_instead_of_losing_data(tmp_path, capsys):
    import pytest
    pytest.importorskip("duckdb")
    from pathlib import Path
    sample = Path(__file__).resolve().parents[1] / "examples" / "sample-data" / "sec-facts-sample.jsonl"
    if not sample.is_file():
        pytest.skip("sample data not present")
    target = tmp_path / "t.json"
    target.write_text('{"a": 1}\n')
    assert main(["import-facts", str(sample), "--db", str(target)]) == 2
    assert "not a DuckDB database" in capsys.readouterr().err
    assert target.read_text() == '{"a": 1}\n'


def test_demo_refuses_an_unrelated_output_folder(tmp_path, capsys):
    (tmp_path / "synthetic-facts.jsonl").write_text('{"x": 1}\n')
    assert main(["demo", "--output", str(tmp_path)]) == 2
    assert "not a demo output" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["export-facts", "factor", "import-sec", "fetch-sec", "study", "evaluate"])
def test_output_cannot_overwrite_input_database(tmp_path, capsys, command):
    path = tmp_path / ("study.json" if command == "study" else "evaluation.json")
    original = b"existing input must survive an invalid output path"
    path.write_bytes(original)
    args = [command, "--db", str(path)]
    if command == "export-facts":
        args += ["--cik", "1", "--concept", "us-gaap:Revenues", "--output", str(path)]
    elif command == "factor":
        args += ["--returns", str(tmp_path / "returns.csv"), "--output", str(path)]
    elif command == "import-sec":
        args += [str(tmp_path / "raw.json"), "--report", str(path)]
    elif command == "fetch-sec":
        args += ["--cik", "1", "--report", str(path)]
    else:
        args += ["--output", str(tmp_path)]
    assert main(args) == 2
    assert "would overwrite" in capsys.readouterr().err
    assert path.read_bytes() == original


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_export_rejects_output_alias_of_database(tmp_path, capsys, link_kind):
    source, target = tmp_path / "source.duckdb", tmp_path / "output.jsonl"
    source.write_bytes(b"original database")
    if link_kind == "symlink":
        target.symlink_to(source)
    else:
        target.hardlink_to(source)
    assert main(["export-facts", "--db", str(source), "--cik", "1",
                 "--concept", "us-gaap:Revenues", "--output", str(target)]) == 2
    assert "would overwrite" in capsys.readouterr().err
    assert source.read_bytes() == b"original database"


def test_import_report_cannot_overwrite_source(tmp_path, capsys):
    source = tmp_path / "raw.json"
    source.write_text('{"cik": 1, "facts": {}}')
    db = tmp_path / "facts.duckdb"
    assert main(["import-sec", str(source), "--db", str(db), "--report", str(source)]) == 2
    assert "would overwrite" in capsys.readouterr().err
    assert source.read_text() == '{"cik": 1, "facts": {}}'
    assert not db.exists()


@pytest.mark.parametrize("option", ["--report", "--db"])
@pytest.mark.parametrize("kind", ["pointer", "snapshot", "symlink", "hardlink", "future_pointer"])
def test_fetch_outputs_cannot_replace_cached_sources(tmp_path, capsys, option, kind):
    import hashlib

    cache = tmp_path / "cache"
    cache.mkdir()
    raw = b'{"cik": 1, "facts": {}}'
    sha = hashlib.sha256(raw).hexdigest()
    snapshot = cache / f"CIK0000000001.{sha}.json"
    snapshot.write_bytes(raw)
    pointer = cache / "CIK0000000001.latest.json"
    pointer.write_text(json.dumps({"cik": "0000000001", "sha256": sha}))
    original_pointer = pointer.read_bytes()
    if kind == "pointer":
        target = pointer
    elif kind == "snapshot":
        target = snapshot
    elif kind == "future_pointer":
        target = cache / "CIK0000000002.latest.json"
    else:
        target = tmp_path / "alias.json"
        if kind == "symlink":
            target.symlink_to(pointer)
        else:
            target.hardlink_to(snapshot)
    db = target if option == "--db" else tmp_path / "facts.duckdb"
    report = target if option == "--report" else tmp_path / "report.json"
    assert main(["fetch-sec", "--cik", "1", "--cache", str(cache),
                 "--db", str(db), "--report", str(report)]) == 2
    assert "would overwrite an SEC cache file" in capsys.readouterr().err
    assert pointer.read_bytes() == original_pointer
    assert snapshot.read_bytes() == raw
    assert not (cache / "CIK0000000002.latest.json").exists()




# ---- proposed: functional coverage of CLI commands that produce published evidence
def _demo_database(tmp_path, capsys, extra=()):
    pytest.importorskip("duckdb")
    from pit_memory.demo import build_demo
    from pit_memory.study import write_facts_jsonl
    facts = build_demo(3).facts + list(extra)
    jsonl, db = tmp_path / "facts.jsonl", tmp_path / "facts.duckdb"
    write_facts_jsonl(facts, jsonl)
    assert main(["import-facts", str(jsonl), "--db", str(db)]) == 0
    capsys.readouterr()
    return facts, db


def test_study_from_database_uses_the_sql_retriever(tmp_path, capsys):
    _, db = _demo_database(tmp_path, capsys)
    out = tmp_path / "study"
    assert main(["study", "--db", str(db), "--concept", "us-gaap:Revenues", "--signal-concept",
                 "us-gaap:Revenues", "--start", "2021-01-01", "--end", "2024-12-31",
                 "--output", str(out), "--bootstrap-samples", "0"]) == 0
    study = json.loads((out / "study.json").read_text())
    assert study["engines"]["leakage_audit_bitemporal"] == "duckdb"
    audit = json.loads((out / "leakage" / "evaluation.json").read_text())
    assert audit["bitemporal_implementation"] == "provided_retriever"


def test_evaluate_filters_concept_and_records_provenance(tmp_path, capsys):
    from dataclasses import replace
    from pit_memory.demo import build_demo
    other = replace(build_demo(3).facts[0], concept="us-gaap:NetIncomeLoss", fact_id="")
    facts, db = _demo_database(tmp_path, capsys, [other])
    out = tmp_path / "audit"
    assert main(["evaluate", "--db", str(db), "--concept", "us-gaap:Revenues", "--output", str(out),
                 "--bootstrap-samples", "0"]) == 0
    report = json.loads((out / "evaluation.json").read_text())
    assert report["dataset"]["facts"] == len(facts) - 1
    assert report["bitemporal_implementation"] == "provided_retriever"
    assert report["source_snapshots"] == [""]


def test_export_facts_selects_unit(tmp_path, capsys):
    from dataclasses import replace
    from pit_memory.demo import build_demo
    euro = replace(build_demo(3).facts[0], unit="EUR", fact_id="")
    _, db = _demo_database(tmp_path, capsys, [euro])
    out = tmp_path / "export.jsonl"
    assert main(["export-facts", "--db", str(db), "--cik", euro.cik, "--concept", euro.concept,
                 "--unit", "USD", "--output", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["facts"] == 6
    assert all(json.loads(line)["unit"] == "USD" for line in out.read_text().splitlines())


def test_factor_cli_honours_min_entities(tmp_path, capsys):
    import csv
    from pit_memory.demo import build_demo
    _, db = _demo_database(tmp_path, capsys)
    returns = tmp_path / "returns.csv"
    with returns.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["cik", "as_of", "forward_return", "return_end"])
        writer.writeheader()
        writer.writerows(build_demo(3).returns)
    out = tmp_path / "factor.json"
    assert main(["factor", "--db", str(db), "--returns", str(returns), "--output", str(out),
                 "--min-entities", "4", "--bootstrap-samples", "0"]) == 0
    report = json.loads(out.read_text())
    assert report["minimum_entities_per_date"] == 4
    assert all(day["ic"] is None for day in report["per_date"])


def test_import_sec_writes_report_and_is_idempotent(tmp_path, capsys):
    import hashlib
    pytest.importorskip("duckdb")
    entry = {"start": "2019-01-01", "end": "2019-12-31", "val": 100, "accn": "0000000123-20-000001",
             "filed": "2020-02-14", "form": "10-K"}
    raw = tmp_path / "CIK0000000123.json"
    raw.write_text(json.dumps({"cik": 123, "facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        entry, {**entry, "start": "2019-10-01", "val": 25}, {**entry, "accn": "bad"}]}}}}}))
    db, report = tmp_path / "facts.duckdb", tmp_path / "report.json"
    for expected in (1, 0):
        assert main(["import-sec", str(raw), "--db", str(db), "--report", str(report)]) == 0
        assert json.loads(capsys.readouterr().out)["inserted"] == expected
        imported = json.loads(report.read_text())["imports"][0]
        assert (imported["accepted"], imported["inserted"], imported["skipped_out_of_scope"],
                len(imported["rejected"])) == (1, expected, 1, 1)
        assert imported["source_sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()


def test_fetch_sec_normalizes_and_deduplicates_ciks(tmp_path, capsys, monkeypatch):
    from io import BytesIO
    pytest.importorskip("duckdb")
    raw = json.dumps({"cik": 1, "facts": {}}).encode()
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request.full_url)
        return BytesIO(raw)

    monkeypatch.setattr("pit_memory.sec.urlopen", fake_urlopen)
    monkeypatch.setenv("SEC_USER_AGENT", "Offline fixture contact@example.test")
    assert main(["fetch-sec", "--cik", "1", "--cik", "0000000001", "--cache", str(tmp_path / "cache"),
                 "--db", str(tmp_path / "facts.duckdb"), "--report", str(tmp_path / "report.json"),
                 "--refresh"]) == 0
    assert json.loads(capsys.readouterr().out)["companies"] == 1
    assert requests == ["https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json"]


def test_unopenable_database_is_a_clean_cli_error(tmp_path, capsys):
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "other.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE facts (x INTEGER)")
    assert main(["query", "--db", str(path), "--cik", "1", "--concept", "us-gaap:Revenues",
                 "--as-of", "2021-03-01"]) == 2
    assert "Could not open" in capsys.readouterr().err


def test_report_cannot_target_the_database_path_before_either_exists(tmp_path, capsys):
    pytest.importorskip("duckdb")
    raw = tmp_path / "raw.json"
    raw.write_text('{"cik": 1, "facts": {}}')
    target = tmp_path / "same.duckdb"
    assert main(["import-sec", str(raw), "--db", str(target), "--report", str(target)]) == 2
    assert "would overwrite" in capsys.readouterr().err
    assert not target.exists()


def test_import_sec_skips_fetch_cache_pointers_and_reports_skip_reasons(tmp_path, capsys):
    pytest.importorskip("duckdb")
    entry = {"start": "2019-01-01", "end": "2019-12-31", "val": 100, "accn": "0000000123-20-000001",
             "filed": "2020-02-14", "form": "10-K"}
    raw = json.dumps({"cik": 123, "facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        entry, {**entry, "form": "8-K"}]}}}}}).encode()
    import hashlib
    cache = tmp_path / "raw"
    cache.mkdir()
    snapshot = cache / f"CIK0000000123.{hashlib.sha256(raw).hexdigest()}.json"
    snapshot.write_bytes(raw)
    pointer = cache / "CIK0000000123.latest.json"
    pointer.write_text(json.dumps({"cik": "0000000123", "sha256": hashlib.sha256(raw).hexdigest()}))
    report = tmp_path / "report.json"
    assert main(["import-sec", str(pointer), str(snapshot), "--db", str(tmp_path / "x.duckdb"),
                 "--report", str(report)]) == 0
    assert json.loads(capsys.readouterr().out)["inserted"] == 1
    written = json.loads(report.read_text())
    assert written["skipped_pointer_files"] == [str(pointer)]
    assert written["imports"][0]["skipped_by_reason"] == {"annual-length period on form 8-K": 1}
    assert written["imports"][0]["source"] == str(snapshot)  # as given, never resolved
    assert main(["import-sec", str(pointer), "--db", str(tmp_path / "y.duckdb"),
                 "--report", str(tmp_path / "r2.json")]) == 2
    assert "pointer" in capsys.readouterr().err


def test_demo_never_replaces_a_real_audit_report(tmp_path, capsys):
    real = {"dataset": {"facts": 10, "queries": 5}, "dataset_kind": "imported_sec_filing_records"}
    (tmp_path / "evaluation.json").write_text(json.dumps(real))
    assert main(["demo", "--output", str(tmp_path)]) == 2
    assert "not a demo output" in capsys.readouterr().err
    assert json.loads((tmp_path / "evaluation.json").read_text()) == real
    rerun = tmp_path / "rerun"
    assert main(["demo", "--output", str(rerun)]) == 0
    assert main(["demo", "--output", str(rerun)]) == 0  # its own outputs may be refreshed


def test_evaluate_streams_traces_and_guards_whole_database_audits(tmp_path, capsys, monkeypatch):
    from pit_memory import cli
    _, db = _demo_database(tmp_path, capsys)
    out = tmp_path / "audit"
    monkeypatch.setattr(cli, "EVALUATE_ALL_LIMIT", 5)
    assert main(["evaluate", "--db", str(db), "--output", str(out), "--bootstrap-samples", "0"]) == 2
    assert "--all-concepts" in capsys.readouterr().err
    assert not (out / "retrievals.jsonl").exists()
    assert main(["evaluate", "--db", str(db), "--output", str(out), "--bootstrap-samples", "0",
                 "--all-concepts"]) == 0
    report = json.loads((out / "evaluation.json").read_text())
    traces = (out / "retrievals.jsonl").read_text().splitlines()
    assert len(traces) == 4 * report["dataset"]["queries"] > 0
    assert report["source_database"] == str(db)
    assert main(["evaluate", "--db", str(db), "--output", str(out), "--bootstrap-samples", "-1",
                 "--all-concepts"]) == 2
    assert len((out / "retrievals.jsonl").read_text().splitlines()) == len(traces)  # not truncated


def test_study_output_cannot_overwrite_its_input_inside_the_leakage_folder(tmp_path, capsys):
    from pit_memory.demo import build_demo
    from pit_memory.study import write_facts_jsonl
    out = tmp_path / "study"
    facts = out / "leakage" / "evaluation.json"
    facts.parent.mkdir(parents=True)
    write_facts_jsonl(build_demo(3).facts, facts)
    assert main(["study", "--facts", str(facts), "--output", str(out), "--bootstrap-samples", "0"]) == 2
    assert "would overwrite" in capsys.readouterr().err
