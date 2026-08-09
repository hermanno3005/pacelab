import gzip
import logging

import pytest

from pacelab.account import Account
from pacelab.providers.http import HttpResponse
from pacelab.providers.intervals import IntervalsProvider, RateLimited

FIT_BYTES = b"\x00" * 8 + b".FIT" + b"payload-bytes"  # .FIT signature at offset 8
GPX_BYTES = b'<?xml version="1.0"?>\n<gpx version="1.1"><trk></trk></gpx>'


class StubHttp:
    def __init__(self, response):
        self.response = response

    def get(self, url, headers):
        return self.response


def _provider(response, tmp_path):
    return IntervalsProvider(Account("secret", "0"), StubHttp(response), cache_dir=tmp_path)


def test_download_decompresses_and_caches_the_original_fit(tmp_path):
    provider = _provider(HttpResponse(200, gzip.compress(FIT_BYTES)), tmp_path)
    path = provider.download("i100")
    assert path.suffix == ".fit"
    assert path.read_bytes() == FIT_BYTES  # decompressed to the original
    assert path.parent.name == "intervals-0"  # cache keyed by the SAME id as the store


def test_download_detects_a_gpx_original(tmp_path):
    provider = _provider(HttpResponse(200, gzip.compress(GPX_BYTES)), tmp_path)
    assert provider.download("i101").suffix == ".gpx"


def test_download_handles_uncompressed_bodies(tmp_path):
    # Not all bodies are gzipped — a raw FIT must still be written.
    provider = _provider(HttpResponse(200, FIT_BYTES), tmp_path)
    assert provider.download("i102").read_bytes() == FIT_BYTES


def test_missing_original_is_logged_as_an_expected_skip(tmp_path, caplog):
    # Strava-synced activities have no original → 404 → skip, don't crash (ADR-0008).
    # It recurs on every listing pass, so it logs at INFO: a steady state, not a fault.
    provider = _provider(HttpResponse(404, b"not found"), tmp_path)

    with caplog.at_level(logging.INFO, logger="pacelab.providers.intervals"):
        assert provider.download("i103") is None

    records = [r for r in caplog.records if r.name == "pacelab.providers.intervals"]
    assert [r.levelno for r in records] == [logging.INFO]
    assert "i103" in records[0].getMessage()


def test_cached_original_is_served_without_a_network_call(tmp_path):
    # Originals are immutable (ADR-0008): once cached, download() must not refetch —
    # the finalization pass (ADR-0012) re-analyses cached FITs days later.
    class ExplodingHttp:
        def get(self, url, headers):
            raise AssertionError("network hit for a cached original")

    first = _provider(HttpResponse(200, gzip.compress(FIT_BYTES)), tmp_path)
    path = first.download("i106")

    again = IntervalsProvider(Account("secret", "0"), ExplodingHttp(), cache_dir=tmp_path)
    assert again.download("i106") == path


def test_rate_limit_raises_instead_of_masquerading_as_missing(tmp_path):
    # A 429 is NOT "no original file" — it must abort the sync (honour Retry-After),
    # not skip-and-hammer the remaining activities (research doc §rate limits).
    resp = HttpResponse(429, b"rate limited", headers={"Retry-After": "90"})
    provider = _provider(resp, tmp_path)
    with pytest.raises(RateLimited) as exc:
        provider.download("i104")
    assert exc.value.retry_after_s == 90


def test_server_error_raises_rather_than_skipping(tmp_path):
    # 5xx is a transient upstream failure, not a missing original — surface it.
    provider = _provider(HttpResponse(503, b"oops"), tmp_path)
    with pytest.raises(RuntimeError):
        provider.download("i105")
