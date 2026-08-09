import logging

from pacelab.analyze import ActivityResult
from pacelab.config import Config
from pacelab.providers.intervals import ActivityRef
from pacelab.publish.annotation import MARKER
from pacelab.publish.publisher import publish_activity, publish_range
from pacelab.store import ResultStore

#: What the running engine stamps. Derived from the coefficients (ADR-0021), so a test that
#: hands `publish_range` a `Config` has to stamp its rows from the same place.
VERSION = Config().model_version


def result(np=277.0):
    return ActivityResult(observed_pace=304.0, np_pace=np, cost_grade=3.0, cost_heat=23.0,
                          cost_wind=-2.0, distance_m=10000.0, segments=[])


class StubDescriptions:
    """Provider stub for the description read/write surface."""

    def __init__(self, descriptions=None, fail=False):
        self.descriptions = descriptions or {}
        self.fail = fail

    def list_activities(self, oldest, newest):
        return self.refs

    def fetch_description(self, activity_id):
        if self.fail:
            raise RuntimeError("intervals.icu down")
        return self.descriptions.get(activity_id)

    def update_description(self, activity_id, text):
        if self.fail:
            raise RuntimeError("intervals.icu down")
        self.descriptions[activity_id] = text


def test_publish_activity_splices_and_marks(tmp_path):
    store = ResultStore(tmp_path / "db")
    store.save("i1", result(), model_version="0.2.0", account_id="acct")
    provider = StubDescriptions({"i1": "My race notes."})

    publish_activity(provider, store, "i1", "0.2.0", "acct")

    text = provider.descriptions["i1"]
    assert text.startswith("My race notes.")
    assert MARKER in text
    assert not store.needs_publish("i1", "0.2.0", account_id="acct")


def test_publish_range_publishes_only_what_needs_it(tmp_path):
    store = ResultStore(tmp_path / "db")
    store.save("i1", result(), model_version=VERSION, account_id="acct")  # needs publish
    store.save("i2", result(), model_version=VERSION, account_id="acct")
    store.mark_published("i2", VERSION, account_id="acct")  # already done
    provider = StubDescriptions()
    provider.refs = [ActivityRef("i1", None, "Run", None), ActivityRef("i2", None, "Run", None),
                     ActivityRef("i9", None, "Run", None)]  # i9 was never analysed

    outcomes = dict(publish_range(provider, store, Config(),
                                  "2026-01-01", "2026-12-31", "acct"))

    assert outcomes == {"i1": "published", "i2": "skip", "i9": "not-analyzed"}
    assert "i1" in provider.descriptions and "i2" not in provider.descriptions


def test_publish_failure_is_contained(tmp_path, caplog):
    # ADR-0011: publishing is best-effort — a target outage must not raise out.
    store = ResultStore(tmp_path / "db")
    store.save("i1", result(), model_version=VERSION, account_id="acct")
    provider = StubDescriptions(fail=True)
    provider.refs = [ActivityRef("i1", None, "Run", None)]

    with caplog.at_level(logging.WARNING, logger="pacelab.publish.publisher"):
        outcomes = dict(publish_range(provider, store, Config(),
                                      "2026-01-01", "2026-12-31", "acct"))

    assert outcomes == {"i1": "publish-failed"}
    assert store.needs_publish("i1", VERSION, account_id="acct")  # retried next time
    assert any("publish failed for i1" in r.getMessage() for r in caplog.records)


def test_the_same_activity_failing_every_pass_logs_every_pass(tmp_path, caplog):
    # ADR-0017: `warnings.warn` printed a given (message, location) once and then went
    # silent — which hid exactly this case, an activity stuck unpublishable for days.
    store = ResultStore(tmp_path / "db")
    store.save("i1", result(), model_version=VERSION, account_id="acct")
    provider = StubDescriptions(fail=True)
    provider.refs = [ActivityRef("i1", None, "Run", None)]

    with caplog.at_level(logging.WARNING, logger="pacelab.publish.publisher"):
        for _ in range(2):
            publish_range(provider, store, Config(), "2026-01-01", "2026-12-31", "acct")

    logged = [r for r in caplog.records if r.name == "pacelab.publish.publisher"]
    assert [r.getMessage() for r in logged] == [
        "publish failed for i1: intervals.icu down"] * 2
