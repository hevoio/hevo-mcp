# Objects (tables): statuses, per-object settings, deletes

For choosing which objects load, see the Edge create and settings notes; for resyncing them,
[resync.md](resync.md).

## Edge object status and what to do

Read one object with `get_pipeline_object` (`status`, and `replication.status` with
`replication.reason`, untrusted text); list them with `list_pipeline_objects` (`fetch_all: true`).

| `status` | Meaning | What helps |
| --- | --- | --- |
| `ACTIVE` | Replicating | Nothing |
| `INACTIVE` | Not selected for replication | Select it with `update_pipeline_objects` (loads its full history on the next run) |
| `INACCESSIBLE` | Hevo can't reach it at the Source | The docs give no fix: the user checks the object's access at the Source (grants; for log-based Sources, that the table is covered by the replication setup). What re-selecting it does isn't documented |
| `DISABLED` | Turned off, not replicated | Read `replication.reason`; the docs name no fix |
| `BLOCKED` | Held back by the pipeline's `schema_evolution` policy | The docs name no fix beyond changing that policy ([settings.md](settings.md)) |
| `DELETED` | Removed from the pipeline; listed only with `query={"status": ["DELETED"]}` | Nothing; the Destination table stays |
| `INCONSISTENT` | A schema change left the object inconsistent with its Destination table; it isn't replicated | Resync the object ([resync.md](resync.md)); ask whether to drop and reload the table (`drop_and_load`) |
| `RESETTING` | Being reset; Hevo replicates its data again from the beginning when the reset finishes (the table is dropped only if the resync was sent with `drop_and_load: true`) | Wait |

Selecting an `INACTIVE` object starts its historical load on the next run: name that reload when asking
for approval (what selecting a `DISABLED` or `INACCESSIBLE` object does isn't documented, and an object
Hevo marks as not selectable can't be selected). Most pipelines report only `ACTIVE`, `INACTIVE`,
`DELETED`, `INACCESSIBLE` and `INCONSISTENT`; filtering the list on `BLOCKED`, `DISABLED` or `RESETTING`
returns an empty list. A field (`fields[].status`) is `ACTIVE` or `INACCESSIBLE`; only when the object
has no `selectable` key can it also be `INACTIVE`, `BLOCKED`, `DELETED` (deleted at the Source, unlike an
object's `DELETED`), `DISABLED` or `INCONSISTENT`.

**Standard** objects use `status` `ACTIVE`, `PAUSED`, `SKIPPED`, `FINISHED`, `PERMISSION_DENIED`
(`standard_list_pipeline_objects`; filter with `query={"statuses": [...]}` for those statuses, or
`query={"category": "FAILED"}`, `AWAITING_MAPPING` or `HAS_FAILED_EVENTS` to find problems; `limit`
defaults to 100). Pause, skip, include and restart with the matching `standard_*_pipeline_object`
endpoints ([resync.md](resync.md) maps each status to its fix); `PERMISSION_DENIED` isn't defined in the
Hevo docs, but the name and the fix (grant access at the Source) point at Source permissions.

## Per-object settings (Edge)

All through `update_pipeline_objects`, only while the pipeline is `Initialized` or `Disabled`, and
approved by the user first. List only the objects you change; others keep their settings.

- `selection_mode`: `INCLUDE_ALL`, `EXCLUDE_ALL` or `RETAIN_EXISTING` (the default).
- `objects[].load_mode`: `MERGE`, `APPEND` or `HISTORY_TRACKING`, only values in the object's
  `allowed_load_modes` and when `allow_load_mode_selection` is true. `MERGE` needs a primary key: for an
  object whose response has no `selectable` field, set `load_mode` to `APPEND` before excluding its
  primary-key field.
- `objects[].include_fields` / `exclude_fields`: only on selected objects; names are matched exactly
  after trimming (case-sensitive); a field can't be in both lists; a field Hevo marks as a key can't be
  excluded, and with `MERGE` the primary key can't be; at least one field stays selected unless the
  pipeline is disabled. A wrong name is rejected and the message lists the names you can use.
- `child_selection_mode` governs parent and child objects: `AUTO_FOLLOW_HEAD` (children take the
  parent's selection unless named), `REQUIRE_HEAD_SELECTED` (name the child too: it can be selected only
  while the parent is selected in the same request, and clearing the parent clears it), `INDEPENDENT`.
  Resync the parent, never a child.
- Every entry needs `name` and `selected`. The request can't set `primary_keys`, and can't select an
  object (or a child the change would select) that has hashed columns: both return 400.
- A running pipeline must keep at least one object selected. A newly selected object loads its history on
  the next run; deselecting stops loading but leaves the Destination table in place.

## Deletes

How rows deleted at the Source appear in the Destination depends on the Destination and the load mode,
and so does what a drop-and-reload does with them: it is in the Hevo docs for the Destination, so point
the user there rather than guessing. An Edge pipeline delete never drops Destination tables. A Standard delete drops them by
default (Hevo's `dropResource` defaults to `true`); the server makes you set `query.dropResource`
explicitly, so ask the user: `true` drops the tables, `false` keeps them.

Docs: [Managing Objects in Pipelines](https://edge-api-docs.hevodata.com/reference/managing-objects-in-pipelines).
