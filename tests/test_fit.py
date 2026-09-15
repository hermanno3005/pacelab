"""The FIT Source Adapter against a checked-in fixture (#60).

FIT is the adapter every ``.fit`` original goes through (ADR-0003), so it is exercised on
the same path GPX takes: parse to Trackpoints, segment, analyse. The fixture is written by
``tests/fixtures/make_fit_fixture.py``, which is also where the ground truth lives — the
assertions here compare the decoded track against the values that generator encoded, never
against the adapter's own output.
"""

import math
from datetime import datetime, timezone
from pathlib import Path

from fixtures.make_fit_fixture import FIXTURE, STEP_S, SourcePoint, build, points
from pacelab.app import analyze_file
from pacelab.config import Config
from pacelab.ingest.fit import FitAdapter
from pacelab.weather.conditions import Conditions

#: Semicircles are a 32-bit encoding of the full circle, so degrees round-trip to ~1e-7.
DEGREE_TOLERANCE = 1e-6


class StubService:
    """A fixed-weather source so the end-to-end path needs no network."""

    def conditions_at(self, lat, lon, t):
        return Conditions(20.0, 50.0, 0.0, 0.0, 0.0, 1013.0)


def test_checked_in_fixture_is_what_the_generator_writes():
    assert FIXTURE.read_bytes() == build()


def test_fit_parses_into_canonical_trackpoints():
    track = FitAdapter().parse(FIXTURE)
    expected = points()

    # The pre-lock record without a position is dropped; every located trackpoint is kept.
    assert len(track.points) == len(expected)
    for got, want in zip(track.points, expected):
        assert got.t == want.t
        assert math.isclose(got.lat, want.lat, abs_tol=DEGREE_TOLERANCE)
        assert math.isclose(got.lon, want.lon, abs_tol=DEGREE_TOLERANCE)
        # Altitude is decoded as raw / 5 - 500, so allow that division's rounding, not more.
        assert math.isclose(got.ele, want.ele, abs_tol=1e-9)
        assert got.hr == want.hr

    assert all(b.t - a.t == STEP_S for a, b in zip(track.points, track.points[1:]))


def test_export_without_the_enhanced_altitude_field_still_yields_the_same_elevations(tmp_path):
    """An older export carrying only ``altitude`` must decode to the same track.

    Note this does not reach the adapter's ``altitude`` fallback line: ``altitude`` is a
    component of ``enhanced_altitude`` in the FIT Profile, so ``fitdecode`` expands the plain
    field and the enhanced lookup finds a value either way. What it does pin is that the
    elevations survive the older wire layout.
    """
    legacy = tmp_path / "legacy.fit"
    legacy.write_bytes(build(enhanced=False))

    track = FitAdapter().parse(legacy)

    assert [p.ele for p in track.points] == [p.ele for p in FitAdapter().parse(FIXTURE).points]


def _write_gpx(track: list[SourcePoint], path: Path) -> Path:
    body = ""
    for p in track:
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

    # The twin is written from the same source trackpoints the fixture was encoded from,
    # so this compares two independent routes into the pipeline, not one route with itself.
    twin = _write_gpx(points(), tmp_path / "short_run.gpx")
    via_gpx = analyze_file(twin, Config(), StubService())

    assert math.isclose(result.distance_m, via_gpx.distance_m, rel_tol=1e-4)
    assert math.isclose(result.np_pace, via_gpx.np_pace, rel_tol=1e-3)
