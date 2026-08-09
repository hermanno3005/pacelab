"""The store-driven reconciliation pass (ADR-0016)."""

import logging
import math

import pytest

from pacelab.analyze import ActivityResult, SegmentResult
from pacelab.config import Config
from pacelab.recompute import recompute
from pacelab.snapshot import SnapshotError
from pacelab.store import ResultStore
from pacelab.weather.conditions import Conditions
from pacelab.weather.service import WeatherUnavailable

ACCOUNT = "intervals-i1"
OLD_VERSION = "0.2.0"


def _write_gpx(path):
    lat = 48.0
    lon_step = 10 / (111_320 * math.cos(math.radians(lat)))
    body = "".join(
        f'<trkpt lat="{lat:.6f}" lon="{i * lon_step:.6f}"><ele>{100 + i * 0.3:.1f}</ele>'
        f'<time>2023-07-04T12:{(i * 3) // 60:02d}:{(i * 3) % 60:02d}Z</time></trkpt>\n'
        for i in range(60)
    )
    path.write_text(
        '<?xml version="1.0"?>\n<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
        f'<trk><trkseg>\n{body}</trkseg></trk></gpx>\n'
    )


class StubProvider:
    """Downloads from the cache and writes descriptions — never lists (ADR-0016)."""

    def __init__(self, gpx_path, publish_fails=False, download_raises=()):
        self._gpx = gpx_path
        self._publish_fails = publish_fails
        self._download_raises = set(download_raises)
        self.downloaded = []
        self.descriptions = {}

    def list_activities(self, oldest, newest):  # pragma: no cover - must never be called
        raise AssertionError("the recompute pass is store-driven, not provider-driven")

    def download(self, activity_id):
        if activity_id in self._download_raises:
            raise RuntimeError("container killed mid-pass")
        self.downloaded.append(activity_id)
        return self._gpx

    def fetch_description(self, activity_id):
        if self._publish_fails:
            raise RuntimeError("intervals.icu down")
        return self.descriptions.get(activity_id)

    def update_description(self, activity_id, text):
        self.descriptions[activity_id] = text


class ArchiveService:
    def conditions_at(self, lat, lon, t):
        return Conditions(20.0, 50.0, 0.0, 0.0, 0.0, 1013.0)


class LaggingArchive:
    """ERA5 before it has published the day — what a fresh provisional still hits."""

    def conditions_at(self, lat, lon, t):
        raise WeatherUnavailable("no archive weather for 2026-07-27 yet")


class _LaggingOnce(ArchiveService):
    """The archive as it is mid-lag: the oldest run is still missing, the rest are there.

    One raise is one skipped activity — ``WeatherUnavailable`` propagates out of the first
    segment enrichment, so no later segment of that run asks again.
    """

    def __init__(self):
        self._calls = 0

    def conditions_at(self, lat, lon, t):
        self._calls += 1
        if self._calls == 1:
            raise WeatherUnavailable("no archive weather for 2026-07-27 yet")
        return super().conditions_at(lat, lon, t)


def _stub_result():
    seg = SegmentResult(0, 100.0, 0.0, 30.0, 12.0, 55.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                        300.0, 298.0, False)
    return ActivityResult(observed_pace=300.0, np_pace=298.0, cost_grade=2.0,
                          cost_heat=1.0, cost_wind=0.0, distance_m=100.0, segments=[seg])


def _stranded_provisional(store, activity_id="i100"):
    """A preview that fell out of watch's window: provisional *and* stale, published."""
    store.save(activity_id, _stub_result(), OLD_VERSION, account_id=ACCOUNT, provisional=True)
    store.mark_published(activity_id, OLD_VERSION, account_id=ACCOUNT)


def _fixture(tmp_path, **provider_kwargs):
    gpx = tmp_path / "run.gpx"
    _write_gpx(gpx)
    return StubProvider(gpx, **provider_kwargs), ResultStore(tmp_path / "pacelab.db")


