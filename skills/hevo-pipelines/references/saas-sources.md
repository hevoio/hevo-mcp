# SaaS Sources (HubSpot, Stripe, Shopify, Google Ads, ...)

Use with the Edge create workflow in SKILL.md, in place of its database parts. A SaaS Source has no
ingestion mode, host or password: the work is finding the credential type, getting it from the user
safely, and the connector's own settings.

## 1. Can it be created through the API?

Read the connector's schema with `get_source_connector` (find the `connector_id` with
`list_source_connectors` and `query={"type": "<TYPE>"}`):

- `config` has a field marked `writeOnly` or `format: password` (for example an `api_key` or
  `private_app_access_token`) → **static token**.
- The schema has a top-level `auth` property → **the user's own OAuth app** (`auth.client_access.client_id`
  and `client_secret`, plus a `refresh_token` or `access_token`).
- A method selector in `config` (`authentication_method`, `connection_mode`, `auth_method`, ...) lists
  choices → ask which first, then read its `if`/`then` rules for what that choice requires.
- None of these → the Source needs a browser sign-in: it can only be created in the Hevo UI. Once it
  exists, everything else works through the API. Never send `auth` to such a connector (400).

If Edge has no connector for the type, the Standard create takes `source_type` and an untyped
`source_config`: copy the key names from an existing Standard pipeline of the same type (report only the
key names), or point the user to the Hevo UI.

## 2. Credentials

Never ask for a token or secret in the chat. Ask the user to save each one as a secret file and tell you
its name (SKILL.md, Secrets), then put `$secret_file:<name>` in the field that holds it: every
`writeOnly`/password field under `config`, and under `auth`, `client_access.client_secret` and the
`refresh_token` or `access_token`. Send every key the schema lists under `auth.client_access`, whether or
not it is in that object's `required` list (Microsoft Ads and Google Ads `developer_token`, Amazon Selling
Partner `application_id`): the connection test fails without them. The
[Authenticating SaaS Sources](https://edge-api-docs.hevodata.com/reference/authenticating-saas-sources)
page explains each connector's app setup. For SaaS Sources `replication_type` is
`HISTORICAL_AND_INCREMENTAL` and can't change later: send it without asking. For Google Ads, Analytics 4 and Search Console, the user's OAuth consent screen
must be published to Production before they generate the refresh token: in Testing status Google limits
each refresh token to seven days and the pipeline stops then. The request asks the user for approval
because it carries a secret reference; tell them which connector it is for.

## 3. Connector settings

Ask for every `config` field in `required` and in the `if`/`then` rules for the chosen method, and offer the
optional ones that change what's loaded, with the schema's allowed values (an historical sync window, for
example; some are fixed after create, so say so when the field's description or the connector's docs say
it can't change).

Send the key a conditional rule tests (the method selector, a `sync_mode`) and leave out fields that
don't apply rather than sending them empty; sending `auth` to a connector without OAuth, or changing the
method selector without `auth`, returns 400. The API never returns the Source configuration, so a wrong
key can't be caught afterwards and an unknown key is stored silently: check every `config` key name against
the connector schema before sending, and confirm only `source.name`, `connector_id` and `type` on the
read-back. Rotating credentials later (`update_pipeline_sources`) works from `Initialized` or
`Disabled` only.
