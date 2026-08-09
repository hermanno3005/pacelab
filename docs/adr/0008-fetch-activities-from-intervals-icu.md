# Fetch activities from intervals.icu: a Provider that downloads original FITs to a cache

Automates the manual FIT export that ADR-0003 assumed. A **Provider** (v1: intervals.icu)
lists activities and downloads each one's **original file** by id into a local cache; the
existing file pipeline (`FitAdapter` → preprocess → analyze) then takes over unchanged.

**intervals.icu over Strava/COROS** (see `docs/research/activity-api-ingestion.md`):
- COROS's own API/MCP exposes no per-point track at all (ADR-0003), so it can't be the source.
- intervals.icu uses **API-key** auth (HTTP Basic `API_KEY:<key>`) and serves the
  **original uploaded FIT** via `GET /api/v1/activity/{id}/file` — highest fidelity, and it
  lets us reuse the validated `FitAdapter`. Strava needs OAuth2 and offers *no* original-file
  download (only processed streams); its complexity and lower fidelity ruled it out.

**Provider ≠ Source Adapter.** The Provider knows the network and credentials, never file
formats; the Source Adapter parses a file, never touches a socket. They compose through the
**FIT cache** (`{account}/{activity_id}.fit`, the intervals.icu `i…` id as canonical key):

- The cache is a **durable local archive** — you keep your original files even if the account
  goes away (NFR-7 privacy, C-3 offline-after-fetch).
- Cached originals are immutable, so re-analysis is **network-free and reproducible** (NFR-3),
  and dedup is the store's existing `is_current(activity_id, model_version)` — an
  already-current activity is skipped without even downloading (rate-limit friendly).

**Workflow:** `pacelab sync --from DATE` lists → downloads new originals → analyzes → stores,
end to end and idempotent.

## Consequences / open risk

- intervals.icu returns **no original for Strava-synced activities**. If the athlete's COROS
  data reaches intervals.icu via Strava, `/file` fails for every activity. Mitigation:
  download the original with **skip-and-log** on failure (at INFO, since a Strava-synced
  activity has no original on every pass — #29), and verify the real sync path with
  a one-request probe during implementation before writing off the **streams** fallback
  (`/api/v1/activity/{id}/streams`), which stays deferred (ADR-0003 fetch strategy).
