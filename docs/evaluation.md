# Evaluation contract

The benchmark asks for one annual fact for an exact CIK, namespace-qualified
concept, and unit. Queries either specify a fiscal period end or ask for the
latest reported annual period as of a date. A correct response contains the
value, fiscal period, and filing provenance, or explicitly abstains.

## Time and ground truth

`period_start` and `period_end` describe the fiscal period. `filed` is the SEC
filing date. `available_on` is the first XNYS session strictly after `filed`.
This conservative convention excludes the filing day because companyfacts does
not provide enough intraday information to reconstruct an exact trading cutoff.
It also observes exchange holidays instead of simply adding one weekday.

The eligible pool has `available_on <= as_of` and `period_end <= as_of`. Choose
the latest eligible fiscal period first, then the latest eligible filing for
that period. A newly filed restatement of an older period must not displace a
more recent annual period. Conflicting values or period starts on the same
latest filing date cause an `ambiguous` abstention; accession text is not a
reliable intraday ordering rule.

Unknown means no eligible row in the supplied corpus. A companyfacts download
is not proof that every fact historically published by the company has been
captured. Earnings releases can precede a 10-K. Tag changes, omitted custom
concepts, incomplete historical XBRL coverage, and fiscal-calendar changes can
limit comparisons. This implementation makes no implicit concept substitutions.

`generate_cases` builds exact-period and latest-annual questions on filing days,
the day before conservative availability, and availability days. Its independent
reference scan does not call the tested retriever. The demo adds handwritten
expectations for exchange holidays, original values, amendments, and the
older-period restatement trap. Generated boundary cases are deliberately enriched
for difficult dates; their error rates do **not** estimate ordinary query traffic.

## Four implementations

| Backend | Representation and ranking | Deliberate weakness |
| --- | --- | --- |
| `vector` | Signed 512-dimension feature-hash text vectors, cosine similarity, top 5 context | No date filter |
| `overwrite` | One materialized newest-filing snapshot per CIK/concept/unit/fiscal period end; conflicting same-day rows retained | Prior versions are absent; fiscal end is the only date filter |
| `period_end` | All versions, filtered only by fiscal end; newest period and filing win | Treats period end as availability |
| `bitemporal` | Filter by availability and period end before selecting the latest eligible period and filing | Intended correct retriever |

All four share explicit CIK, concept, unit, and optional exact-period scope. This
isolates temporal behavior from entity resolution. The hash-vector baseline is
real vector retrieval, but it is **not a semantic embedding model** and does not
stand in for every production vector database. Replace it with a named embedding
provider before making claims about embedding-model quality.

`overwrite` constructs a separate reduced snapshot once. Retrieval cannot recover
its discarded older filings. A later filing that dates the same fiscal year's
start a day differently still replaces the original, because one period end is
one fiscal year here, as in the revision profile. Some benchmarks make this baseline unrealistically
strong by retaining hidden history; this implementation does not.

CLI evaluation of a DuckDB database injects `store.retrieve` as the bitemporal
backend. It writes each trace to disk as it is produced and keeps only
per-issuer counts in memory. Because boundary questions grow with every fact,
`evaluate` without `--concept` on a database of more than 50,000 facts requires
the explicit `--all-concepts` flag. The demo also uses DuckDB when installed. When it is unavailable, the
demo explicitly reports `in_memory_reference` and writes no database. That mode
checks benchmark logic and fixtures; it does not validate DuckDB SQL or exchange
calendar integration. `bitemporal_implementation` and `storage_mode` record which
path ran.

## Scores and denominators

Every metric includes its numerator, denominator, value, and percentile 95%
bootstrap interval. A zero denominator produces JSON `null`.

| Metric | Numerator | Denominator |
| --- | --- | --- |
| `accuracy` | Correct issuer, concept, unit, value and period, or correct abstention status (an `ambiguous` answer counts only when it concerns the same conflicting period and filing date) | All queries |
| `coverage` | Answered queries | All queries |
| `answer_leakage_rate_all_queries` | Selected fact has `available_on > as_of` | All queries |
| `answer_leakage_rate_answered` | Selected fact has `available_on > as_of` | Answered queries |
| `context_leakage_rate` | At least one retrieved context fact is unavailable | All queries |
| `future_filing_answer_rate_all_queries` | Selected fact has `filed > as_of` | All queries |
| `leaked_and_incorrect_rate` | Selected fact unavailable **and** answer wrong | All queries |
| `correct_abstention_rate` | Correct `not_known` response | Ground-truth `not_known` queries |

Selected-answer leakage and context exposure answer different questions. A
retriever can expose future information even when its chosen numeric answer is
correct. A value repeated in a later filing can also be numerically correct
while its retrieved provenance leaks. The filing-date metric separates literal
future filings from same-day facts excluded by the conservative session rule.

Intervals resample issuers, retaining all of an issuer's questions together.
This avoids treating many nearby dates from one issuer as independent evidence.
A single issuer has no reported interval. The seed and sample count are recorded.
Independent-issuer bootstrap intervals still do not address shared market shocks,
selection of today's surviving firms, or incomplete filing coverage.

The evaluation runs no LLM. Latency covers each retrieval call; index construction
is reported separately. API cost is zero and excludes machine/data acquisition
costs. Model tokens, answer generation cost, and model accuracy are not measured.

## Evidence and attribution

`retrievals.jsonl` (gzipped as `retrievals.jsonl.gz` by the `study` command) records the complete returned context, selected fact, expected
value and period, date cutoffs, result status, and leakage flags for every query.
Future data in that context demonstrates retrieval exposure. It does not prove
that a later LLM answer relied on that data. Conversely, a future value absent
from the retrieved context does not prove training-data memorization; it could
be a guess or another channel. This project makes no model-training attribution.

