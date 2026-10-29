"""Factor evaluation tests use explicit, hand-checkable annual observations."""
from pathlib import Path

import pytest

from pit_memory.factor import ForwardReturn, evaluate_factor, load_returns, revenue_growth, spearman
from pit_memory.models import Fact


def fact(cik, year, value, *, amended=False):
    filed = f"{year + (2 if amended else 1)}-02-15"
    available = f"{year + (2 if amended else 1)}-02-16"
    return Fact(
        cik=str(cik), concept="us-gaap:Revenues", unit="USD", value=value,
        period_start=f"{year}-01-01", period_end=f"{year}-12-31",
        filed=filed, available_on=available, accession=f"{cik}-{year}-{int(amended)}",
        form="10-K/A" if amended else "10-K",
    )


def sample_facts():
    result = []
    for cik in (1, 2, 3):
        result.extend([fact(cik, 2019, 100), fact(cik, 2020, 100 + 10 * cik),
                       fact(cik, 2020, 150 - 10 * cik, amended=True)])
    return result


def sample_returns():
    return [ForwardReturn(str(cik), as_of, cik / 100)
            for as_of in ("2021-03-01", "2021-04-01") for cik in (1, 2, 3)]


def test_tie_aware_spearman_and_constant_inputs():
    assert spearman([1, 1, 2], [1, 2, 3]) == pytest.approx(0.866025403784)
    assert spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1)
    assert spearman([1, 1, 1], [1, 2, 3]) is None
    assert spearman([], []) is None
    with pytest.raises(ValueError, match="finite"):
        spearman([1, float("nan")], [1, 2])


def test_future_restatement_changes_growth_and_ic():
    result = evaluate_factor(sample_facts(), sample_returns(), bootstrap_samples=30)
    clean = result["summary"]["bitemporal"]
    leaky = result["summary"]["period_end"]
    assert clean["mean_ic"] == pytest.approx(1)
    assert clean["signal_rows_with_future_facts"] == 0
    assert leaky["mean_ic"] == pytest.approx(-1)
    assert leaky["signal_rows_with_future_facts"] == 6
    assert leaky["paired_mean_delta_ic_vs_bitemporal"] == pytest.approx(-2)
    assert leaky["paired_delta_ci95"] == pytest.approx([-2, -2])
    row = next(t for t in result["observations"] if t["backend"] == "bitemporal" and t["cik"] == "0000000001")
    assert row["signal"] == pytest.approx(0.1)
    assert row["current_fact_id"] != row["previous_fact_id"]


def test_missing_previous_year_and_zero_denominator_abstain():
    row = ForwardReturn("1", "2021-03-01", 0.1)
    assert revenue_growth([fact(1, 2020, 110)], row, "bitemporal")["status"] == "missing_previous_year"
    invalid = revenue_growth([fact(1, 2019, 0), fact(1, 2020, 110)], row, "bitemporal")
    assert invalid["signal"] is None
    assert invalid["status"] == "invalid_denominator_or_value"
    gap = revenue_growth([fact(1, 2018, 100), fact(1, 2020, 110)], row, "bitemporal")
    assert gap["signal"] is None


def test_small_sample_has_no_ic_or_ci():
    result = evaluate_factor(sample_facts(), [ForwardReturn("1", "2021-03-01", .1)], bootstrap_samples=20)
    assert result["summary"]["bitemporal"]["mean_ic"] is None
    assert result["summary"]["bitemporal"]["mean_ic_ci95"] is None
    rows = [ForwardReturn(str(cik), "2021-03-01", cik / 100) for cik in (1, 2, 3)]
    single_date = evaluate_factor(sample_facts(), rows)
    assert single_date["summary"]["bitemporal"]["mean_ic"] == pytest.approx(1)
    assert single_date["summary"]["bitemporal"]["mean_ic_ci95"] is None


