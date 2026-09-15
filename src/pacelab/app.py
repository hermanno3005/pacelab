"""Orchestration: a file on disk → an analysed ActivityResult.

Picks a source adapter by extension (FIT primary per ADR-0003, GPX fallback), preprocesses
into segments, enriches with weather, and analyses. The weather source is injected so the
pure path can be tested without a network.
"""

from pathlib import Path
from typing import Protocol

from pacelab.analyze import ActivityResult, analyze
from pacelab.config import Config
from pacelab.ingest.base import SourceAdapter
from pacelab.ingest.fit import FitAdapter
from pacelab.ingest.gpx import GpxAdapter
from pacelab.preprocess.pipeline import to_segments
from pacelab.weather.conditions import Conditions
from pacelab.weather.enrich import enrich


# The source adapter for each file suffix — FIT is primary (ADR-0003), GPX the fallback.
_ADAPTERS: dict[str, type[SourceAdapter]] = {".fit": FitAdapter, ".gpx": GpxAdapter}

# The formats a source adapter exists for — every other original is cached but not read.
PARSEABLE_SUFFIXES = frozenset(_ADAPTERS)


class UnsupportedSourceError(ValueError):
    """The file's suffix has no source adapter, so it cannot become a Track."""


class ConditionsSource(Protocol):
    def conditions_at(self, lat: float, lon: float, t: float) -> Conditions:
        ...


def adapter_for(path: Path) -> SourceAdapter:
    """Pick the source adapter by suffix (case-insensitive); refuse what none can read."""
    suffix = path.suffix.lower()
    try:
        return _ADAPTERS[suffix]()
    except KeyError:
        raise UnsupportedSourceError(
            f"no source adapter for '{suffix or path.name}': "
            f"expected one of {sorted(PARSEABLE_SUFFIXES)}"
        ) from None


def analyze_file(path: Path, config: Config, source: ConditionsSource) -> ActivityResult:
    track = adapter_for(path).parse(path)
    segments = to_segments(track, step_m=config.step_m)
    enriched = enrich(segments, source)
    return analyze(enriched, config)
