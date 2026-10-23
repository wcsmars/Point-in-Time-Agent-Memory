import json

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
