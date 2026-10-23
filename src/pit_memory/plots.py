"""Static figures for the README, drawn from study and demo outputs.

Requires matplotlib (``pip install -e '.[plots]'``). Every figure reads only the
JSON/CSV evidence written by the ``study`` and ``demo`` commands, so figures can
be regenerated without repeating retrieval.
"""
from __future__ import annotations

import csv
import json
import math
from datetime import date
from pathlib import Path

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
# Fixed backend-to-colour mapping; colour follows the backend in every figure.
COLORS = {"bitemporal": "#2a78d6", "period_end": "#eb6834", "overwrite": "#1baf7a", "vector": "#eda100"}
ORDER = ("vector", "overwrite", "period_end", "bitemporal")
LABELS = {"vector": "Vector (no date filter)", "overwrite": "Overwrite (latest only)",
          "period_end": "Fiscal period end only", "bitemporal": "Bitemporal (point in time)"}
SHORT = {"vector": "Vector", "overwrite": "Overwrite", "period_end": "Period end", "bitemporal": "Bitemporal"}


def _pyplot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID, "axes.labelcolor": INK_2, "xtick.color": INK_2, "ytick.color": INK_2,
        "text.color": INK, "font.size": 10, "axes.titlesize": 11, "axes.titleweight": "bold",
        "axes.spines.top": False, "axes.spines.right": False, "axes.grid": False,
        "legend.frameon": False, "figure.dpi": 150,
    })
    return plt


