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


@pytest.mark.parametrize("pointer", ['{"sha256":"../../outside"}', "[]", '{"sha256": 5}', "not json"])
def test_damaged_cache_pointer_is_never_followed_and_triggers_a_fresh_download(tmp_path, monkeypatch, pointer):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "CIK0000000123.latest.json").write_text(pointer)
    raw = json.dumps(payload([row()])).encode()
    monkeypatch.setattr("pit_memory.sec.urlopen", lambda request, timeout: BytesIO(raw))
    client = SecClient(cache, "Offline fixture contact@example.test")
    path = client.download_companyfacts("123")
    assert path.parent == cache and path.read_bytes() == raw
    assert json.loads((cache / "CIK0000000123.latest.json").read_text())["sha256"] in path.name


def test_truncated_snapshot_is_replaced_by_a_complete_download(tmp_path, monkeypatch):
    raw = json.dumps(payload([row()])).encode()
    sha = hashlib.sha256(raw).hexdigest()
    damaged = tmp_path / f"CIK0000000123.{sha}.json"
    damaged.write_bytes(raw[:10])  # an interrupted earlier write
    (tmp_path / "CIK0000000123.latest.json").write_text(json.dumps({"sha256": sha}))
    monkeypatch.setattr("pit_memory.sec.urlopen", lambda request, timeout: BytesIO(raw))
    client = SecClient(tmp_path, "Offline fixture contact@example.test")
    assert client.download_companyfacts("123").read_bytes() == raw
    assert client.fetch_companyfacts("123")["cik"] == 123
    assert not list(tmp_path.glob("*.part"))


def test_dropped_connections_and_truncated_bodies_are_retried(tmp_path, monkeypatch):
    import http.client
    raw = json.dumps(payload([row()])).encode()
    failures = [http.client.IncompleteRead(b"{", 100), ConnectionResetError("reset"), None]
    attempts = []

    def flaky(request, timeout):
        attempts.append(1)
        failure = failures[len(attempts) - 1]
        if failure is not None:
            raise failure
        return BytesIO(raw)

    monkeypatch.setattr("pit_memory.sec.urlopen", flaky)
    monkeypatch.setattr("pit_memory.sec.time.sleep", lambda _seconds: None)
    client = SecClient(tmp_path, "Offline fixture contact@example.test")
    assert client.download_companyfacts("123").read_bytes() == raw
    assert len(attempts) == 3
    monkeypatch.setattr("pit_memory.sec.urlopen", lambda request, timeout: BytesIO(raw[:20]))
    with pytest.raises(RuntimeError, match="failed after 4 attempt"):
        client.download_companyfacts("123", refresh=True)


def test_rejected_rows_with_non_finite_values_stay_json_safe():
    bad = row(val=float("nan"))
    result = parse_companyfacts(payload([bad, row()]), calendar=fake_calendar)
    assert len(result.facts) == 1
    assert result.rejected[0]["row"]["val"] == "nan"
    json.dumps(result.rejected, allow_nan=False)


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
            raise HTTPError(request.full_url, 429, "Too Many Requests", {"Retry-After": "3"}, None)
        return BytesIO(raw)

    monkeypatch.setattr("pit_memory.sec.urlopen", transient)
    monkeypatch.setattr("pit_memory.sec.time.sleep", sleeps.append)
    client = SecClient(tmp_path, "Offline fixture contact@example.test")
    client.download_companyfacts(123)
    assert len(calls) == 2
    assert 3.0 in sleeps  # Retry-After, not the default first backoff of 1 s

    def forbidden(request, timeout):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr("pit_memory.sec.urlopen", forbidden)
    with pytest.raises(RuntimeError, match=r"CIK 0000000123 failed: HTTP 403.*SEC_USER_AGENT") as error:
        client.download_companyfacts(123, refresh=True)
    assert error.value.__cause__.code == 403


def test_missing_companyfacts_names_the_cik(tmp_path, monkeypatch):
    def missing(request, timeout):
        raise HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr("pit_memory.sec.urlopen", missing)
    client = SecClient(tmp_path, "Offline fixture contact@example.test")
    with pytest.raises(RuntimeError, match=r"CIK 0000999999 .*HTTP 404.*no XBRL companyfacts"):
        client.download_companyfacts(999999)


