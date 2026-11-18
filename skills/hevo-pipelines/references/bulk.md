# Changing many pipelines or objects at once

For any request that changes more than one pipeline, object set or Destination ("pause everything
tonight", "all Postgres pipelines hourly", "exclude tmp_* everywhere", "delete the unused ones").

1. **Build the plan from reads, not memory:** every target's product, region, id, name, and its *current*
   value of what will change (state, schedule, selection), plus the new value. Skip targets already in
   the requested state and say how many you skipped. Check each target's state allows the action
   ([pipeline-states.md](pipeline-states.md)); list the ones that don't separately.
2. **Show the plan** to the user: the count, the full list (a table; past about 50 rows, the count and a
   sample plus the file or message with the rest), and the consequences that apply to any of them
   (re-ingestion, cascades, log retention). Wait for their yes. One yes covers exactly the listed targets;
   anything added later needs a new one.
3. **One call per action, with `batch`:** the same endpoint on many targets is one tool call with
   `batch` (a list of `path_params`, one per target; body and query shared; at most 100 per call, fewer on a
   shared HTTP server: INVALID_ARGUMENTS gives its `max`), and the
   server shows the user one approval prompt listing every target. Different actions, bodies or products
   are separate calls, each with its own prompt. Never spread one bulk action over single calls to avoid a
   prompt.
4. **Read the result per target:** `succeeded`, `failed` and `skipped` with an entry each. A 401, 403 or
   429 stops the rest (listed as skipped); 404 and 409 don't. Retry 409s after the in-progress change
   settles; never retry a 400 unchanged.
5. **Report** done / skipped / failed counts and every failure with its reason, then read back a sample.

**Undo and "resume later"** touch only the targets this plan changed: "resume everything at 6 am" means
the pipelines you paused, never the ones already paused before. Keep the plan in the conversation until
the user is done. You can't schedule actions for later; say so and offer to run the second half when they
come back.
