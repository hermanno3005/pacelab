import logging
import math

from pacelab.analyze import ActivityResult
from pacelab.config import Config
from pacelab.providers.intervals import ActivityRef
from pacelab.store import ResultStore
from pacelab.sync import sync
from pacelab.weather.conditions import Conditions


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
    def __init__(self, refs, gpx_path, publish_fails=False):
        self._refs = refs
        self._gpx = gpx_path
        self.downloaded = []
        self.descriptions = {}
        self._publish_fails = publish_fails

    def list_activities(self, oldest, newest):
        return self._refs

    def download(self, activity_id):
        self.downloaded.append(activity_id)
        return self._gpx

    def fetch_description(self, activity_id):
        if self._publish_fails:
            raise RuntimeError("intervals.icu down")
        return self.descriptions.get(activity_id)

    def update_description(self, activity_id, text):
        self.descriptions[activity_id] = text


class StubService:
    def conditions_at(self, lat, lon, t):
        return Conditions(20.0, 50.0, 0.0, 0.0, 0.0, 1013.0)


def _write_trackless_gpx(path):
    # A valid GPX with no trackpoints — e.g. a treadmill/strength activity (FR-1.4).
    path.write_text('<?xml version="1.0"?>\n<gpx version="1.1" '
                    'xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
                    '</trkseg></trk></gpx>\n')


def test_sync_skips_activities_with_no_gps_track(tmp_path):
    # A treadmill run is type Run but has no GPS — flagged, not stored (FR-1.4).
    empty = tmp_path / "empty.gpx"
    _write_trackless_gpx(empty)
    store = ResultStore(tmp_path / "db")
    provider = StubProvider([ActivityRef("i900", "2024-07-01", "Run", "Treadmill")], empty)

    outcomes = dict(sync(provider, StubService(), store, Config(), "2024-01-01", "2024-12-31",
                         account_id="acct"))

    assert outcomes["i900"] == "no-track"
    assert store.load("i900", account_id="acct") is None  # not stored


def test_sync_marks_unparseable_formats_unsupported(tmp_path):
    # intervals.icu originals can be TCX; we cache them (they're the user's data) but
    # have no TCX parser — sync must say so, not feed them to the GPX adapter.
    tcx = tmp_path / "i300.tcx"
    tcx.write_text('<?xml version="1.0"?><TrainingCenterDatabase></TrainingCenterDatabase>')
    store = ResultStore(tmp_path / "db")
    provider = StubProvider([ActivityRef("i300", "2024-07-03", "Run", "C")], tcx)

    outcomes = dict(sync(provider, StubService(), store, Config(), "2024-01-01", "2024-12-31",
                         account_id="acct"))

    assert outcomes["i300"] == "unsupported"
    assert store.load("i300", account_id="acct") is None


def test_activity_without_weather_yet_is_deferred_not_stored(tmp_path):
    # A run more recent than ERA5's publication lag can't be analysed yet — report it
    # and leave nothing in the store, so the next sync retries it.
    from pacelab.weather.service import WeatherUnavailable

    class NoWeatherService:
        def conditions_at(self, lat, lon, t):
            raise WeatherUnavailable("no weather for 2026-07-05 yet")

    gpx = tmp_path / "a.gpx"
    _write_gpx(gpx)
    store = ResultStore(tmp_path / "db")
    provider = StubProvider([ActivityRef("i500", "2026-07-05", "Run", "Night Laufen")], gpx)

    outcomes = dict(sync(provider, NoWeatherService(), store, Config(), "2026-01-01",
                         "2026-12-31", account_id="acct"))

    assert outcomes["i500"] == "no-weather"
    assert store.load("i500", account_id="acct") is None


class LaggedWeather:
    """Archive service that starts empty (ERA5 lag) and gains the day later."""

    def __init__(self, available=False):
        self.available = available

    def conditions_at(self, lat, lon, t):
        from pacelab.weather.service import WeatherUnavailable

        if not self.available:
            raise WeatherUnavailable("not published yet")
        return Conditions(20.0, 50.0, 0.0, 0.0, 0.0, 1013.0, 100.0)


def test_recent_run_is_analysed_provisionally_then_finalized(tmp_path):
    # ADR-0012: inside ERA5's lag a run gets a forecast-tier preview (tilde in the
    # annotation, provisional in the store); once the archive catches up, the same sync
    # command finalizes it — recomputed, republished without the tilde.
    gpx = tmp_path / "a.gpx"
    _write_gpx(gpx)
    store = ResultStore(tmp_path / "db")
    provider = StubProvider([ActivityRef("i500", "2026-07-05", "Run", "Night Laufen")], gpx)
    archive = LaggedWeather(available=False)
    forecast = StubService()  # forecast tier always has recent days

    outcomes = dict(sync(provider, archive, store, Config(), "2026-01-01", "2026-12-31",
                         account_id="acct", provisional_service=forecast))

    assert outcomes["i500"] == "provisional"
    assert store.is_provisional("i500", account_id="acct")
    assert "NP ~" in provider.descriptions["i500"]

    archive.available = True  # ~a week later, ERA5 has the day
    outcomes = dict(sync(provider, archive, store, Config(), "2026-01-01", "2026-12-31",
                         account_id="acct", provisional_service=forecast))

    assert outcomes["i500"] == "finalized"
    assert not store.is_provisional("i500", account_id="acct")
    assert "NP ~" not in provider.descriptions["i500"]
    assert "PaceLab" in provider.descriptions["i500"]


