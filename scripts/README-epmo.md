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

## What the brief contains

- Team-level status overview (health mix, attention count, completions, watchlist).
- KPIs: open / needs-attention / completed this week / this month / last month.
- Charts: open-by-health doughnut + a history line (open, needs-attention, weekly completions).
- Workload by member, most-recent task movements (last 3 days).
- Per-project card: daily summary, main roadblocks, recent movement, and the full status update text.

## Run it manually

Needs env vars `ASANA_PAT`, `SUPABASE_URL`, `SUPABASE_KEY`:

```bash
python3 scripts/generate_epmo_digest.py
```

## Maintenance notes

- The routine's prompt embeds a copy of this script so it runs even on a fresh
  checkout. **If you change the logic here, update the trigger prompt too**
  (or point the prompt at this file once it is on `main`).
- To change the team, edit the `TEAM` map in `scripts/generate_epmo_digest.py`
  (and the trigger prompt copy).
- The Supabase anon key used by the browser already lives in `index.html`
  (shared with the CoE tab); no dashboard change is needed for data refresh.