def _save(fig, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    return str(path)


def _metric_panels(metrics_by_backend: dict, title: str, path: Path,
                   panels: tuple[tuple[str, str], ...]) -> str:
    """Small multiples: one panel per metric, one bar per backend, 0-100% axis."""
    plt = _pyplot()
    fig, axes = plt.subplots(1, len(panels), figsize=(3.1 * len(panels), 3.4), sharey=True)
    for ax, (metric, label) in zip(axes, panels):
        for position, backend in enumerate(ORDER):
            entry = metrics_by_backend[backend][metric]
            value = entry["value"]
            if value is None:
                continue
            ax.bar(position, value * 100, width=0.72, color=COLORS[backend], edgecolor=SURFACE, linewidth=2)
            top = value * 100
            if entry.get("ci95"):
                low, high = (v * 100 for v in entry["ci95"])
                if high - low > 0.5:
                    ax.plot([position, position], [low, high], color=INK, linewidth=1)
                    top = max(top, high)
            ax.text(position, top + 2.5, f"{value * 100:.0f}%", ha="center", va="bottom",
                    fontsize=9, color=INK)
        ax.set_title(label, loc="left")
        ax.set_xticks(range(len(ORDER)), [SHORT[b] for b in ORDER], rotation=30, ha="right")
        ax.set_ylim(0, 112)
        ax.set_yticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
        ax.yaxis.grid(True, color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(length=0)
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, fontweight="bold")
    fig.tight_layout()
    return _save(fig, path)


PANELS = (
    ("accuracy", "Correct answer"),
    ("answer_leakage_rate_all_queries", "Answer not yet available"),
    ("leaked_and_incorrect_rate", "Unavailable and wrong"),
    ("context_leakage_rate", "Future fact in context"),
)


def plot_study(study_dir: str | Path, output_dir: str | Path) -> list[str]:
    study_dir, output_dir = Path(study_dir), Path(output_dir)
    study = json.loads((study_dir / "study.json").read_text())
    written = []
    sched = study["schedule"]
    written.append(_metric_panels(
        study["leakage"],
        f"SEC companyfacts, {study['universe']['issuers']} issuers: monthly latest-annual questions "
        f"{sched['start'][:4]}-{sched['end'][:4]} (n={study['leakage_queries']:,})",
        output_dir / "sec_leakage_by_backend.png", PANELS))

    plt = _pyplot()
    rows = list(csv.DictReader((study_dir / "revisions.csv").open()))
    sizes = [abs(float(r["relative_change"])) for r in rows if r["relative_change"] not in ("", None)]
    sizes = [s for s in sizes if s > 0]
    # Remove figures an earlier run drew, so a run without them leaves none stale.
    for name in ("sec_revision_sizes.png", "sec_revision_example.png"):
        (output_dir / name).unlink(missing_ok=True)
    if sizes:
        fig, ax = plt.subplots(figsize=(7.2, 3.4))
        edges = [10 ** (x / 4) for x in range(-24, 5)]
        ax.hist(sizes, bins=edges, color=COLORS["bitemporal"], edgecolor=SURFACE, linewidth=1.5)
        ax.set_xscale("log")
        ax.set_xticks([1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1], ["0.001%", "0.01%", "0.1%", "1%", "10%", "100%"])
        ax.set_xlabel("Size of revision relative to the originally filed value")
        ax.set_ylabel("Revised fiscal periods")
        from matplotlib.ticker import MaxNLocator
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax.yaxis.grid(True, color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        overall = study["revisions"]["overall"]
        ax.set_title(f"{overall['periods_revised']:,} of {overall['periods']:,} annual periods were later "
                     f"reported with a different value", loc="left")
        ax.tick_params(length=0)
        fig.tight_layout()
        written.append(_save(fig, output_dir / "sec_revision_sizes.png"))

    example = study.get("example_revision")
    if example:
        fig, ax = plt.subplots(figsize=(7.2, 3.6))
        xs = [date.fromisoformat(r["as_of"]) for r in example["rows"]]
        for backend in ("overwrite", "bitemporal"):
            ys = [r[backend] / 1e9 if r[backend] is not None else math.nan for r in example["rows"]]
            ax.step(xs, ys, where="post", color=COLORS[backend], linewidth=2.2, label=LABELS[backend])
        ax.set_ylim(0, max(v for r in example["rows"] for v in (r["overwrite"], r["bitemporal"])
                           if v is not None) / 1e9 * 1.18)
        for when, text in ((example["original_filed"], "original 10-K filed"),
                           (example["first_revision_filed"], "different value filed")):
            ax.axvline(date.fromisoformat(when), color=INK_2, linewidth=0.8, linestyle=(0, (3, 3)))
            ax.text(date.fromisoformat(when), ax.get_ylim()[1], f" {text}\n {when}", va="top",
                    fontsize=8, color=INK_2)
        name = example["issuer"] or f"CIK {example['cik']}"
        ax.set_title(f"{name}: fiscal year ending {example['period_end']}, value reported as of each date",
                     loc="left")
        ax.set_ylabel(f"{example['concept'].split(':')[1][:40]} (USD bn)")
        ax.yaxis.grid(True, color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.set_ylim(0, ax.get_ylim()[1] * 1.12)
        ax.legend(loc="lower center", fontsize=8, ncol=2)
        ax.tick_params(length=0)
        fig.tight_layout()
        written.append(_save(fig, output_dir / "sec_revision_example.png"))

    divergence = json.loads((study_dir / "signal_divergence.json").read_text())
    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    identical = all(
        divergence["summary"]["overwrite"][k] == divergence["summary"]["period_end"][k]
        for k in ("changed_vs_bitemporal", "mean_rank_correlation_with_bitemporal"))
    for backend in ("vector", "period_end") if identical else ("vector", "overwrite", "period_end"):
        label = "Overwrite and period end (identical here)" if identical and backend == "period_end" else LABELS[backend]
        points = [(date.fromisoformat(r["as_of"]), r["rank_correlation_with_bitemporal"])
                  for r in divergence["per_date"]
                  if r["backend"] == backend and r["rank_correlation_with_bitemporal"] is not None]
        if points:
            ax.plot([p[0] for p in points], [p[1] for p in points], color=COLORS[backend],
                    linewidth=1.6, label=label)
    ax.set_ylim(min(0.0, ax.get_ylim()[0]), 1.25)
    ax.set_yticks([t for t in (-0.5, -0.25, 0, 0.25, 0.5, 0.75, 1.0) if t >= ax.get_ylim()[0]])
    ax.axhline(1, color=GRID, linewidth=0.8)
    ax.set_ylabel("Rank correlation with\npoint-in-time signal")
    ax.set_title("Cross-sectional revenue-growth ranking under each backend", loc="left")
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.legend(loc="upper left", fontsize=8, ncol=2)
    ax.tick_params(length=0)
    fig.tight_layout()
    written.append(_save(fig, output_dir / "sec_signal_rank_correlation.png"))
    return written


def plot_demo(demo_dir: str | Path, output_dir: str | Path) -> list[str]:
    demo_dir, output_dir = Path(demo_dir), Path(output_dir)
    report = json.loads((demo_dir / "evaluation.json").read_text())
    metrics = {backend: summary["metrics"] for backend, summary in report["backends"].items()}
    data = report["dataset"]
    written = [_metric_panels(metrics, f"Synthetic benchmark: {data['issuers']} fictional issuers, "
                              f"{data['queries']} labelled questions",
                              output_dir / "demo_leakage_by_backend.png", PANELS)]
    factor = json.loads((demo_dir / "factor.json").read_text())
    plt = _pyplot()
    fig, ax = plt.subplots(figsize=(6.4, 3.2))
    for position, backend in enumerate(ORDER):
        summary = factor["summary"][backend]
        value = summary["mean_ic"]
        if value is None:
            continue
        ax.barh(position, value, height=0.66, color=COLORS[backend], edgecolor=SURFACE, linewidth=2)
        low, high = summary["mean_ic_ci95"] or (value, value)
        if summary["mean_ic_ci95"]:
            ax.plot([low, high], [position, position], color=INK, linewidth=1)
        anchor = max(value, high, 0) + 0.04 if value >= 0 else min(value, low, 0) - 0.04
        ax.text(anchor, position, f"{value:+.2f}",
                va="center", ha="left" if value >= 0 else "right", fontsize=9)
    ax.axvline(0, color=INK_2, linewidth=0.8)
    ax.set_yticks(range(len(ORDER)), [LABELS[b] for b in ORDER])
    ax.set_xlim(-1.15, 1.15)
    ax.set_xlabel("Mean rank IC of revenue growth vs planted forward returns")
    ax.set_title("Synthetic factor check: future restatements inflate IC", loc="left")
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)
    fig.tight_layout()
    written.append(_save(fig, output_dir / "demo_factor_ic.png"))
    return written
