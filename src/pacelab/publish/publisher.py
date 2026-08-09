"""Publish orchestration (ADR-0011): write annotations to the publish target, best-effort.

The single target is the intervals.icu activity description; intervals.icu's
push-to-Strava bridge carries it onward. Publishing is idempotent (splice replaces our own
block) and tracked per (activity, model_version) — a recompute resets the mark, a failure
leaves it unset so the next run retries.
"""

import logging

from pacelab.config import Config
from pacelab.publish.annotation import render_annotation, splice_annotation
from pacelab.store import ResultStore

log = logging.getLogger(__name__)


def publish_activity(provider, store: ResultStore, activity_id: str, model_version: str,
                     account_id: str) -> None:
    """Render, splice into the current description, write back, and mark published."""
    result = store.load(activity_id, account_id=account_id)
    block = render_annotation(result, provisional=store.is_provisional(activity_id, account_id=account_id))
    current = provider.fetch_description(activity_id)
    provider.update_description(activity_id, splice_annotation(current, block))
    store.mark_published(activity_id, model_version, account_id=account_id)


def try_publish(provider, store: ResultStore, activity_id: str, model_version: str,
                account_id: str) -> bool:
    """Best-effort publish: never raises (a target outage must not fail a sync).

    Logs rather than warns: under the watch loop the same activity can fail every tick for
    days, and ``warnings.warn`` prints a given (message, location) once and then goes silent
    (ADR-0017). This is a contained failure, so it stays clear of ``consecutive_failures``.
    """
    try:
        publish_activity(provider, store, activity_id, model_version, account_id)
        return True
    except Exception as e:  # noqa: BLE001 — containment is the contract here
        log.warning("publish failed for %s: %s", activity_id, e)
        return False


def publish_range(provider, store: ResultStore, config: Config, oldest: str, newest: str,
                  account_id: str) -> list[tuple[str, str]]:
    """Backfill/re-publish annotations for analysed activities in a date range.

    Outcomes per activity: ``"published"`` | ``"skip"`` (already current) |
    ``"not-analyzed"`` (nothing stored) | ``"publish-failed"``.
    """
    outcomes: list[tuple[str, str]] = []
    for ref in provider.list_activities(oldest, newest):
        if store.load(ref.id, account_id=account_id) is None:
            outcomes.append((ref.id, "not-analyzed"))
            continue
        if not store.needs_publish(ref.id, config.model_version, account_id=account_id):
            outcomes.append((ref.id, "skip"))
            continue
        ok = try_publish(provider, store, ref.id, config.model_version, account_id)
        outcomes.append((ref.id, "published" if ok else "publish-failed"))
    return outcomes
