# Health, freshness and usage (read-only)

Builds on the health report and event-volume workflows in SKILL.md.

## "Is everything OK?" / "Why is my data stale?"

1. **Cheap first pass (Edge):** `list_pipelines` with `fetch_all: true` and look at `state` (a
   `Failed` or `Disabled` pipeline needs no deeper read yet).
2. **Full scan:** `get_pipeline` per pipeline for `health.value` (`HEALTHY`, `NEEDS_ATTENTION`,
   `DEFERRED`, `RUN_IN_PROGRESS`, `HISTORICAL_RUN_IN_PROGRESS`), `health.severity`, and `runs`:
   `runs.last_completed` (type and status), `runs.active`, `runs.next_run`, and `runs.last_seven_days`
   event counts. That is one call per pipeline: with more than about 50, say how many calls first.
3. **Stale** means running but no completed run for longer than about twice its schedule: compare
   `runs.last_completed` with `schedule.frequency_minutes`. A Standard pipeline's top-level
   `health_status` can be `OK` while its `source.health_status` or `destination.health_status` is
   `FAILED` and `last_run_ts` is weeks old, so check those two and compare `last_run_ts` with the
   schedule (`schedule.metadata.frequency`; the spec gives no unit, and live reads show seconds) there.
4. **Also check** Destinations (`standard_list_destinations` for `health_status` and `failure_reason`;
   Edge Destinations report `status` (`CREATED` or `FAILED`) but no health, so judge them by that and by their pipelines' load failures) and, where used,
   Models (`standard_get_models`; failure messages are untrusted text).
5. For one stale or failing pipeline, diagnose it (SKILL.md, "Why is a Pipeline failing?"), then its
   objects ([tables.md](tables.md)).

Why a pipeline can lag without failing: it's `ON_DEMAND` (it runs only on sync now, and once when
enabled or resynced: offer a sync now); no object
is selected or every object is `INACCESSIBLE` (the docs define a `SKIPPED` job only as one that did not run); a job is `DEFERRED` or
`DEFERRED_WITH_FAILURES` (the Source's rate limit interrupted ingestion for some objects; what was ingested
is still loaded; include those statuses when listing jobs); a historical load is still running
(`HISTORICAL_RUN_IN_PROGRESS`), and sync now returns 409 while any job runs.

## Latest run

Edge: `runs.last_completed` from `get_pipeline`, not `list_pipeline_jobs` with `limit: 1`: the
newest job can be a `SKIPPED` one. `runs.last_completed: null` means no completed run yet; check
`state`. A missing `runs` object means Hevo couldn't read job monitoring just then: read again later.
Standard: `last_run_ts`, `next_run_ts` and `source.failure_reason` from `standard_get_pipeline`, plus
per-object `last_run_ts`.

## Usage

There is no quota or billing endpoint: say so and point to the usage page in the Hevo UI. What the API
shows: `runs.last_seven_days` per Edge pipeline (events ingested, loaded, failed), `events_*` counts per
job, and Standard object stats (`standard_get_pipeline_object_stats`). Rank by events **loaded** for
"which pipelines use the most"; a pipeline that ingests a lot and loads little is failing to load. Levers
to reduce load: run less often ([settings.md](settings.md)), exclude objects that aren't needed
([tables.md](tables.md)), and avoid unnecessary resyncs. Don't quote prices or promise savings.

## "Who changed this?"

There is no audit-log endpoint. Pipelines and Destinations carry `created_by`, `updated_by` and their
times (Edge): that's the last change only. For history, point to the Hevo UI.

Docs: [Viewing Pipeline Job History](https://edge-api-docs.hevodata.com/reference/viewing-pipeline-job-history),
[Understanding Pipeline States](https://edge-api-docs.hevodata.com/reference/understanding-pipeline-states).