def test_recompute_finalizes_a_stranded_provisional(tmp_path):
    # The acceptance case: provisional and stale, outside watch's window, so no sync
    # will ever reach it. The pass finalizes it against the archive and republishes.
    provider, store = _fixture(tmp_path)
    _stranded_provisional(store)
    config = Config()

    outcomes = recompute(provider, ArchiveService(), store, config, ACCOUNT)

    assert outcomes == [("i100", "finalized")]
    assert store.is_current("i100", config.model_version, account_id=ACCOUNT)
    assert not store.is_provisional("i100", account_id=ACCOUNT)
    assert not store.needs_publish("i100", config.model_version, account_id=ACCOUNT)
    assert "PaceLab" in provider.descriptions["i100"]
    assert store.needs_recompute(config.model_version, ACCOUNT) == []  # settled


def test_a_provisional_inside_the_lag_is_skipped_not_republished(tmp_path):
    # Archive-tier only: the pass may improve a ~ preview into a final result, never
    # overwrite one guess with another. sync picks it up via the forecast tier instead.
    provider, store = _fixture(tmp_path)
    _stranded_provisional(store)
    config = Config()

    outcomes = recompute(provider, LaggingArchive(), store, config, ACCOUNT)

    assert outcomes == [("i100", "no-weather")]
    assert store.is_provisional("i100", account_id=ACCOUNT)  # untouched
    assert store.is_current("i100", OLD_VERSION, account_id=ACCOUNT)
    assert provider.descriptions == {}
    assert store.needs_recompute(config.model_version, ACCOUNT) == ["i100"]  # next pass


def test_a_publish_failure_leaves_the_row_for_the_next_pass(tmp_path, caplog):
    # Analysis converges absolutely; publishing retries forever (ADR-0016).
    provider, store = _fixture(tmp_path, publish_fails=True)
    _stranded_provisional(store)
    config = Config()

    with caplog.at_level(logging.WARNING, logger="pacelab.publish.publisher"):
        outcomes = recompute(provider, ArchiveService(), store, config, ACCOUNT)

    assert any("publish failed" in r.getMessage() for r in caplog.records)
    assert outcomes == [("i100", "publish-failed")]
    assert store.is_current("i100", config.model_version, account_id=ACCOUNT)
    assert store.needs_publish("i100", config.model_version, account_id=ACCOUNT)
    assert store.needs_recompute(config.model_version, ACCOUNT) == ["i100"]


def test_a_crash_mid_pass_leaves_no_row_half_updated(tmp_path):
    # Interleaved analyse→save→publish: at every crash point each row is either old and
    # annotated old, or new and annotated new. The next pass resumes with no state.
    provider, store = _fixture(tmp_path, download_raises=["i200"])
    _stranded_provisional(store, "i100")
    _stranded_provisional(store, "i200")
    config = Config()

    with pytest.raises(RuntimeError):
        recompute(provider, ArchiveService(), store, config, ACCOUNT)

    assert store.is_current("i100", config.model_version, account_id=ACCOUNT)
    assert not store.needs_publish("i100", config.model_version, account_id=ACCOUNT)
    assert store.is_current("i200", OLD_VERSION, account_id=ACCOUNT)
    assert store.is_provisional("i200", account_id=ACCOUNT)
    assert store.needs_recompute(config.model_version, ACCOUNT) == ["i200"]


def test_a_settled_corpus_costs_one_query_and_no_downloads(tmp_path):
    provider, store = _fixture(tmp_path)
    config = Config()
    store.save("i100", _stub_result(), config.model_version, account_id=ACCOUNT)
    store.mark_published("i100", config.model_version, account_id=ACCOUNT)

    assert recompute(provider, ArchiveService(), store, config, ACCOUNT) == []
    assert provider.downloaded == []


