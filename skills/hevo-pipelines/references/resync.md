# Sync now, resync and recovery

Pick the smallest action that fixes the problem, and name its cost before asking for approval. Every
action here except sync now is destructive (re-ingests data), and the server asks the user to approve it.

| User wants | Action | What it costs |
| --- | --- | --- |
| Fresh data now | **Sync now** | Nothing extra: only changes since the last run |
| A few tables wrong or missing rows | **Resync those objects** | Re-reads them from the start: Destination load, and events that count toward the Events quota |
| Most tables wrong, a Source change that affects loaded data, or a long outage | **Resync the pipeline** | Re-reads every active object; the historical load runs again |

Don't promise a quota figure; the public APIs have no billing endpoint (see the usage page in the Hevo UI).

**Ask before every resync whether to drop and reload** (`drop_and_load`). `true`: Hevo drops and recreates
the Destination tables, then re-ingests; `false` (the default): tables are kept and updated to match the
Source. With load mode `APPEND`, loading the data again can create duplicate records either way: say so.

## Edge

Check the state first ([pipeline-states.md](pipeline-states.md)).

- **Sync now:** `sync_now_pipeline`; `Initialized` only. 202 starts an incremental job; the state
  stays `Initialized`. 409 means a job is already running or a change is in progress: offer to wait, or
  to cancel the running job with `cancel_pipeline_job`, taking its id from `get_pipeline` → `runs.active`
  (`list_pipeline_jobs` also lists finished jobs, and cancelling one returns 400) (cancelling a historical load or resync also
  cancels the incremental jobs running alongside it; queued jobs aren't cancelled; the schedule is kept).
  The cancel is accepted, not immediate: read the job back with `get_pipeline_job` until `status`
  is `CANCELED`. A finished job, one already being cancelled, a cancel sent during another cancel or a
  resync, or an incremental job running beside a historical load or resync (cancel that job instead)
  returns 400; if the job finishes first the cancel can return 500, so read the job again.
- **Resync objects:** `resync_pipeline_objects` with `body={"object_names": [...],
  "drop_and_load": false}`. Names are the `fully_qualified_name` values from `list_pipeline_objects`
  (`fetch_all: true`), **at most 50 per request**; split a longer list into several calls, each approved.
  An object must be selected and in status `ACTIVE`, `INCONSISTENT` or `RESETTING`; name a child's
  parent, not the child (Hevo resyncs the children with it). Allowed from any state except `Disabling`,
  `Disabled`, `Restarting` (a pipeline resync is running: 400 `ALREADY_EXISTS: Pipeline is being
  ReSynced`; wait for `Initialized`) or after a failed setup, enable or disable (400). The schedule, the
  state and every object not named are left unchanged. 404 = no such object; 409 = that object is
  already resyncing.
- **Resync the pipeline:** `resync_pipeline` with `body={"drop_and_load": ...}` (the body is
  optional; leaving it out means `false`). Allowed from `Initialized` or `Failed` (not after a failed delete), so a disabled pipeline is enabled first, and a pipeline that never ran needs enabling, not a resync. It runs the historical load again
  for every active object; any running job is cancelled, and the pipeline doesn't run to its schedule
  until the resync completes. It also releases what Hevo created in the Source (such as a PostgreSQL
  replication slot), reads the Source and Destination schemas again and resets the object mappings; an
  `ON_DEMAND` pipeline starts a sync at once. Say this when asking for approval. If it fails, the state is
  `Failed`. 202 with state `Restarting`; read back until `Initialized`. With
  `drop_and_load: true` the Destination has no data for those objects until the load completes.

## Standard

- **Run now:** `standard_run_pipeline`.
- **Restart an object** (re-ingest it from the start, from the earliest records the Source or its logs
  still hold; the re-ingested events are billable): `standard_restart_pipeline_object`; destructive.
- **Fix an object by its status:** `SKIPPED` → `standard_include_pipeline_object`; `PAUSED` →
  `standard_resume_pipeline_object`; `PERMISSION_DENIED` → the user grants access at the Source, then
  include or restart it.
- **Load to the Destination now:** `standard_load_destination_now`.

## Replication position (log-based Sources)

Reading where a pipeline or object resumes from is safe: `get_pipeline_position`,
`standard_get_pipeline_position` (log-based Standard pipelines only; 400 otherwise) and
`standard_get_object_position` (a polled object's offset: its `type`, `field_name` and `keys`). **Changing it**
(`standard_update_pipeline_position`, `standard_update_object_position`) skips or replays changes: do it
only on the user's explicit request with the exact position they give, never as a fix you propose.

Docs: [Syncing and Resyncing Pipelines](https://edge-api-docs.hevodata.com/reference/syncing-and-resyncing-pipelines),
[Viewing Pipeline Job History](https://edge-api-docs.hevodata.com/reference/viewing-pipeline-job-history).
