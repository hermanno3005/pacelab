# A heartbeat row plus real logging, and a health predicate built on last success

`pacelab watch` is deliberately built never to die: every tick is failure-contained, so the
loop survives anything transient (ADR-0013). Once it runs unattended on HermiPi, that
resilience becomes the observability problem — a loop that fails forever looks exactly like
a loop that has nothing to do. This ADR fixes what the loop records, what it logs, and what
declares it unhealthy.

**The defect this starts from.** `watch.py` reports a contained tick failure with
`warnings.warn`. Python's default filter prints **once per unique (message, location)** and
then suppresses repeats — so a *persistently* failing tick (an expired API key) warns once
and goes silent while the loop spins on. That is precisely backwards for a daemon, where the
repetition is the signal.

**Sizing.** The product is partly its own health check: a missing annotation on a fresh run
is a real signal a human notices. What that misses is the unattended recompute/republish
path and slow degradation. The design below is sized to that gap, not to monitoring in
general.

## The heartbeat is one overwritten row in `pacelab.db`

A single-row `health` table, upserted each tick:

    last_tick_at          every tick, pass or fail
    last_success_at       last tick that raised nothing
    consecutive_failures  exceptions only; reset on any clean return
    last_error            exception type + message
    last_error_at
    last_recompute_at     last tick where the ADR-0016 pass had work
    last_tick_summary     "5 listed, 1 ok, 4 skip"
    interval_s            so readers derive the threshold instead of being told it

Rejected: an append-only tick log. It answers "when did this break" — but so do the logs,
and an unbounded table on the Pi's aged SD card (already carrying Home Assistant's own
SQLite recorder) buys a second retention problem to solve. Current state and history are
different surfaces; each gets the storage that suits it.

**The row is unkeyed** — `PRIMARY KEY CHECK (id = 1)` — not scoped by `account_id` like
`activities` and `segments`. The heartbeat describes the watch *process*, not an account. The
consequence is the point: reading it needs no account resolution, so `pacelab health` reports
broken credentials without itself requiring credentials to be present and valid.

**Derivable state is not stored.** How many activities are unpublished or on a stale
`model_version` is a live query against `activities`; duplicating it into the heartbeat would
create a second copy that can disagree with the first.

## `tick()` writes it, through an injected writer

`ResultStore` grows `record_tick()`, and `tick()` takes it as a second injected callable
beside `sync_fn`. `watch.py` stays free of any storage import and remains unit-testable
against a fake.

This is forced, not stylistic: `_SyncContext.run` lets exceptions propagate and `tick()`'s
`except` is the only code that sees a failed pass. Recording from inside the sync context
could capture successes only — exactly the half that doesn't matter.

## Success means the tick raised nothing

`consecutive_failures` counts escaping exceptions and nothing else. A tick that completes
while every publish fails is a *success* that reports its degradation through
`last_tick_summary` and the live unpublished-backlog query.

Rejected: counting `publish-failed` as failure. A single permanently unpublishable activity
— deleted upstream, say — would pin the counter high forever, with no way for a human to
acknowledge it and no remaining signal for real breakage. One counter, one crisp meaning.

Accepted consequence: a `RateLimited` 429 escapes the pass and counts as a failure. It
self-clears on the next tick, and sustained rate limiting at 96 calls/day against a 5 000/day
budget *is* something worth surfacing.

## `logging` replaces `warnings.warn`, for operational events only

`basicConfig` at INFO, format `%(asctime)s %(levelname)-5s %(message)s`, timestamps in UTC
(pinned there by `time.gmtime`, because the archive work is UTC), handler on stdout,
`--log-level` defaulting to `$PACELAB_LOG_LEVEL`.

> Corrected at bring-up (#16): this originally read "the container carries no tz data",
> which is false — `python:3.13-slim` ships tzdata, and compose now sets
> `TZ=Europe/Berlin` so the rolling window's `date.today()` is the athlete's calendar day.
> The log is UTC because `time.gmtime` pins it, deliberately, so timestamps stay
> comparable either side of a DST change. Nothing else in this ADR depends on the claim.

> Completed at #29: the swap originally landed in `watch.py` only, leaving the two
> `warnings.warn` calls the watch loop reaches through — a contained publish failure and a
> missing original file. Both are now module loggers, at the level their recurrence
> deserves: `log.warning` for the publish failure, `log.info` for the missing original,
> which is an expected skip on every listing pass rather than a fault.

The Dockerfile sets `PYTHONUNBUFFERED=1` —
without it Python block-buffers a non-tty stdout and `docker logs` lags by kilobytes, which
defeats the purpose.

**Report commands keep `print()`.** `calibrate`, `trend` and `analyze` emit data a human
asked for on stdout; prefixing a coefficient table with `INFO` makes it worse to read, and
carving out a bare formatter for it is `print()` with extra steps. Under `watch`,
per-activity output collapses to a single log line rather than `_emit`'s full summary block,
so the daemon's log stays scannable.

**A failure streak logs a traceback once, then one-liners carrying the count.** The first
occurrence is the one worth reading after the fact; the repeats need to prove only that it is
still happening. `consecutive_failures` is already in the heartbeat, so the count is free.
This is the deliberate inverse of the `warnings.warn` behaviour it replaces.

## Unhealthy: no successful tick within 3 × `interval_s`

One clause, and it covers all three failure modes — a dead loop, a wedged loop, and a
live-but-failing loop all stop producing successes. Expressed as a multiple of the recorded
interval so it tracks whatever cadence `--interval` was given. A `NULL last_success_at` reads
as infinitely stale: a loop that has never succeeded is unhealthy, correctly.

Rejected: thresholding `consecutive_failures`. It only advances while ticks are still
happening, so a dead or wedged loop freezes it at its last value and reads healthy forever.
The counter is still recorded — it is what a future push notification would fire on — but it
is not the predicate.

## `pacelab health`, and the probe stays in compose

`pacelab health` prints a human summary and exits 0 or 1. The compose `HEALTHCHECK` invokes
that same command, so there is exactly one implementation of the predicate, and its interval
is tunable without republishing the image. `start_period` covers the seconds between
container start and the first tick landing.

The image still ships **no** `HEALTHCHECK`, per ADR-0015. That ADR's objection was to a
*liveness* probe, green in exactly the failure mode that matters; a last-success predicate is
a real signal. Its other observation still stands and is accepted here: `restart:
unless-stopped` does not restart unhealthy containers, so today this colours `docker ps` and
gives the future notifier something to hang off. Nothing auto-acts on unhealthy yet.

`watch --ticks 1` writes a heartbeat — it is the cron-compatible form of the loop. Plain
`pacelab sync` does not; it is not the loop, and a manual sync should not refresh the
evidence that the daemon is alive.
