---
name: daily-digest
description: >
  Generate, publish, and correct the pmdashboard daily briefings (morning
  digest and EOD). Use whenever asked to create a daily brief/digest/EOD for
  the dashboard, fix or republish a digest, or work with briefing_state.json,
  digests.json, or the pending-digests publish flow in tudobempm/pmdashboard.
---

# Daily Digest Generation — pmdashboard

You generate the morning briefing (`daily-YYYY-MM-DD-morning`) and end-of-day
digest (`eod-YYYY-MM-DD`) for Jean's dashboard at `tudobempm/pmdashboard`.
This skill is the operating contract. Deviating from it has broken production
before (see "Incident history" at the end).

## Architecture — what reads and writes what

```
User acts in dashboard ──► Supabase (app_state id=1)     [always succeeds]
                      └──► briefing_state.json on GitHub [via proxy; can fail]

sync-supabase-state workflow (every 30 min, 06:00–18:30 AST, Mon–Fri)
  pulls Supabase ──► reconciles ──► commits briefing_state.json
  so briefing_state.json is ALWAYS fresh when you read it.

YOU read:  briefing_state.json (state), digests.json (history)
YOU write: ONE file — pending-digests/<digest-id>.json
           The publish-pending-digest workflow does everything else.
```

**You never need Supabase access.** Do not try to read or write it; the
workflows keep it in sync in both directions. Do not edit `digests.json` or
`briefing_state.json` directly either — always go through the pending payload.

## State contract — briefing_state.json

| Field | Meaning |
|---|---|
| `completed` | map keyed by **taskId** → truthy when done |
| `hidden` | digest ids archived by the user |
| `reminders` | ACTIVE reminders queued for the next briefing |
| `reminderHistory` | reminders already shown in a digest (consumed) |
| `edits`, `reactions` | user UI state — pass through untouched |
| `lastModified` | epoch ms of last state change |

Sanity check before generating: on a weekday, `lastModified` should be less
than ~24h old. If it looks stale, say so in the digest greeting rather than
silently trusting it.

## taskId contract (hard-enforced at publish)

Full spec: `docs/digest-taskid-contract.md`. The essentials:

1. Every action item (any item in a non-`info` section) MUST have a `taskId`:
   `task-<YYYYMMDD>-<short-kebab-slug>`, dated the day the task FIRST appeared.
2. Mint once, reuse forever. For carry-overs, copy the `taskId` verbatim from
   the previous digest. Rewording a label never changes its `taskId`.
3. **Carry-over decision:** include an item in "Carried Over" ONLY if
   `completed[taskId]` is not truthy. Never key off the daily display `id`.
4. Any `completed` entries you write in the state payload are keyed by taskId.

The publish workflow hard-fails a pending digest with a missing `taskId`.

## Reminders — lifecycle rules

- **Section content:** the digest's "Reminders" section reflects EXACTLY the
  eligible entries of `briefing_state.json.reminders`. Never invent, drop, or
  substitute reminders. Jean often writes them in Spanish — the item `label`
  is the reminder `text` verbatim (whatever the language); the `detail` is
  your English one-liner tying it to today's calendar/context.
- **Eligibility by schedule:**
  - no schedule → show in the next briefing, then consume;
  - `scheduledFor: "YYYY-MM-DD"` → show in the first briefing ON or AFTER that
    date, then consume; before that date, leave it active and untouched;
  - `expiresAt: "YYYY-MM-DD"` → persistent: show it EVERY morning briefing and
    do NOT consume it until the briefing on/after its expiry date.
- **Consumption:** in the state payload, move each shown (non-persistent)
  reminder from `reminders` to `reminderHistory`, preserving its fields and
  adding `shownInDigestId: "<digest-id>"` and `archivedAt: "<now ISO>"`.
- Give each reminder item a fresh `taskId` (`task-<today>-<slug>`) so it gets
  a checkbox, unless it clearly IS an existing tracked task — then reuse that
  task's `taskId`.
- Never resurrect anything whose id already appears in `reminderHistory`.

## Publish flow

Write `pending-digests/<digest-id>.json`:

```json
{
  "digest": { "id": "daily-2026-07-06-morning", "type": "daily",
              "date": "Morning - Monday, July 6", "isoDate": "2026-07-06",
              "greeting": "...", "sections": [ ... ] },
  "state":  { "completed": {...}, "hidden": [...], "reactions": {...},
              "edits": {...}, "reminders": [...], "reminderHistory": [...],
              "lastModified": <now epoch ms> }
}
```

- `state` must contain ALL of: `completed`, `hidden`, `reactions`, `edits`,
  `reminders`, `reminderHistory` — the workflow rejects partial payloads. It
  REPLACES briefing_state.json wholesale, so start from the current file and
  change only what this digest changes (reminder consumption, `lastModified`).
- Set `lastModified` to now (epoch ms). This is what prevents older Supabase
  state from clobbering the consumption you just recorded.
- The workflow validates taskIds, prepends the digest to `digests.json`,
  overwrites `briefing_state.json`, deletes the pending file, commits, and
  syncs the published state to Supabase.

**Duplicate ids are silently skipped.** To REPUBLISH a digest (fix a bad one),
a pending file is not enough — the existing entry must be removed from
`digests.json` in the same change. Prefer a direct commit that rewrites the
entry in place.

## After publishing

Nothing to do — visibility is automatic on two independent paths:

1. The dashboard reads `digests.json` / `briefing_state.json` from
   raw.githubusercontent.com first (the Pages-relative path is only a
   fallback), so published data is live within seconds of the commit.
2. `publish-pending-digest.yml` also dispatches `deploy-pages.yml` after each
   publish, keeping the Pages-served copies fresh as backup.

If a digest STILL doesn't show, check that the publish workflow run actually
succeeded before suspecting anything else.

## Never do

- Write to Supabase, or read it as a state source — the sync workflows own
  that boundary.
- Edit `digests.json`/`briefing_state.json` directly for a normal publish.
- Reuse a digest `id`, or push a pending file to republish an existing id.
- Recompute a carried task's `taskId`, or decide carry-over by display `id`.
- Drop `state` keys or reuse a stale `lastModified`.

## Incident history (why these rules exist)

**2026-07-06:** the morning digest was generated from a briefing_state.json
frozen 3 days earlier (the dashboard's GitHub write path had silently failed;
the fresh state was only in Supabase). Result: 4 of 6 "Carried Over" items
were already completed, and the Reminders section showed 2 deleted reminders
instead of the 4 active ones. Fixed by reconciling state, republishing the
digest, and adding the `sync-supabase-state` workflow. The freshness sanity
check, the completed-taskId filter, and the exact-reminders rule above are the
guardrails from that incident.
