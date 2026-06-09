# Digest Task Identity Contract (`taskId`)

## Why this exists

Carry-over completion used to break across days. The dashboard keyed completion
off each item's per-day display `id` (e.g. `tue-0609-co-10`), but the digest
generator mints a **new** `id` every day for the same recurring task. So
checking off `mon-0608-co-10` never matched the next day's `tue-0609-co-10`, and
the task reappeared forever — even after being completed many times.

The fix: a **stable `taskId`** that identifies the *real task* across days,
independent of the disposable daily `id`.

## Schema

Every **action item** (any item in a non-`info` section) MUST include a
`taskId` in addition to its display `id`:

```json
{
  "id": "tue-0609-co-10",
  "taskId": "task-20260528-staff-aug-prd",
  "label": "Review Staff Aug PRD and queue impacts",
  "detail": "...",
  "flag": "carried",
  "urgent": true
}
```

- **`id`** — per-day, disposable. Keep generating it exactly as today.
- **`taskId`** — permanent. Format `task-<YYYYMMDD>-<short-kebab-slug>`, where
  `<YYYYMMDD>` is the date the task **first appeared** and the slug is a short,
  human-readable description. Example: `task-20260528-staff-aug-prd`.

Items in `info` sections (schedule, highlights, FYI) do not need a `taskId`.

## Rules for the digest generation agent

> 1. **Display `id` stays per-day and disposable** (e.g. `tue-0609-co-10`).
>    Keep generating it as you do today.
> 2. **`taskId` is permanent.** Format `task-<YYYYMMDD>-<short-kebab-slug>`,
>    with `<YYYYMMDD>` = the date the task FIRST appeared.
> 3. **Mint once, reuse forever.** When an item is a carry-over, find the SAME
>    task in the previous digest and copy its `taskId` *verbatim* into today's
>    item. Never recompute it. A reworded label MUST NOT change the `taskId`.
> 4. **Decide carry-over by `taskId`, not the display `id`.** Read
>    `briefing_state.json`. Carry an item over only if `completed[taskId]` is
>    NOT `true`. Do NOT check the daily `id` — it changes every day and will
>    always look incomplete.
> 5. **Key completion by `taskId`.** If you write a `state` payload, any
>    `completed` entries you set MUST be keyed by `taskId`.
> 6. **Every action item needs a valid `taskId`.** The publish workflow
>    (`publish-pending-digest.yml`) rejects any pending digest whose action
>    items are missing a `taskId`.

## How the dashboard consumes it

`index.html` keys all user state (`completed`, `edits`, `reactions`) by
`taskId`, falling back to the display `id`:

- **Read:** `isDone(item, completed)` returns true if
  `completed[item.taskId] || completed[item.id]`. The fallback keeps legacy
  entries (keyed by old daily ids, pre-`taskId`) and any item still missing a
  `taskId` working unchanged.
- **Write:** toggling writes under `keyOf(item)` = `item.taskId || item.id`.
  Un-checking clears both keys so the OR-based read never sticks "done".

This means: no data migration is required. Old `completed` keys (e.g.
`tue-0602-co-1`) stay valid and age out naturally; new completions go under
`taskId`; cross-day continuity starts the moment items carry a `taskId`.

## Enforcement

`.github/workflows/publish-pending-digest.yml` validates, on every pending
digest, that each action item has a non-empty `taskId`. It hard-fails the
publish if one is missing, and warns (non-fatal) on a malformed
(`task-YYYYMMDD-…`) or duplicated `taskId`.

> **Rollout note:** because this is a hard fail, the generation agent must adopt
> the contract above **before** its next run, or the next digest will be
> rejected. To run a grace period instead, change the `raise SystemExit(...)`
> for the missing-`taskId` case to a `print('WARNING: ...')` until the agent is
> updated.

## Related: state writes must reach GitHub

`briefing_state.json` is the source of truth the agent reads. The dashboard
writes it via `githubWriteState()` using a token in `localStorage`
(`llf-github-token`). If that token is missing/expired, completions only live
in the browser and **never reach GitHub** — so even a perfect `taskId` won't
help. Verify a valid token with `contents:write` on `tudobempm/pmdashboard` is
set, and watch the console for `[github] state synced…` vs
`[github] state sync FAILED:` after checking an item.
