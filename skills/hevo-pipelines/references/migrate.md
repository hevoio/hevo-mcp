# Clone a pipeline, or move to a new one

Some things can't change on an existing pipeline: the Destination, the `destination_prefix`, the
`replication_type`, a database Source's host, port, user or database, the ingestion mode. The answer is a
new pipeline; never delete the old one as part of the move.

## Clone ("same as orders-prod, for staging")

1. Read the original: `get_pipeline` (settings, schedule, prefix, Destination) and its objects with
   `list_pipeline_objects` (`fetch_all: true`; keep `fully_qualified_name`, `selected`, `load_mode`).
2. The API never returns the Source configuration or a credential, so they can't be copied: ask for the
   new Source's connection details as for any create, with credentials as secret references (SKILL.md,
   Secrets).
3. Ask for the new name and a **new `destination_prefix`** (or a different Destination): reusing the
   prefix on the same Destination would load into the original's tables.
4. Create through the Edge create workflow in SKILL.md with the copied settings shown as the defaults the
   user confirms, then apply the copied object selection with `update_pipeline_objects` once the
   objects are discovered.

## Move (new Destination, new mode, new host)

1. Create the new pipeline as above; it is created disabled by default. Select the same objects.
2. Enable it and let the first historical load finish while the old one keeps running (both load the
   Destination and count events meanwhile: say so).
3. Help the user compare: object lists and row counts on both sides. The user runs the queries in their
   Destination; the API can't.
4. When the user is satisfied, pause the old pipeline (its own approval prompt, with the log-retention
   note from [sources.md](sources.md) for log-based Sources). Deleting it, and its Destination, is a
   separate step the user asks for later, with its own prompt ([destinations.md](destinations.md)).
