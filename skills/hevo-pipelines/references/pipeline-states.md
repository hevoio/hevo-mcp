# Edge pipeline states

Check the state before any action on an Edge pipeline: it decides whether Hevo accepts the call. Read it
with `get_pipeline` (`state`). Tell the user what they can do in plain words, not the raw state alone.

| `state` | Meaning |
| --- | --- |
| `Created`, `Created Source`, `Created Destination`, `Configured Pipeline`, `Configured Object` | Still being set up |
| `Initialized` | Running normally on its schedule (there is no "enabled" state) |
| `Disabled` | Paused: no schedule |
| `Initializing`, `Enabling`, `Disabling`, `Restarting` | An action is in progress; wait, don't send another change |
| `Failed` | An action failed (setup, enable, disable, resync or delete). The API doesn't say which: read the recent jobs. Data may still flow: after a failed disable the pipeline keeps replicating, after a failed setup or enable it isn't replicating, and after a failed resync the objects were not re-ingested. After a failed enable or disable the pipeline can only be resynced or deleted; after a failed delete the API treats it as deleted (404 everywhere) and Hevo Support has to help |
| `Deleting` | Being deleted; reads already return 404 |

A deleted pipeline returns `UPSTREAM_NOT_FOUND` (404).

## What each state allows

| Action | Endpoint | Allowed from |
| --- | --- | --- |
| Enable (resume) | `enable_pipeline` | `Disabled`, with at least one object selected (none selected → 400). A 409 can come back for a few seconds even from `Disabled`: retry shortly. Restores the schedule; if the first historical load never finished, Hevo reads both schemas again and resets the object mappings (say so when confirming); an `ON_DEMAND` pipeline also starts a sync, except on the first enable of a pipeline created disabled |
| Disable (pause) | `disable_pipeline` | `Initialized`. Running jobs finish unless `body={"cancel_active_jobs": true}`; the pipeline stays `Disabling` until they do. If the first historical load hadn't finished, Hevo also releases what it created in the Source (a PostgreSQL replication slot) |
| Sync now | `sync_now_pipeline` | `Initialized`. Starts an incremental job; the state stays `Initialized`; follow it in `runs.active` or `list_pipeline_jobs` |
| Resync objects | `resync_pipeline_objects` | Any state except `Disabling`, `Disabled`, `Restarting` (a pipeline resync is running: 400), or after a failed setup, enable or disable; see [resync.md](resync.md) |
| Resync the pipeline | `resync_pipeline` | `Initialized` or `Failed` (not after a failed delete); see [resync.md](resync.md) |
| Change settings, Source config or objects | `update_pipeline`, `update_pipeline_sources`, `update_pipeline_objects` | `Initialized` or `Disabled`; the API returns 400 from `Failed` and 409 from an in-progress state |
| Delete | `delete_pipeline` | Any state |

## After an action

Actions return 202 with a transitional state. Read the pipeline back with `get_pipeline` every few
seconds until `state` is no longer `Initializing`, `Enabling`, `Disabling` or `Restarting`; `Failed` means
the action failed. Every settings or object change briefly moves a running pipeline through
`Initializing`, so wait before the next change. The API never returns a `*_FAILED` state value.

## 400 vs 409

- **409** (`UPSTREAM_ERROR` with status 409): the request is valid but another change is in progress, or
  the pipeline is in a transitional state. Wait, then send the same request again unchanged. If it
  persists, ask the user to close any edit left open in the Hevo UI.
- **400**: not applied; the request is wrong or the state doesn't allow it, and the message says which.
  Fix the request or tell the user what state is needed. Never retry a 400 unchanged.

Docs: [Understanding Pipeline States](https://edge-api-docs.hevodata.com/reference/understanding-pipeline-states),
[Managing Pipelines](https://edge-api-docs.hevodata.com/reference/managing-pipelines),
[Modifying the Pipeline Configuration](https://edge-api-docs.hevodata.com/reference/modifying-the-pipeline-configuration).
