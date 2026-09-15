"""The FIT Source Adapter against a checked-in fixture (#60).

FIT is the adapter every ``.fit`` original goes through (ADR-0003), so it is exercised on
the same path GPX takes: parse to Trackpoints, segment, analyse. The fixture is written by
``tests/fixtures/make_fit_fixture.py``, which is also where its ground truth lives.
"""

import math
from pathlib import Path

from fixtures.make_fit_fixture import EXPECTED, FIXTURE, build
from pacelab.app import analyze_file
from pacelab.config import Config
from pacelab.ingest.fit import FitAdapter
from pacelab.weather.conditions import Conditions


class StubService:
    def conditions_at(self, lat, lon, t):
        return Conditions(20.0, 50.0, 0.0, 0.0, 0.0, 1013.0)


def test_checked_in_fixture_is_what_the_generator_writes():
    assert FIXTURE.read_bytes() == build()


def test_fit_parses_into_canonical_trackpoints():
    track = FitAdapter().parse(FIXTURE)

    # The pre-lock record without a position is dropped; every fix is kept.
    assert len(track.points) == EXPECTED["n_points"]
    first, last = track.points[0], track.points[-1]
    assert first.t == EXPECTED["start_t"]
    assert all(b.t - a.t == EXPECTED["step_s"] for a, b in zip(track.points, track.points[1:]))
    # Altitude is decoded as raw / 5 - 500, so allow the division's rounding, not more.
    assert math.isclose(first.ele, EXPECTED["first_ele"], abs_tol=1e-9)
    assert math.isclose(last.ele, EXPECTED["last_ele"], abs_tol=1e-9)
    assert first.hr == EXPECTED["first_hr"]
    assert last.hr == EXPECTED["last_hr"]
    # Semicircles round-trip to degrees within their own quantisation (~1e-7°).
    assert all(math.isclose(p.lat, EXPECTED["lat"], abs_tol=1e-6) for p in track.points)
    assert all(b.lon > a.lon for a, b in zip(track.points, track.points[1:]))


def _as_gpx(track, path: Path) -> Path:
    from datetime import datetime, timezone

    body = ""
    for p in track.points:
        iso = datetime.fromtimestamp(p.t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body += (f'<trkpt lat="{p.lat:.7f}" lon="{p.lon:.7f}">'
                 f"<ele>{p.ele:.2f}</ele><time>{iso}</time></trkpt>\n")
    path.write_text(
        '<?xml version="1.0"?>\n<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
        f"<trk><trkseg>\n{body}</trkseg></trk></gpx>\n"
    )
    return path


def test_fit_file_analyses_end_to_end_and_agrees_with_its_gpx_twin(tmp_path):
    result = analyze_file(FIXTURE, Config(), StubService())

    assert result.distance_m > 0
    assert len(result.segments) >= 1
    assert result.cost_heat > 0  # 20 °C is above the 10 °C reference

    twin = _as_gpx(FitAdapter().parse(FIXTURE), tmp_path / "short_run.gpx")
    via_gpx = analyze_file(twin, Config(), StubService())

    assert math.isclose(result.distance_m, via_gpx.distance_m, rel_tol=1e-4)
    assert math.isclose(result.np_pace, via_gpx.np_pace, rel_tol=1e-3)
