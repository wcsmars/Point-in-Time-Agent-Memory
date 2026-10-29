"""Charts must preserve the unit and full range of the evidence."""
import json
from pathlib import Path
import shutil

import pytest


def test_revision_chart_shows_negative_values_and_actual_unit(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    from pit_memory import plots

    source = Path(__file__).resolve().parents[1] / "examples" / "results"
    study = json.loads((source / "study.json").read_text())
    example = study["example_revision"]
    example["unit"] = "EUR"
    example["concept"] = "us-gaap:NetIncomeLoss"
    for row in example["rows"]:
        for backend in ("overwrite", "bitemporal"):
            if row[backend] is not None:
                row[backend] = -abs(row[backend])
    (tmp_path / "study.json").write_text(json.dumps(study))
    for filename in ("revisions.csv", "signal_divergence.json"):
        shutil.copyfile(source / filename, tmp_path / filename)

    captured = {}

    def capture(fig, path):
        import matplotlib.pyplot as plt
        if path.name == "sec_revision_example.png":
            ax = fig.axes[0]
            captured["limits"] = ax.get_ylim()
            captured["label"] = ax.get_ylabel()
        plt.close(fig)
        return str(path)

    monkeypatch.setattr(plots, "_save", capture)
    plots.plot_study(tmp_path, tmp_path / "figures")
    values = [r[b] / 1e9 for r in example["rows"] for b in ("overwrite", "bitemporal") if r[b] is not None]
    assert captured["limits"][0] < min(values) < max(values) < captured["limits"][1]
    assert captured["label"] == "NetIncomeLoss (EUR bn)"


def test_charts_leave_gaps_for_undefined_correlations_and_keep_every_revision(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    import csv
    import math
    from pit_memory import plots

    source = Path(__file__).resolve().parents[1] / "examples" / "results"
    shutil.copyfile(source / "study.json", tmp_path / "study.json")
    with (source / "revisions.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows[0]["relative_change"] = "25.0"  # a 2,500% restatement of a near-zero value
    with (tmp_path / "revisions.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    divergence = json.loads((source / "signal_divergence.json").read_text())
    vector = [r for r in divergence["per_date"] if r["backend"] == "vector"]
    vector[50]["rank_correlation_with_bitemporal"] = None  # a gap in the middle
    vector[51]["rank_correlation_with_bitemporal"] = -0.9
    (tmp_path / "signal_divergence.json").write_text(json.dumps(divergence))

    captured = {}

    def capture(fig, path):
        import matplotlib.pyplot as plt
        ax = fig.axes[0]
        if path.name == "sec_revision_sizes.png":
            captured["bars"] = sum(patch.get_height() for patch in ax.patches)
        if path.name == "sec_signal_rank_correlation.png":
            line = next(l for l in ax.get_lines() if l.get_label().startswith("Vector"))
            captured["gap"] = math.isnan(line.get_ydata()[50])
            captured["ticks"] = list(ax.get_yticks())
        plt.close(fig)
        return str(path)

    monkeypatch.setattr(plots, "_save", capture)
    plots.plot_study(tmp_path, tmp_path / "figures")
    assert captured["bars"] == len([r for r in rows if r["relative_change"] and float(r["relative_change"]) != 0])
    assert captured["gap"] is True
    assert min(captured["ticks"]) <= -0.75


def test_equal_aggregate_correlations_do_not_hide_different_curves(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    from pit_memory import plots

    source = Path(__file__).resolve().parents[1] / "examples" / "results"
    for filename in ("study.json", "revisions.csv"):
        shutil.copyfile(source / filename, tmp_path / filename)
    divergence = json.loads((source / "signal_divergence.json").read_text())
    overwrite = [row for row in divergence["per_date"] if row["backend"] == "overwrite"]
    defined = [row for row in overwrite if row["rank_correlation_with_bitemporal"] is not None]
    first = defined[0]
    second = next(row for row in defined if row["rank_correlation_with_bitemporal"]
                  != first["rank_correlation_with_bitemporal"])
    # Swapping dates preserves the mean and changed-signal total. Both paths
    # still need to be visible because the timing of signal divergence differs.
    key = "rank_correlation_with_bitemporal"
    first[key], second[key] = second[key], first[key]
    (tmp_path / "signal_divergence.json").write_text(json.dumps(divergence))

    labels = []

    def capture(fig, path):
        import matplotlib.pyplot as plt
        if path.name == "sec_signal_rank_correlation.png":
            labels.extend(fig.axes[0].get_legend_handles_labels()[1])
        plt.close(fig)
        return str(path)

    monkeypatch.setattr(plots, "_save", capture)
    plots.plot_study(tmp_path, tmp_path / "figures")
    assert labels == [plots.LABELS[b] for b in ("vector", "overwrite", "period_end")]