def test_force_re_analyses_rows_that_have_not_drifted(tmp_path):
    # Exercises a pipeline change without editing config.py.
    provider, store = _fixture(tmp_path)
    config = Config()
    store.save("i100", _stub_result(), config.model_version, account_id=ACCOUNT)
    store.mark_published("i100", config.model_version, account_id=ACCOUNT)

    outcomes = recompute(provider, ArchiveService(), store, config, ACCOUNT, force=True)

    assert outcomes == [("i100", "ok")]
    assert provider.downloaded == ["i100"]
    assert store.load("i100", account_id=ACCOUNT).distance_m > 400  # re-analysed


def test_an_activity_with_no_downloadable_original_is_reported_not_stored(tmp_path):
    provider, store = _fixture(tmp_path)
    provider.download = lambda activity_id: None
    _stranded_provisional(store)
    config = Config()

    assert recompute(provider, ArchiveService(), store, config, ACCOUNT) == [("i100", "no-file")]
    assert store.is_current("i100", OLD_VERSION, account_id=ACCOUNT)


def test_an_unparseable_original_is_reported_not_fed_to_the_gpx_adapter(tmp_path):
    provider, store = _fixture(tmp_path)
    tcx = tmp_path / "i100.tcx"
    tcx.write_text('<?xml version="1.0"?><TrainingCenterDatabase></TrainingCenterDatabase>')
    provider._gpx = tcx
    _stranded_provisional(store)
    config = Config()

    assert recompute(provider, ArchiveService(), store, config, ACCOUNT) == [("i100", "unsupported")]
    assert store.is_current("i100", OLD_VERSION, account_id=ACCOUNT)


def test_a_current_row_that_was_never_annotated_is_published_not_re_analysed(tmp_path):
    # Analysis converges absolutely — once per activity per bump. A row whose publish
    # keeps failing stays enumerated forever, so it must not re-download and re-analyse
    # on every 15-minute tick as well.
    provider, store = _fixture(tmp_path)
    config = Config()
    store.save("i100", _stub_result(), config.model_version, account_id=ACCOUNT)

    outcomes = recompute(provider, ArchiveService(), store, config, ACCOUNT)

    assert outcomes == [("i100", "ok")]
    assert provider.downloaded == []
    assert not store.needs_publish("i100", config.model_version, account_id=ACCOUNT)
    assert store.load("i100", account_id=ACCOUNT).distance_m == 100.0  # stub, untouched


def test_a_version_bump_snapshots_before_the_first_row_is_rewritten(tmp_path):
    # ADR-0018: the snapshot guards the moment of maximal damage — every row rewritten
    # and every public description republished, unattended. It must land before the
    # first write, not alongside it.
    provider, store = _fixture(tmp_path)
    _stranded_provisional(store, "i100")
    _stranded_provisional(store, "i200")
    config = Config()
    events = _watch_writes(store)

    recompute(provider, ArchiveService(), store, config, ACCOUNT,
              before_rewrite=lambda: events.append("snapshot"))

    assert events[0] == "snapshot"
    assert events.count("save") == 2
    assert events.count("snapshot") == 1  # once per pass, not once per activity


def test_stale_rows_that_the_pass_only_skips_take_no_snapshot(tmp_path):
    # The bug of #37: after a bump, activities still inside ERA5's publication lag stay
    # stamped at the old version for days. They are enumerated every tick and skipped
    # `no-weather` — untouched. "Stale rows exist" fired the guard every 15 minutes and
    # evicted the one snapshot the bump had correctly produced.
    provider, store = _fixture(tmp_path)
    _stranded_provisional(store)
    config = Config()
    taken = []

    outcomes = recompute(provider, LaggingArchive(), store, config, ACCOUNT,
                         before_rewrite=lambda: taken.append("snapshot"))

    assert outcomes == [("i100", "no-weather")]
    assert taken == []