def test_duplicates_and_invalid_return_inputs(tmp_path: Path):
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_factor([], [ForwardReturn("1", "2021-03-01", .1), ForwardReturn("0000000001", "2021-03-01", .2)])
    with pytest.raises(ValueError, match="finite"):
        ForwardReturn("1", "2021-03-01", float("inf"))
    with pytest.raises(ValueError, match="below"):
        ForwardReturn("1", "2021-03-01", -1.1)
    with pytest.raises(ValueError, match="follow"):
        ForwardReturn("1", "2021-03-01", .1, "2021-03-01")
    path = tmp_path / "returns.csv"
    path.write_text("cik,as_of,forward_return\n1,2021-03-01,\n")
    with pytest.raises(ValueError, match="invalid forward-return"):
        load_returns(path)
    path.write_text("cik,as_of,forward_return,return_end\n1,2021-03-01,0.05,2021-03-31\n")
    assert load_returns(path) == [ForwardReturn("1", "2021-03-01", .05, "2021-03-31")]


def test_paired_ic_uses_identical_entity_universe():
    # Entity 4 only has a future publication; naive retrieval gains coverage,
    # while paired comparisons use only the three entities known at the cutoff.
    facts = sample_facts() + [fact(4, 2019, 100), fact(4, 2020, 125, amended=True)]
    returns = sample_returns() + [ForwardReturn("4", "2021-03-01", -0.5)]
    result = evaluate_factor(facts, returns)
    daily = next(d for d in result["per_date"] if d["backend"] == "period_end" and d["as_of"] == "2021-03-01")
    assert daily["entities"] == 4
    assert daily["paired_entities"] == 3
    assert daily["paired_delta_ic"] == pytest.approx(-2)


def test_empty_evaluation_reports_null_rates():
    result = evaluate_factor([], [])
    assert result["summary"]["bitemporal"]["signal_coverage"] is None
    assert result["summary"]["bitemporal"]["mean_ic"] is None


def test_future_only_period_metadata_cannot_change_historical_growth():
    row = ForwardReturn("1", "2021-03-01", .1)
    known = [fact(1, 2019, 100), fact(1, 2020, 120)]
    future_period = Fact(
        cik="1", concept="us-gaap:Revenues", unit="USD", value=101,
        period_start="2019-01-06", period_end="2020-01-05",
        filed="2022-02-10", available_on="2022-02-11", accession="future-period",
        form="10-K",
    )
    before = revenue_growth(known, row, "bitemporal")
    after = revenue_growth([*known, future_period], row, "bitemporal")
    assert before["signal"] == pytest.approx(.2)
    assert after["signal"] == pytest.approx(.2)
    assert after["previous_fact_id"] == before["previous_fact_id"]


def test_identical_backends_get_identical_intervals():
    result = evaluate_factor(sample_facts(), sample_returns() + [
        ForwardReturn(str(cik), "2021-05-03", cik / 50) for cik in (1, 2, 3)], bootstrap_samples=50)
    overwrite, period_end = result["summary"]["overwrite"], result["summary"]["period_end"]
    assert overwrite["mean_ic"] == period_end["mean_ic"]
    assert overwrite["mean_ic_ci95"] == period_end["mean_ic_ci95"]
    assert overwrite["paired_delta_ci95"] == period_end["paired_delta_ci95"]




# ---- proposed: leakage flags, tie ranks, documented abstentions and parameters
def test_restated_previous_year_alone_marks_the_signal_as_leaked():
    row = ForwardReturn("1", "2021-03-01", .1)
    restated_2019 = Fact(cik="1", concept="us-gaap:Revenues", unit="USD", value=80,
                         period_start="2019-01-01", period_end="2019-12-31", filed="2021-06-01",
                         available_on="2021-06-02", accession="1-2019-restated", form="10-K/A")
    facts = [fact(1, 2019, 100), fact(1, 2020, 120), restated_2019]
    clean = revenue_growth(facts, row, "bitemporal")
    leaky = revenue_growth(facts, row, "period_end")
    assert clean["signal"] == pytest.approx(.2) and clean["answer_leakage"] is False
    assert leaky["signal"] == pytest.approx(.5)
    assert leaky["current_fact"]["available_on"] <= row.as_of  # only the previous year leaks
    assert leaky["answer_leakage"] is True


def test_future_fact_rows_without_a_signal_are_not_counted_as_signals():
    # Entity 5 has only a future FY2020 filing: period_end leaks it but cannot form growth.
    result = evaluate_factor(sample_facts() + [fact(5, 2020, 100, amended=True)],
                             sample_returns() + [ForwardReturn("5", "2021-03-01", .2)], bootstrap_samples=0)
    leaky = result["summary"]["period_end"]
    assert leaky["signal_rows"] == 6
    assert leaky["signal_rows_with_future_facts"] == 6


