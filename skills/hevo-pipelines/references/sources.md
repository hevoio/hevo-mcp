# Database Sources: prerequisites and what can change

For SaaS Sources see [saas-sources.md](saas-sources.md).

## Ingestion modes

Build the list of modes from the connectors, as the "Choose the Source mode" rule in SKILL.md says: Edge
from `list_source_connectors` (one connector per mode, each with a `description` and a
`documentation_url`), Standard from `get_schema("standard_create_pipeline")`'s `source_type` values.
Describe a mode by what it captures (log-based change capture vs polling; whether deletes are captured),
using the connector's own `description`, and never pick one for the user.

## When a connection test or an access error fails

Hevo tests the Source connection on create (`create_pipeline`) and on Source changes; a failed test
returns 400 with the connector's message, and nothing is saved. Before retrying, check with the user, in
this order:

1. Host, port, database and user as the Source sees them; the user has the grants the connector's
   `documentation_url` lists.
2. Hevo's IP addresses for the region are allowed at the Source. The API doesn't return them: the user
   copies them from the Hevo UI's create screen or the docs.
3. For log-based modes, the database's own replication prerequisites (for example MySQL binary logging in
   row format, PostgreSQL logical replication with a publication and a replication slot, SQL Server
   Change Tracking or CDC on the tables). The exact settings are per Source in the Hevo docs; link the
   connector's `documentation_url` rather than reciting them from memory.
4. SSH and SSL settings: the connector schema and its `documentation_url` say which fields go together
   (a client certificate needs its key) and what format they take; a private key is a secret, so it goes
   in as a secret reference (SKILL.md, Secrets).

Objects that stay `INACCESSIBLE` after the pipeline runs point to the Source side, typically the table not
being covered by the log-based setup (publication, Change Tracking) or missing grants; the Source's docs
page has the checklist: [tables.md](tables.md).

## What can change on an existing Source

`update_pipeline_sources` accepts only the fields the connector allows editing; a non-editable
field is rejected with a 400 that names it, even with its current value, and the connector schema doesn't
mark which are editable. For MySQL the docs list the password, the SSH tunnel and the SSL settings; for
PostgreSQL also the replication slot and write-ahead-log settings; the host, port, user and database name
can't change, so a different one means a new pipeline ([migrate.md](migrate.md)). Hevo tests the Source
connection before saving a `config` or `auth` change; the pipeline's state doesn't change. Standard:
`standard_update_pipeline_source`. Wrong credentials stop the pipeline.

## Log retention

A log-based pipeline reads the changes the database still keeps. If it is paused, fails, or runs less
often than the database's log retention, the changes in between are lost and a resync is the fix
([resync.md](resync.md)). Say so when pausing a log-based pipeline for long, and point to the Source's
docs for the retention setting.

Docs: [Creating a Pipeline with Hevo API](https://edge-api-docs.hevodata.com/reference/creating-a-pipeline-with-hevo-api),
[Modifying the Source Configuration](https://edge-api-docs.hevodata.com/reference/modifying-the-source-configuration).