@pytest.mark.parametrize("setting,value", [
    ("min_interval", float("nan")), ("min_interval", float("inf")),
    ("min_interval", "0.2"), ("min_interval", True),
    ("timeout", float("nan")), ("timeout", float("inf")),
    ("timeout", "30"), ("timeout", True),
    ("retries", 1.5), ("retries", float("nan")),
    ("retries", "3"), ("retries", True),
])
def test_invalid_client_settings_fail_before_network(tmp_path, setting, value):
    with pytest.raises(ValueError, match="min_interval"):
        SecClient(tmp_path, "Offline fixture contact@example.test", **{setting: value})


@pytest.mark.parametrize("digest", [123, [], "A" * 64])
def test_invalid_source_digest_is_a_validation_error(digest):
    with pytest.raises(ValueError, match="source_sha256"):
        parse_companyfacts(payload([row()]), source_sha256=digest)


@pytest.mark.parametrize("response", [[], None, 42, {"cik": 123, "facts": []},
                                      {"cik": 456, "facts": {}}, {"facts": {}}])
def test_wrong_download_shape_or_company_never_becomes_a_snapshot(tmp_path, monkeypatch, response):
    monkeypatch.setattr("pit_memory.sec.urlopen", lambda request, timeout: BytesIO(json.dumps(response).encode()))
    client = SecClient(tmp_path, "Offline fixture contact@example.test")
    with pytest.raises(ValueError, match="requested companyfacts CIK"):
        client.download_companyfacts(123)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("cached_raw", [b"not json", b"[]", b"null", b"\xff",
                                       b'{"cik":456,"facts":{}}',
                                       b'{"cik":123,"facts":[]}'])
def test_hash_valid_cache_still_requires_matching_companyfacts(tmp_path, monkeypatch, cached_raw):
    sha = hashlib.sha256(cached_raw).hexdigest()
    (tmp_path / f"CIK0000000123.{sha}.json").write_bytes(cached_raw)
    (tmp_path / "CIK0000000123.latest.json").write_text(json.dumps({"sha256": sha}))
    raw = json.dumps(payload([row()])).encode()
    downloads = []

    def download(request, timeout):
        downloads.append(request)
        return BytesIO(raw)

    monkeypatch.setattr("pit_memory.sec.urlopen", download)
    client = SecClient(tmp_path, "Offline fixture contact@example.test")
    assert client.download_companyfacts(123).read_bytes() == raw
    assert len(downloads) == 1


def test_invalid_response_encoding_is_retried(tmp_path, monkeypatch):
    raw = json.dumps(payload([row()])).encode()
    responses = iter([b"\xff", raw])
    monkeypatch.setattr("pit_memory.sec.urlopen", lambda request, timeout: BytesIO(next(responses)))
    monkeypatch.setattr("pit_memory.sec.time.sleep", lambda _seconds: None)
    client = SecClient(tmp_path, "Offline fixture contact@example.test")
    assert client.download_companyfacts(123).read_bytes() == raw


