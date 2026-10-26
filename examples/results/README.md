# Study results

Outputs of `pit-memory study` on the full 40-issuer universe in
`examples/universe.txt` (245,551 imported facts; 3,951 in the four study
concepts), with the bitemporal answers served by DuckDB SQL. The raw downloads,
the database and the 3.6 MB compressed per-query trace file are not included; rerun the
commands in the main README to regenerate them.

| File | Contents |
| --- | --- |
| `study.json` | Universe, schedule, revision profile, leakage metrics with denominators and issuer-cluster bootstrap intervals, signal-divergence summary, the plotted example |
| `leakage_summary.csv` | One row per backend and metric |
| `leakage_evaluation.json` | The study's `leakage/evaluation.json`: aggregate metrics and run metadata of the scheduled audit (timings differ between runs) |
| `revisions.csv` | Every fiscal period whose value changed in a later filing, with both accession numbers and the size of the change |
| `signal_divergence.json` | Per-date rank correlation of each backend's revenue-growth signal with the point-in-time signal |
