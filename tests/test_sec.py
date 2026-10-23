import hashlib
from io import BytesIO
import json
from urllib.error import HTTPError

import pytest

from pit_memory.sec import SecClient, parse_companyfacts


def row(**changes):
    entry = dict(start="2019-01-01", end="2019-12-31", val=100, accn="0000000123-20-000001", filed="2020-02-14", form="10-K", fy=2020, fp="FY")
    entry.update(changes)
    return entry


def payload(rows):
    return {"cik": 123, "entityName": "Synthetic Example", "facts": {"us-gaap": {"Revenues": {"units": {"USD": rows}}}}}


def fake_calendar(_filed):
    return "2022-01-03"


def test_parse_preserves_original_and_restatement_without_fy_keying():
    data = payload([row(), row(val=90, filed="2021-03-01", accn="0000000123-21-000001", fy=2021)])
    result = parse_companyfacts(data, calendar=fake_calendar)
    assert [f.value for f in result.facts] == [100, 90]
    assert {f.period_end for f in result.facts} == {"2019-12-31"}
    assert {f.concept for f in result.facts} == {"us-gaap:Revenues"}
    assert len(result.rejected) == 0


def test_reject_missing_dates_nonfinite_values_malformed_accession():
    result = parse_companyfacts(payload([row(filed=None), row(val=float("nan")), row(accn=""), row(end="2021-12-31")]), calendar=fake_calendar)
    assert len(result.facts) == 0
    assert len(result.rejected) == 3
    assert result.skipped == 1  # multi-year duration is outside annual-flow scope


def test_skip_quarterly_instant_and_nonannual_forms():
    instant = row()
    del instant["start"]
    result = parse_companyfacts(payload([row(start="2019-10-01"), instant, row(form="10-Q")]), calendar=fake_calendar)
    assert result.skipped == 3
    assert not result.facts


def test_dedupes_same_payload_record_and_qualifies_namespace():
    data = payload([row(), row()])
    data["facts"]["ifrs-full"] = {"Revenues": {"units": {"USD": [row()]}}}
    result = parse_companyfacts(data, calendar=fake_calendar)
    assert len(result.facts) == 2
    assert {f.concept for f in result.facts} == {"us-gaap:Revenues", "ifrs-full:Revenues"}


def test_accept_exact_raw_snapshot_digest():
    raw = json.dumps(payload([row()]), indent=4).encode()
    sha = hashlib.sha256(raw).hexdigest()
    result = parse_companyfacts(json.loads(raw), calendar=fake_calendar, source_sha256=sha)
    assert result.source_sha256 == sha
    assert result.facts[0].source_sha256 == sha
    assert result.facts[0].source_url.endswith("0000000123-20-000001-index.html")


def test_requires_real_user_agent_and_rate_bound(tmp_path, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(ValueError, match="SEC_USER_AGENT"):
        SecClient(tmp_path)
    with pytest.raises(ValueError, match="min_interval"):
        SecClient(tmp_path, "Synthetic Test contact@example.test", min_interval=0.01)


def test_cache_snapshot_integrity_without_network(tmp_path, monkeypatch):
    raw = json.dumps(payload([row()])).encode()
    sha = hashlib.sha256(raw).hexdigest()
    cached = tmp_path / f"CIK0000000123.{sha}.json"
    cached.write_bytes(raw)
    (tmp_path / "CIK0000000123.latest.json").write_text(json.dumps({"sha256": sha}))
    client = SecClient(tmp_path, "Offline fixture contact@example.test")
    assert client.download_companyfacts("123") == cached
    assert client.fetch_companyfacts("123")["cik"] == 123


def test_malformed_cache_digest_rejected(tmp_path):
    (tmp_path / "CIK0000000123.latest.json").write_text('{"sha256":"../../outside"}')
    client = SecClient(tmp_path, "Offline fixture contact@example.test")
    with pytest.raises(ValueError, match="digest"):
        client.download_companyfacts("123")


def test_download_refresh_preserves_raw_snapshots_and_exact_bytes(tmp_path, monkeypatch):
    raw_original = json.dumps(payload([row()]), indent=2).encode()
    raw_amended = json.dumps(payload([row(), row(val=90, filed="2021-03-01", accn="0000000123-21-000001")]), indent=4).encode()
    calls = []
    responses = iter([raw_original, raw_amended])

    def fake_urlopen(request, timeout):
        calls.append(request)
        assert request.headers["User-agent"] == "Offline fixture contact@example.test"
        return BytesIO(next(responses))

    monkeypatch.setattr("pit_memory.sec.urlopen", fake_urlopen)
    monkeypatch.setattr("pit_memory.sec.time.sleep", lambda _seconds: None)
    client = SecClient(tmp_path, "Offline fixture contact@example.test")
    original = client.download_companyfacts(123)
    assert original.read_bytes() == raw_original
    assert client.download_companyfacts(123) == original
    assert len(calls) == 1
    refreshed = client.download_companyfacts(123, refresh=True)
    assert refreshed != original
    assert original.read_bytes() == raw_original
    assert refreshed.read_bytes() == raw_amended
    assert hashlib.sha256(raw_amended).hexdigest() in refreshed.name


def test_transient_errors_retry_but_authorization_errors_stop(tmp_path, monkeypatch):
    sleeps = []
    calls = []
    raw = json.dumps(payload([row()])).encode()

    def transient(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 429, "Too Many Requests", {"Retry-After": "1"}, None)
        return BytesIO(raw)

    monkeypatch.setattr("pit_memory.sec.urlopen", transient)
    monkeypatch.setattr("pit_memory.sec.time.sleep", sleeps.append)
    client = SecClient(tmp_path, "Offline fixture contact@example.test")
    client.download_companyfacts(123)
    assert len(calls) == 2
    assert 1.0 in sleeps

    def forbidden(request, timeout):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr("pit_memory.sec.urlopen", forbidden)
    with pytest.raises(HTTPError) as error:
        client.download_companyfacts(123, refresh=True)
    assert error.value.code == 403
