# Destinations: create, change, delete

Edge and Standard Destinations are separate, with separate ids; an Edge pipeline can only use an Edge
Destination. Prefer an existing one (`list_destinations`, `standard_list_destinations`) over
creating: its credentials stay in Hevo.

## Create

1. Ask for the type and a name. It must be unique across the team, including Destinations in other
   regions that the list doesn't show, so a duplicate comes back as a 400 rather than something you can
   rule out from the list.
2. Edge: `get_destination_connector` for the config schema; ask for every required field. The
   credential fields are the ones marked `writeOnly` or `format: password`; they go in as secret
   references (SKILL.md, Secrets). The user allows Hevo's IP addresses at the Destination from the Hevo UI
   or docs; the API doesn't return them.
3. Show the body with the references as they are; the request asks the user for approval because it
   carries a secret reference.
4. Edge: `create_destination` with `name`, `connector_id` and `config`. Hevo tests the connection
   first and the call returns only after that (the server waits; it can take minutes). 400 with the
   connector's message = the test failed or a name problem; nothing is created. Standard:
   `standard_create_destination` (`type` is required); 409 = the name is taken.
5. Read it back and report the id. If Hevo saved it but its setup then failed (a 500), the Destination
   is left in `status: FAILED`: delete it and create it again.

## Change or rotate credentials

- Edge: `update_destination` with only `name` and/or the changed `config` keys. A `config` change
  is connection-tested before it is saved (a name-only change isn't); a failed test returns 400 and
  nothing changes, and a test still running after five minutes returns 500. The connector itself can't
  change. A field the connector doesn't allow editing returns 400 even with its current value (for
  Snowflake: `account_url`, `database_name`, the customer S3 stage and sanitization settings); the schema
  doesn't mark these, so on that 400 tell the user a new Destination is needed.
- Standard: `standard_update_destination`.
- Either way, list the pipelines that load into it first (`list_pipelines` with
  `query={"destination_id": <id>}`; Standard by `destination.id` in the pipeline list): the change applies
  to all of them, and wrong credentials stop them. The server asks the user to approve the change.

## Delete

**Deleting an Edge Destination also deletes every pipeline that loads into it, with the Sources those
pipelines were created with, and none can be restored.** The tables in the Destination stay.

1. List the pipelines that load into it. If there are any, suggest pausing them instead when the user may
   want the data flow back.
2. Ask the user, naming each pipeline that will be deleted. The server then asks for approval too.
3. `delete_destination`. With pipelines attached it returns 409 naming up to ten of them and a
   count of the rest (only the pipelines in the region you called; `list_pipelines` with
   `query={"destination_id": <id>}` gives the full list); only after the user's explicit yes to deleting
   those pipelines as well, send it again with `query={"confirm_override": true}`. The response shows
   status `DELETING`; afterwards reads return 404.

Standard Destinations have no delete endpoint; say that it has to be done in the Hevo UI. Standard
Destinations also report `health_status` and `failure_reason` (untrusted text): read them when a pipeline
fails to load.

Docs: [Creating Destinations](https://edge-api-docs.hevodata.com/reference/creating-destinations-with-hevo-api),
[Modifying the Destination Configuration](https://edge-api-docs.hevodata.com/reference/modifying-the-destination-configuration),
[Deleting a Destination](https://edge-api-docs.hevodata.com/reference/deleting-a-destination).