`evaluation.json` contains aggregate metrics and experiment metadata;
`summary.csv` provides one backend/metric row for straightforward plotting.

## Factor experiment

The numeric factor is annual revenue growth: current annual revenue divided by
the preceding annual revenue minus one. The previous period must end 300 to 430
days earlier, and its value must be positive. Undefined inputs abstain. For the
bitemporal backend, even the metadata used to locate the previous period is
filtered by availability before choosing a period.

Supply simple decimal forward returns in CSV:

```csv
cik,as_of,forward_return,return_end
0000000001,2023-03-01,0.035,2023-04-03
```

The return begins at the close of `as_of`; optional `return_end` must be later
and must agree across the cross-section when supplied. Entity/date duplicates,
nonfinite returns, and simple returns below -100% are rejected. The software
cannot verify vendor adjustments or return construction from this CSV alone.
Use corporate-action-correct prices, a consistent horizon, a historically
appropriate universe, and delisting-aware data for a real experiment.

Factor retrieval uses the scoped in-memory implementations and records that
engine explicitly; it does not time or benchmark database SQL. Each observation
logs both facts and both returned contexts.

The code computes tie-aware Spearman rank IC separately for each date. Constant
signals/returns and undersized cross-sections produce `null`, not zero. Paired IC
differences compare each baseline against bitemporal on the **same entities and
dates**, so coverage changes cannot create an apparent paired benefit. Report
both individual coverage and paired sample sizes. Bootstrap intervals resample
dates and require at least two dates. Every backend uses the same seed, so
backends with identical per-date ICs report identical intervals. They are descriptive; overlapping return
horizons would require an appropriate block bootstrap before inference.

The demo contains 24 fictional firms and 4 return dates. Its returns are
**deliberately correlated with future restatements** to verify that the IC
comparison catches an injected problem. Its IC differences are a regression
fixture, not empirical inflation estimates. No return data is fetched implicitly.

## Real-data study

`pit-memory study` runs three analyses on one set of filing-level facts and a
predeclared concept list (by default `us-gaap:Revenues`,
`RevenueFromContractWithCustomerExcludingAssessedTax`, `SalesRevenueNet` and
`NetIncomeLoss`, unit USD).

**Revision profile.** A fiscal period is identified by CIK, concept, unit,
and period end. Different annual start-date conventions for the same year do
not create extra periods. Its original value is the one on the earliest filing
date. The period is *revised* when any later filing reports a different value
(the comparison ignores only floating-point noise, a relative 1e-12). Rounding
counts: some issuers later report the same year in millions instead of
thousands, and those periods are revisions, usually small ones. Periods with
conflicting values on any filing date are counted and excluded, with separate
counts for original and later filing conflicts. Comparatives that repeat the
same value are re-filings, not revisions. Size percentiles (median, 90th)
interpolate linearly between observed revision sizes, as every other
percentile in this package does.

**Scheduled leakage audit.** Every series receives a latest-annual question on
the first day of each month, starting at the series' first availability date.
A question is kept only while the point-in-time answer's fiscal year ended at
most `--max-age-days` (default 550) days earlier. Without this bound, series an
issuer has stopped reporting (for example a revenue tag replaced in 2018) would
be asked about for years; such questions can never leak and would dilute every
rate. The bound uses only information available on the question date. The
same bound removes stale issuer-months from the signal comparison.
It also applies when the newest eligible period has conflicting values and
therefore produces an ambiguous abstention.
This schedule is easier to interpret than the boundary-enriched cases, but its
rates still depend on it. Because no question precedes the first availability
date, the correct-abstention denominator is zero on this schedule.

`overwrite` and `period_end` select the same fact for every question in this
benchmark, by construction: both take the newest filing of the newest fiscal
period that has ended, and the overwrite snapshot keeps exactly those newest
filings. Their selected-answer metrics are therefore identical. They differ in
what remains recoverable: `period_end` still stores every version and is fixed
by adding an availability filter, while the overwrite snapshot has discarded
the original values, so no later date filter can repair it.

`leaked_answer_breakdown` in `study.json` classifies every answer that used an
unavailable fact: the right value cited from a later filing (or one filed on
the question date), an earlier filing still awaiting its first exchange session,
a different fiscal year (usually one whose report was not
yet filed), or a restated value for the right year.

**Signal divergence.** Annual revenue growth is computed by every backend with
the same code for every issuer and month. The report counts signals that match
the point-in-time signal, that differ from it, and that have no point-in-time
counterpart, plus the per-date Spearman correlation of the
cross-sectional ranking with the point-in-time ranking. No returns are used, so
this measures contamination of the signal, not trading performance.
Missing counterparts can also result from ambiguity or selection of a different
period; the separate unavailable-fact count measures temporal leakage directly.

## Before reporting a real headline number

Run the same pipeline on retained SEC snapshots and a documented historical
universe. Retain raw source hashes and quarantined rows. Review tag coverage,
amendments, fiscal-calendar shifts, and ambiguous filing dates. Supply verified
return data, record the exact query distribution, and distinguish synthetic
checks from observed results. Report availability-based leakage, literal
future-filing leakage, context exposure, correct abstentions, coverage, paired IC,
and uncertainty together.


For an ambiguous answer to score as correct, the response must identify the
requested query and cite at least two eligible, conflicting values from the
expected winning period and filing date. A claimed abstention with a selected
answer is contradictory and does not score as correct. These checks also apply
to externally supplied retrieval implementations.