def test_no_weather_on_either_tier_defers(tmp_path):
    gpx = tmp_path / "a.gpx"
    _write_gpx(gpx)
    store = ResultStore(tmp_path / "db")
    provider = StubProvider([ActivityRef("i501", "2026-07-07", "Run", "Just now")], gpx)

    outcomes = dict(sync(provider, LaggedWeather(False), store, Config(), "2026-01-01",
                         "2026-12-31", account_id="acct",
                         provisional_service=LaggedWeather(False)))

    assert outcomes["i501"] == "no-weather"
    assert store.load("i501", account_id="acct") is None


def test_finalized_activity_is_then_skipped(tmp_path):
    gpx = tmp_path / "a.gpx"
    _write_gpx(gpx)
    store = ResultStore(tmp_path / "db")
    provider = StubProvider([ActivityRef("i500", "2026-07-05", "Run", "N")], gpx)
    archive = LaggedWeather(available=True)

    dict(sync(provider, archive, store, Config(), "2026-01-01", "2026-12-31",
              account_id="acct", provisional_service=StubService()))
    outcomes = dict(sync(provider, archive, store, Config(), "2026-01-01", "2026-12-31",
                         account_id="acct", provisional_service=StubService()))

    assert outcomes["i500"] == "skip"


def test_non_run_activities_are_ignored_without_downloading(tmp_path):
    # PaceLab's physics are running physics (Minetti grade, running heat curve) — a bike
    # ride analysed with them is wrong on every axis. Only outdoor runs pass; nothing is
    # downloaded for the rest (rate-limit friendly).
    gpx = tmp_path / "a.gpx"
    _write_gpx(gpx)
    store = ResultStore(tmp_path / "db")
    provider = StubProvider(
        [ActivityRef("i600", "2026-07-03", "Ride", "München Rennrad"),
         ActivityRef("i601", "2026-07-04", "TrailRun", "Morning Trailrun"),
         ActivityRef("i602", "2026-07-04", "VirtualRide", "Zwift"),
         ActivityRef("i603", "2026-07-05", None, "Mystery")],
        gpx,
    )

    outcomes = dict(sync(provider, StubService(), store, Config(), "2026-01-01", "2026-12-31",
                         account_id="acct"))

    assert outcomes["i600"] == "not-a-run"
    assert outcomes["i602"] == "not-a-run"
    assert outcomes["i603"] == "not-a-run"
    assert outcomes["i601"] == "ok"  # trail runs are runs
    assert provider.downloaded == ["i601"]  # rides never downloaded
    assert store.load("i600", account_id="acct") is None


def test_sync_skips_current_downloads_new_and_stores(tmp_path):
    gpx = tmp_path / "a.gpx"
    _write_gpx(gpx)
    store = ResultStore(tmp_path / "db")
    # i100 is already current (under the current model version); i200 is new.
    store.save("i100", ActivityResult(0, 0, 0, 0, 0, 0, []), Config().model_version, account_id="acct")
    provider = StubProvider(
        [ActivityRef("i100", "2024-07-01", "Run", "A"), ActivityRef("i200", "2024-07-02", "Run", "B")],
        gpx,
    )

    outcomes = dict(sync(provider, StubService(), store, Config(), "2024-01-01", "2024-12-31",
                         account_id="acct"))

    assert provider.downloaded == ["i200"]  # current one never downloaded (rate-limit friendly)
    assert outcomes["i100"] == "skip"
    assert outcomes["i200"] == "ok"
    assert store.is_current("i200", Config().model_version, account_id="acct")
    # Ambient publish (ADR-0011): the analysed run got its annotation written and marked.
    assert "PaceLab" in provider.descriptions["i200"]
    assert not store.needs_publish("i200", Config().model_version, account_id="acct")


def test_publish_failure_does_not_fail_the_sync(tmp_path, caplog):
    gpx = tmp_path / "a.gpx"
    _write_gpx(gpx)
    store = ResultStore(tmp_path / "db")
    provider = StubProvider([ActivityRef("i200", "2024-07-02", "Run", "B")], gpx,
                            publish_fails=True)

    with caplog.at_level(logging.WARNING, logger="pacelab.publish.publisher"):
        outcomes = dict(sync(provider, StubService(), store, Config(),
                             "2024-01-01", "2024-12-31", account_id="acct"))

    assert any("publish failed for i200" in r.getMessage() for r in caplog.records)
    assert outcomes["i200"] == "publish-failed"  # analysed and stored, annotation pending
    assert store.is_current("i200", Config().model_version, account_id="acct")
    assert store.needs_publish("i200", Config().model_version, account_id="acct")
