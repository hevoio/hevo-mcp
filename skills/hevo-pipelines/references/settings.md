# Change pipeline settings

Read the current settings first, show old → new for each change, and let the server's approval prompt
show the exact request. Send only what changes.

## Edge: `update_pipeline`

Allowed only in `Initialized` or `Disabled` ([pipeline-states.md](pipeline-states.md)). Send at least one
field; Hevo keeps every field you don't send. A running pipeline passes through `Initializing` while the
change applies (even a rename): read it back until `Initialized` before the next change. A schedule change
takes effect from the next run.

| Field | Values | Notes |
| --- | --- | --- |
| `name` | text | Unique in the workspace |
| `schedule` | `{"sync_type": "SCHEDULED", "frequency_minutes": n}` or `{"sync_type": "ON_DEMAND"}` | `n` is 15, 20, 30 or 45, or whole hours from 60 to 1440; 5 and 10 only for a team with smaller frequencies enabled. Send `sync_type` whenever you send `schedule`. `ON_DEMAND` runs on sync now, and once when the pipeline is enabled or resynced |
| `load_mode` | `MERGE`, `APPEND` | Only what both the Source connector and the Destination support; applies to every object without its own mode (`HISTORY_TRACKING` is per object, [tables.md](tables.md)) |
| `schema_evolution` | `ALLOW_ALL`, `ALLOW_COLUMN_LEVEL`, `BLOCK_ALL` | `BLOCK_ALL` replicates no schema change; an object the policy holds back shows `status: BLOCKED` |
| `latency_alert` | `{"enabled": true, "threshold_minutes": n}` | Send the whole object: one without `enabled` turns the alert off. `n` from 5 to 21600 and at least twice `frequency_minutes`, checked against the settings as they will be after the change: when slowing the schedule, send a matching `latency_alert` in the same request (propose twice the new schedule) |
| `failure_handling_policy.object_failure_level` | `PARTIAL`, `ALL_OR_NONE` | Partial: other objects keep loading when one fails |

A change sent before the pipeline is back in `Initialized` returns 409; from `Failed` it returns 400.

**Can't be changed:** `destination_prefix` and `failure_handling_policy.object_failure_threshold` (set at
creation; a different value returns 400); `replication_type` and `destination_id` (not in the update
body: the server refuses a body that sends them, INVALID_BODY). Say so plainly; the only way is a new pipeline
([migrate.md](migrate.md)).

Source settings: `update_pipeline_sources` with `name`, `config` and/or `auth`; only the fields the
connector allows editing (a non-editable field is rejected even with its current value); `null` clears a
field to the connector's default. Hevo tests the Source connection before saving a `config` or `auth`
change (a name-only change isn't tested); a failed test returns 400 and nothing is saved. The pipeline's
state doesn't change. Credentials go in as secret references (SKILL.md, Secrets).

Docs: [Modifying the Pipeline Configuration](https://edge-api-docs.hevodata.com/reference/modifying-the-pipeline-configuration),
[Modifying the Source Configuration](https://edge-api-docs.hevodata.com/reference/modifying-the-source-configuration).

## Standard

- **Schedule:** check `schedule.schedule_configurable` in `standard_get_pipeline` first, then `standard_get_pipeline_schedule`;
  it returns a list of schedules, each with `type` (`REAL_TIME`, `FREQUENCY`, `CRON`, `LOAD`) and, for a
  frequency schedule, `metadata.min_allowed_frequency`, `max_allowed_frequency` and
  `suggested_frequencies`, all in seconds (3600 is "every 1 hour"; the spec gives no unit, live reads show
seconds). Offer those in plain units ("every 3
  hours"), then `standard_update_pipeline_schedule` with `body={"frequency": <seconds>}`. 400 means the
  schedule can't be changed for this pipeline; 404 here means the frequency is outside the allowed range,
  not a missing pipeline.
- **Priority:** `standard_update_pipeline_priority` with `body={"priority": "HIGH"}` or `"NORMAL"`.
- **Source config:** `standard_update_pipeline_source` with `name` and/or `config` (keys as in the
  existing pipeline's `source.config`; the API reference doesn't type them). A 400 "Pipeline with given
  name already exists" is a `name` collision, not a credential error.
- **Status:** `standard_update_pipeline_status` with `{"state": "PAUSED"}` or `{"state": "ACTIVE"}`.
- **Auto-mapping:** `standard_update_auto_mapping` with `body={"state": "ENABLED"}` or `{"state": "DISABLED"}`,
  one switch for the whole pipeline (`auto_mapping` on the pipeline shows the current value). Transformation code and schema mappings have their own
  endpoints (SKILL.md consequences). Anything else (JSON parsing strategy, table prefix, the
  Destination) has no Standard endpoint: say it can't be changed through the API.