def test_simultaneous_refreshes_keep_both_snapshots_and_an_intact_pointer(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import os
    import threading

    raws = [json.dumps(payload([row(val=value)])).encode() for value in (100, 90)]
    responses = iter(raws)
    monkeypatch.setattr("pit_memory.sec.urlopen", lambda request, timeout: BytesIO(next(responses)))
    pointer_ready = threading.Barrier(2)
    replace = os.replace

    def concurrent_replace(source, destination):
        if str(destination).endswith("latest.json"):
            # Both clients have finished writing their pointer temporary files.
            # Reusing a fixed name here makes one replace lose the other's file.
            pointer_ready.wait(timeout=5)
        return replace(source, destination)

    monkeypatch.setattr("pit_memory.sec.os.replace", concurrent_replace)
    clients = [SecClient(tmp_path, "Offline fixture contact@example.test") for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(lambda client: client.download_companyfacts(123, refresh=True), clients))
    assert {path.read_bytes() for path in paths} == set(raws)
    pointer = json.loads((tmp_path / "CIK0000000123.latest.json").read_text())
    snapshot = tmp_path / f"CIK0000000123.{pointer['sha256']}.json"
    assert snapshot.read_bytes() in raws
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == pointer["sha256"]
    assert not list(tmp_path.glob("*.part"))


def test_failed_pointer_replace_keeps_previous_cache_and_cleans_temporary(tmp_path, monkeypatch):
    import os

    original = json.dumps(payload([row()])).encode()
    updated = json.dumps(payload([row(val=90)])).encode()
    responses = iter([original, updated])
    monkeypatch.setattr("pit_memory.sec.urlopen", lambda request, timeout: BytesIO(next(responses)))
    monkeypatch.setattr("pit_memory.sec.time.sleep", lambda _seconds: None)
    client = SecClient(tmp_path, "Offline fixture contact@example.test")
    prior = client.download_companyfacts(123)
    replace = os.replace

    def failed_replace(source, destination):
        if str(destination).endswith("latest.json"):
            raise OSError("simulated write failure")
        return replace(source, destination)

    monkeypatch.setattr("pit_memory.sec.os.replace", failed_replace)
    with pytest.raises(OSError, match="simulated write failure"):
        client.download_companyfacts(123, refresh=True)
    assert client.download_companyfacts(123) == prior
    assert prior.read_bytes() == original
    assert not list(tmp_path.glob("*.part"))




# ---- proposed: ingestion scope boundaries
@pytest.mark.parametrize("form", ["10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"])
def test_every_supported_annual_form_is_ingested(form):
    result = parse_companyfacts(payload([row(form=form)]), calendar=fake_calendar)
    assert [fact.form for fact in result.facts] == [form]


@pytest.mark.parametrize("start,kept", [("2019-01-11", False), ("2019-01-10", True),
                                        ("2018-12-21", True), ("2018-12-20", False),
                                        ("2019-02-01", False), ("2018-11-29", False)])
def test_annual_duration_window_is_355_to_375_days(start, kept):
    # Durations against end 2019-12-31: 354, 355, 375 and 376 days, then an
    # 11-month (333-day) and a 13-month (397-day) fiscal transition period.
    result = parse_companyfacts(payload([row(start=start)]), calendar=fake_calendar)
    assert (len(result.facts), result.skipped) == ((1, 0) if kept else (0, 1))
    if not kept:
        assert result.skipped_by_reason == {"period not 355-375 days": 1}


def test_skipped_rows_are_counted_by_reason():
    rows = [row(form="10-Q", start="2019-10-01"), row(form="8-K"), row(form="8-K"),
            {key: value for key, value in row().items() if key != "start"}, row()]
    result = parse_companyfacts(payload(rows), calendar=fake_calendar)
    assert len(result.facts) == 1 and result.skipped == 4
    assert result.skipped_by_reason == {"form 10-Q": 1, "annual-length period on form 8-K": 2,
                                        "instant fact (no start date)": 1}


class _Response(BytesIO):
    def __init__(self, raw, headers):
        super().__init__(raw)
        self.headers = headers


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "identity"])
def test_compressed_downloads_are_stored_decompressed_with_their_date(tmp_path, monkeypatch, encoding):
    import gzip
    import zlib
    raw = json.dumps(payload([row()])).encode()
    body = {"gzip": gzip.compress, "deflate": zlib.compress, "identity": lambda data: data}[encoding](raw)
    sent = []

    def respond(request, timeout):
        sent.append(request.get_header("Accept-encoding"))
        return _Response(body, {"Content-Encoding": encoding, "Last-Modified": "Thu, 22 Oct 2026 21:00:00 GMT"})

    monkeypatch.setattr("pit_memory.sec.urlopen", respond)
    client = SecClient(tmp_path, "Offline fixture contact@example.test")
    path = client.download_companyfacts(123)
    assert path.read_bytes() == raw
    assert path.name == f"CIK0000000123.{hashlib.sha256(raw).hexdigest()}.json"
    assert sent == ["gzip, deflate"]
    metadata = client.snapshot_metadata(123)
    assert metadata["last_modified"] == "Thu, 22 Oct 2026 21:00:00 GMT"
    assert metadata["downloaded_at"].endswith("+00:00")


