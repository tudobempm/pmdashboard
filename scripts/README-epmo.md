# EPMO Daily Brief

Daily digest of the EPMO team's open projects in the **PMO Projects Portfolio**,
rendered in the dashboard's **EPMO** tab. It is the twin of the CoE Reviewer
routine: an autonomous Claude Code routine reads Asana and writes to Supabase,
and the dashboard reads Supabase live in the browser (no GitHub/commit step at
run time).

## Pieces

| Piece | Where |
|---|---|
| Generator | `scripts/generate_epmo_digest.py` |
| Dashboard tab | `EpmoDashboardTab` in `index.html` (nav id `epmo`) |
| Live data | Supabase `app_state` row **id = 6** |
| History (charts) | Supabase `app_state` row **id = 7**, rolling 120 days |
| Project notes | Supabase `app_state` row **id = 8** — written by the dashboard, keyed by project gid; `collect` reads it (never writes) to hand the AI stage each project's `userNotes` |
| Schedule | Claude Code Remote trigger `EPMO Daily Brief`, cron `5 12 * * 1-5` |

`5 12 * * 1-5` = **08:05 America/Santo_Domingo (UTC-4), Monday–Friday** = 12:05 UTC.

## Team (filtered by project **owner**)

| Member | Asana owner GID |
|---|---|
| Jhara Ochoa | 1209512777279096 |
| Maria Garcia | 1209702001489520 |
| Laura Urena | 1214644877862651 |
| Thiago Santuzzi | 1210847964982552 |
| Nicolas Cavalcanti | 1210175205608295 |

A project is included when it is **not completed**, **not archived**, and its
owner is one of the members above — even if it has no status update.

## How generation works (three stages)

The per-project "how it's going" summary and the team overview are written by
**AI (Claude)** — interpreted, not copied from the raw Asana status. So the
generator is split into stages and the routine's Claude session does the middle
one:

```
collect  ->  python3 scripts/generate_epmo_digest.py collect --out /tmp/epmo.json
             (fetches Asana + the team's dashboard notes from row 8 — read-only,
             never writes Supabase; each project gets a `userNotes` list,
             pinned note first, capped at 5)
 (AI)    ->  Claude reads /tmp/epmo.json and fills, per project: `aiScope` (ONE
             stable sentence on what the project is/delivers, from `description`),
             `aiSummary` (current state: momentum, next gate, blockers — never
             leading with overdue-task counts), `aiDetail` (2-4 bullets for the
             expanded "Where it stands" section), plus the top-level `aiOverview`;
             then saves the file back. The payload embeds these expectations as
             `_aiGuidance` (stripped at publish). `userNotes` are first-hand
             context from the team — often fresher than the Asana status
publish  ->  python3 scripts/generate_epmo_digest.py publish --in /tmp/epmo.json
             (updates history and upserts Supabase rows 6 & 7)
```

`python3 scripts/generate_epmo_digest.py all` runs collect+publish
deterministically with **no AI** (fallback); the dashboard then shows the
rule-based `fallbackSummary` for each project and the raw Asana status
("From Asana" badge) in the expanded card. The routine prompt (see the
Claude Code Remote trigger) drives collect → AI → publish and requires the AI
fields (`aiScope`, `aiSummary`, `aiDetail`, `aiOverview`) before publishing —
when `aiDetail` is missing the dashboard falls back to the raw Asana status,
and when `aiScope` is missing it shows the raw project description (or nothing).

## What the brief contains

- **AI team overview** — a few sentences interpreting the whole team's state.
- KPIs: open / needs-attention / at-risk.
- Charts: open-by-health doughnut + a history line (open, needs-attention, weekly completions).
- Recent activity feed (task movements in the last 3 days, with actor and time).
- Projects completed this week / this month / last month.
- **Projects grouped by member**, each an expandable card: a fixed scope line
  (what the project is, `aiScope`) followed by a current-state AI summary when
  collapsed, and — when expanded — roadblocks, recent movement, the full Asana
  status update, and the team's notes.

## Run it manually

Needs env vars `ASANA_PAT`, `SUPABASE_URL`, `SUPABASE_KEY`. To reproduce the
AI-enriched brief, run `collect`, fill `aiSummary`/`aiDetail`/`aiOverview` in the
JSON, then `publish`. For a quick deterministic refresh:

```bash
python3 scripts/generate_epmo_digest.py all
```

## Maintenance notes

- The routine prompt runs the **repo copy** of this script (with a curl fallback
  to `main`). Keep the script on `main` in sync with what the routine expects.
- To change the team, edit the `TEAM` map in `scripts/generate_epmo_digest.py`.
- The Supabase anon key used by the browser already lives in `index.html`
  (shared with the CoE tab); no dashboard change is needed for data refresh.