def test_a_bump_still_snapshots_when_an_in_lag_row_is_enumerated_first(tmp_path):
    # The mixed corpus a bump actually leaves: a few in-lag rows the pass will decline
    # and the rest it will rewrite. The declined rows must not suppress the snapshot,
    # and it must still land before the first row that *is* rewritten.
    provider, store = _fixture(tmp_path)
    _stranded_provisional(store, "i100")  # enumerated first, skipped no-weather
    _stranded_provisional(store, "i200")
    config = Config()
    events = _watch_writes(store)

    recompute(provider, _LaggingOnce(), store, config, ACCOUNT,
              before_rewrite=lambda: events.append("snapshot"))

    assert events == ["snapshot", "save"]
    assert store.is_current("i200", config.model_version, account_id=ACCOUNT)


def test_a_settled_corpus_takes_no_snapshot(tmp_path):
    # Rejected in ADR-0018: snapshotting every tick, which writes to the aged SD card
    # 96 times a day to capture nothing.
    provider, store = _fixture(tmp_path)
    config = Config()
    store.save("i100", _stub_result(), config.model_version, account_id=ACCOUNT)
    store.mark_published("i100", config.model_version, account_id=ACCOUNT)
    taken = []

    recompute(provider, ArchiveService(), store, config, ACCOUNT,
              before_rewrite=lambda: taken.append("snapshot"))

    assert taken == []


def test_a_publish_only_retry_takes_no_snapshot(tmp_path, caplog):
    # This row is enumerated every pass until intervals.icu accepts the write, and
    # nothing about it is rewritten. Snapshotting here would fire on a loop.
    provider, store = _fixture(tmp_path, publish_fails=True)
    config = Config()
    store.save("i100", _stub_result(), config.model_version, account_id=ACCOUNT)
    taken = []

    with caplog.at_level(logging.WARNING, logger="pacelab.publish.publisher"):
        outcomes = recompute(provider, ArchiveService(), store, config, ACCOUNT,
                             before_rewrite=lambda: taken.append("snapshot"))

    assert any("publish failed" in r.getMessage() for r in caplog.records)
    assert outcomes == [("i100", "publish-failed")]
    assert taken == []
    assert provider.downloaded == []


def test_a_forced_pass_snapshots_because_it_rewrites_everything(tmp_path):
    provider, store = _fixture(tmp_path)
    config = Config()
    store.save("i100", _stub_result(), config.model_version, account_id=ACCOUNT)
    store.mark_published("i100", config.model_version, account_id=ACCOUNT)
    taken = []

    recompute(provider, ArchiveService(), store, config, ACCOUNT, force=True,
              before_rewrite=lambda: taken.append("snapshot"))

    assert taken == ["snapshot"]


def test_a_failed_snapshot_aborts_the_pass_with_the_corpus_untouched(tmp_path):
    # ADR-0018: the recompute must not run unprotected. Nothing is lost by stopping —
    # the pass is derived from the store, so a skipped pass looks like one not started.
    provider, store = _fixture(tmp_path)
    _stranded_provisional(store)
    config = Config()

    def failing_snapshot():
        raise SnapshotError("no space left on device")

    with pytest.raises(SnapshotError):
        recompute(provider, ArchiveService(), store, config, ACCOUNT,
                  before_rewrite=failing_snapshot)

    # The original was downloaded and analysed before the guard could know a rewrite was
    # imminent — both are reads. Nothing that outlives the pass was written.
    assert provider.descriptions == {}
    assert store.is_current("i100", OLD_VERSION, account_id=ACCOUNT)
    assert store.is_provisional("i100", account_id=ACCOUNT)
    assert store.needs_recompute(config.model_version, ACCOUNT) == ["i100"]  # next tick


def _watch_writes(store):
    """Return an event log that records ``"save"`` for every row the pass rewrites.

    The snapshot's contract is about *writes*, not about reaching an activity: the pass
    downloads and analyses before it knows whether the row can be rewritten at all.
    """
    events = []
    original = store.save

    def recording_save(*args, **kwargs):
        events.append("save")
        return original(*args, **kwargs)

    store.save = recording_save
    return events