def test_truncated_compressed_body_is_retried(tmp_path, monkeypatch):
    import gzip
    raw = json.dumps(payload([row()])).encode()
    bodies = [gzip.compress(raw)[:20], gzip.compress(raw)]
    monkeypatch.setattr("pit_memory.sec.urlopen",
                        lambda request, timeout: _Response(bodies.pop(0), {"Content-Encoding": "gzip"}))
    monkeypatch.setattr("pit_memory.sec.time.sleep", lambda seconds: None)
    client = SecClient(tmp_path, "Offline fixture contact@example.test")
    assert client.download_companyfacts(123).read_bytes() == raw
    assert bodies == []


def test_malformed_accession_is_rejected_and_source_url_is_canonical():
    result = parse_companyfacts(payload([row(accn="0000000123-20-1"), row()]), calendar=fake_calendar)
    assert len(result.rejected) == 1 and "accession" in result.rejected[0]["reason"]
    assert result.facts[0].source_url == (
        "https://www.sec.gov/Archives/edgar/data/123/000000012320000001/0000000123-20-000001-index.html")


# ---- proposed: retry policy and rate limit
def test_server_errors_and_url_errors_are_retried_honoring_capped_retry_after(tmp_path, monkeypatch):
    from urllib.error import URLError
    raw = json.dumps(payload([row()])).encode()
    failures = [HTTPError("u", 503, "Unavailable", {"Retry-After": "7"}, None),
                HTTPError("u", 500, "Error", {"Retry-After": "120"}, None),
                URLError("temporary failure in name resolution")]
    sleeps, calls = [], []

    def flaky(request, timeout):
        calls.append(request)
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1]
        return BytesIO(raw)

    monkeypatch.setattr("pit_memory.sec.urlopen", flaky)
    monkeypatch.setattr("pit_memory.sec.time.sleep", sleeps.append)
    client = SecClient(tmp_path, "Offline fixture contact@example.test")
    assert client.download_companyfacts(123).read_bytes() == raw
    assert len(calls) == 4
    backoff = [seconds for seconds in sleeps if seconds >= 1]
    assert backoff == [7.0, 30.0, 4]  # Retry-After, Retry-After capped at 30 s, 2**2


def test_consecutive_requests_are_spaced_by_min_interval(tmp_path, monkeypatch):
    raw = json.dumps(payload([row()])).encode()
    monkeypatch.setattr("pit_memory.sec.urlopen", lambda request, timeout: BytesIO(raw))
    clock, sleeps = [1000.0], []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("pit_memory.sec.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("pit_memory.sec.time.sleep", sleep)
    client = SecClient(tmp_path, "Offline fixture contact@example.test", min_interval=0.5)
    client.download_companyfacts(123)
    client.download_companyfacts(123, refresh=True)
    assert sleeps == [pytest.approx(0.5)]


def test_no_backoff_sleep_after_the_final_attempt(tmp_path, monkeypatch):
    def unavailable(request, timeout):
        raise HTTPError(request.full_url, 503, "Unavailable", {}, None)

    sleeps = []
    monkeypatch.setattr("pit_memory.sec.urlopen", unavailable)
    monkeypatch.setattr("pit_memory.sec.time.sleep", sleeps.append)
    client = SecClient(tmp_path, "Offline fixture contact@example.test", retries=2)
    with pytest.raises(RuntimeError, match="after 3 attempt"):
        client.download_companyfacts(123)
    assert [seconds for seconds in sleeps if seconds >= 1] == [1, 2]


def test_user_agent_needs_a_name_besides_the_address(tmp_path):
    with pytest.raises(ValueError, match="SEC_USER_AGENT"):
        SecClient(tmp_path, "contact@example.test")


@pytest.mark.parametrize("changes,reason", [
    ({"start": "2020-01-01", "end": "2019-12-31"}, "ends before it starts"),
    ({"start": "2019-03-02", "end": "2020-03-01"}, "ends after filing"),
])
def test_impossible_annual_rows_are_quarantined_with_their_reason(changes, reason):
    result = parse_companyfacts(payload([row(**changes)]), calendar=fake_calendar)
    assert result.facts == [] and result.skipped == 0
    assert reason in result.rejected[0]["reason"]