def test_spearman_averages_tied_ranks():
    # x ranks are [1, 2.5, 2.5, 4]; Pearson with [1, 2, 3, 4] is 4.5 / sqrt(4.5 * 5).
    assert spearman([1, 2, 2, 3], [1, 2, 3, 4]) == pytest.approx(4.5 / (4.5 * 5) ** .5)


def test_percentile_of_bootstrap_draws_interpolates():
    from pit_memory.factor import _percentile
    assert _percentile([4, 1, 3, 2], .25) == pytest.approx(1.75)


def test_negative_previous_value_abstains():
    row = ForwardReturn("1", "2021-03-01", 0.1)
    result = revenue_growth([fact(1, 2019, -100), fact(1, 2020, 110)], row, "bitemporal")
    assert result["signal"] is None
    assert result["status"] == "invalid_denominator_or_value"


def test_previous_period_must_end_300_to_430_days_earlier():
    row = ForwardReturn("1", "2021-03-01", 0.1)
    transition = Fact(cik="1", concept="us-gaap:Revenues", unit="USD", value=60,
                      period_start="2019-07-01", period_end="2020-06-30", filed="2020-08-14",
                      available_on="2020-08-17", accession="1-transition", form="10-K")
    result = revenue_growth([transition, fact(1, 2020, 110)], row, "bitemporal")
    assert result["status"] == "missing_previous_year"


def test_inconsistent_return_horizons_on_one_date_are_rejected():
    with pytest.raises(ValueError, match="inconsistent return_end"):
        evaluate_factor([], [ForwardReturn("1", "2021-03-01", .1, "2021-04-01"),
                             ForwardReturn("2", "2021-03-01", .1, "2021-05-03")])


def test_cross_sections_below_min_entities_have_no_ic():
    rows = [ForwardReturn(str(cik), "2021-03-01", cik / 100) for cik in (1, 2, 3)]
    result = evaluate_factor(sample_facts(), rows, min_entities=4)
    daily = next(d for d in result["per_date"] if d["backend"] == "period_end")
    assert daily["entities"] == 3
    assert daily["ic"] is None and daily["paired_ic"] is None


def test_identical_backends_get_identical_nondegenerate_intervals():
    # Per-date ICs differ, so a backend-specific seed would change the interval.
    returns = []
    for as_of, order in (("2021-03-01", (1, 2, 3)), ("2021-04-01", (2, 1, 3)),
                         ("2021-05-03", (3, 1, 2)), ("2021-06-01", (1, 3, 2))):
        returns += [ForwardReturn(str(cik), as_of, rank / 100) for cik, rank in zip((1, 2, 3), order)]
    result = evaluate_factor(sample_facts(), returns, bootstrap_samples=50)
    overwrite, period_end = result["summary"]["overwrite"], result["summary"]["period_end"]
    assert overwrite["mean_ic_ci95"][0] < overwrite["mean_ic_ci95"][1]
    assert overwrite["mean_ic_ci95"] == period_end["mean_ic_ci95"]


def test_previous_year_is_the_latest_period_inside_the_window():
    row = ForwardReturn("1", "2021-03-01", 0.1)
    # A fiscal-year change leaves two candidate prior periods 366 and 397 days back.
    november = Fact(cik="1", concept="us-gaap:Revenues", unit="USD", value=50,
                    period_start="2018-12-01", period_end="2019-11-30", filed="2020-01-15",
                    available_on="2020-01-16", accession="1-nov", form="10-K")
    result = revenue_growth([november, fact(1, 2019, 100), fact(1, 2020, 110)], row, "bitemporal")
    assert result["previous_period_end"] == "2019-12-31"
    assert result["signal"] == pytest.approx(0.1)


@pytest.mark.parametrize("kwargs", [
    {"bootstrap_samples": -1}, {"bootstrap_samples": 1.5}, {"bootstrap_samples": True},
    {"min_entities": 2}, {"min_entities": 3.5}, {"min_entities": True},
])
def test_factor_count_parameters_are_validated_even_with_no_observations(kwargs):
    with pytest.raises(ValueError, match="integer"):
        evaluate_factor([], [], **kwargs)
