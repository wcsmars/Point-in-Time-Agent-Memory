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
