# Query modes (Standard: how each object is polled)

Standard database pipelines have a query mode per object (in a log-based pipeline it governs the
historical load only); SaaS Sources don't, except Salesforce, which has a `use_bulk_api` switch in the
same place. Edge has no per-object query mode. Describe modes by what they capture, never by API name
alone.

Why it matters: a full-load mode re-reads the whole table on every run and uses the most events; set the
right mode soon after the pipeline is created.

| `config.mode` (`get_schema("standard_update_object_query_mode")` lists the variants) | Captures | Needs |
| --- | --- | --- |
| `full_load` | Everything, each run; uses the most events; meant for tables up to about 5 million rows, re-read at most every six hours | nothing |
| `timestamp` | Rows whose timestamp moved past the last one read | `timestamp_column_name` (and a delay interval): a column that is never NULL and set on every change |
| `incrementing` | New rows | `incrementing_column_name`: numeric, unique, never NULL; updates aren't captured. Optional `stop_on_gap` (boolean) |
| `unique_incrementing` | New rows, appended | `unique_column_name`: a numeric auto-incrementing column, unique and never NULL (NULLs defer the object); updates aren't captured |
| `timestamp+incrementing` | Inserts and updates | `incrementing_column_name` and `timestamp_column_name` |
| `change_tracking` | What SQL Server Change Tracking records | Change Tracking enabled on the table first, with retention longer than the schedule; `historical_enabled`, and then `unique_column_name` for the historical load |
| `xmin` | PostgreSQL xmin-based polling | Existing pipelines only |

Which modes a given Source supports is in the Hevo docs for that Source; the schema's `oneOf` variants
list the fields each mode takes. Docs: [Query Modes](https://docs.hevodata.com/data-ingestion/ingestion-modes-and-query-modes/query-modes-for-ingesting-data/),
[Query Modes for Pipelines (API)](https://api-docs.hevodata.com/reference/query-modes-for-pipelines).

**Changing an object's mode restarts its ingestion from the beginning**: say so when the server asks the
user to approve it.

## Steps

1. Read the current mode: `standard_get_object_query_mode` (the object name is URL-encoded by the server).
2. Recommend a mode per object from what the user tells you about it: does it get updates, deletes, an
   auto-increment id, an `updated_at` column? Usually `timestamp+incrementing` when the table has both.
   Ask for the column names; the API can't list a table's columns, so never guess one.
3. For many objects, group them by the mode and columns they share, confirm each group with the user, and
   use `batch` ([bulk.md](bulk.md)) so each group is one approval prompt.
4. `standard_update_object_query_mode` with `body={"config": {"mode": "...", ...}}`, then read it back.
