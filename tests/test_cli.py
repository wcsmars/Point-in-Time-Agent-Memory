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
