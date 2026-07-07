#!/usr/bin/env python3
"""
EPMO daily digest generator (two-stage, AI-enriched).

Twin of the CoE Reviewer routine, for the "PMO Projects Portfolio". Reads the
portfolio from Asana, keeps only OPEN projects (not completed, not archived)
whose OWNER is one of the EPMO team members, reads each project's status update
and tasks, and writes two Supabase rows the dashboard reads live:

  - app_state id = 6  -> live snapshot (EPMO dashboard tab)
  - app_state id = 7  -> rolling history (historical charts)

Because the per-project "how it's going" summary must be written by AI (Claude)
rather than copied from the raw status update, generation is split into stages:

    collect  -> fetch Asana, compute signals, write a raw payload to a file
    (Claude) -> read that file, write `aiSummary` + `aiDetail` per project + `aiOverview`
    publish  -> read the enriched file, update history, upsert Supabase 6 & 7

The orchestrator (the Claude Code routine session, or a human running it) does
the middle step. `all` runs collect+publish deterministically with no AI, as a
fallback — the dashboard falls back to the rule-based summary when aiSummary is
absent.

Credentials come from environment variables:
  ASANA_PAT / SUPABASE_URL / SUPABASE_KEY

Usage:
  python generate_epmo_digest.py collect --out /tmp/epmo.json
  python generate_epmo_digest.py publish --in  /tmp/epmo.json
  python generate_epmo_digest.py all                 # deterministic, no AI
"""

import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- config -----
PORTFOLIO_GID = "1210500083704814"          # PMO Projects Portfolio
LIVE_ROW_ID = 6
HISTORY_ROW_ID = 7
HISTORY_RETENTION_DAYS = 120
RECENT_MOVEMENT_DAYS = 3
STALE_STATUS_DAYS = 14
RD_TZ = timezone(timedelta(hours=-4))        # America/Santo_Domingo (UTC-4 all year)

TEAM = {
    "1209512777279096": "Jhara Ochoa",
    "1209702001489520": "Maria Garcia",
    "1214644877862651": "Laura Urena",
    "1210847964982552": "Thiago Santuzzi",
    "1210175205608295": "Nicolas Cavalcanti",
}
# Preserve this order in the dashboard's per-person sections.
TEAM_ORDER = list(TEAM.keys())

HEALTH_LABEL = {
    "on_track": "On track", "at_risk": "At risk", "off_track": "Off track",
    "on_hold": "On hold", "none": "No status", None: "No status",
}
ATTENTION_HEALTH = {"at_risk", "off_track"}

# ------------------------------------------------------------- http layer -----
_CA = "/root/.ccr/ca-bundle.crt"
_ctx = ssl.create_default_context(cafile=_CA) if os.path.exists(_CA) else ssl.create_default_context()
_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
_handlers = [urllib.request.HTTPSHandler(context=_ctx)]
if _proxy:
    _handlers.append(urllib.request.ProxyHandler({"https": _proxy, "http": _proxy}))
_OPENER = urllib.request.build_opener(*_handlers)


def _http(url, headers, data=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _OPENER.open(req, timeout=90) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else {}


def asana_get(path, params=None):
    pat = os.environ["ASANA_PAT"]
    url = "https://app.asana.com/api/1.0/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _http(url, {"Authorization": f"Bearer {pat}"})


def asana_get_all(path, params):
    params = dict(params)
    params.setdefault("limit", 100)
    out, offset = [], None
    while True:
        if offset:
            params["offset"] = offset
        page = asana_get(path, params)
        out.extend(page.get("data", []))
        nxt = page.get("next_page")
        if nxt and nxt.get("offset"):
            offset = nxt["offset"]
        else:
            return out


def supabase_get_data(row_id):
    url = os.environ["SUPABASE_URL"].rstrip("/") + f"/rest/v1/app_state?id=eq.{row_id}&select=data"
    key = os.environ["SUPABASE_KEY"]
    rows = _http(url, {"apikey": key, "Authorization": f"Bearer {key}"})
    return rows[0]["data"] if rows and rows[0].get("data") else None


def supabase_upsert(row_id, data):
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/app_state"
    key = os.environ["SUPABASE_KEY"]
    body = json.dumps({"id": row_id, "data": data}).encode("utf-8")
    _http(url, {
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }, data=body, method="POST")


# --------------------------------------------------------------- helpers ------
def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_rd_date(value):
    dt = parse_dt(value)
    return dt.astimezone(RD_TZ).date() if dt else None


def first_sentences(text, limit=280):
    if not text:
        return ""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    dot = cut.rfind(". ")
    return (cut[:dot + 1] if dot > 80 else cut).rstrip() + " …"


# ---------------------------------------------------------------- build -------
def build():
    now = datetime.now(RD_TZ)
    today = now.date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    fields = ",".join([
        "name", "resource_type", "owner.name", "owner.gid", "completed",
        "completed_at", "archived", "due_on", "modified_at", "permalink_url",
        "current_status_update.title", "current_status_update.text",
        "current_status_update.status_type", "current_status_update.created_at",
    ])
    items = asana_get_all(f"portfolios/{PORTFOLIO_GID}/items", {"opt_fields": fields})
    projects = [p for p in items if p.get("resource_type") == "project"
                and (p.get("owner") or {}).get("gid") in TEAM]
    open_projects = [p for p in projects if not p.get("completed") and not p.get("archived")]
    completed = [p for p in projects if p.get("completed") and p.get("completed_at")]

    def comp_entry(p):
        return {
            "name": p["name"], "url": p.get("permalink_url"),
            "member": TEAM[p["owner"]["gid"]], "memberGid": p["owner"]["gid"],
            "completedDate": str(to_rd_date(p["completed_at"])),
        }

    ctw, ctm, clm = [], [], []
    for p in sorted(completed, key=lambda x: x["completed_at"], reverse=True):
        d = to_rd_date(p["completed_at"])
        if not d:
            continue
        if d >= week_start:
            ctw.append(comp_entry(p))
        if d >= month_start:
            ctm.append(comp_entry(p))
        elif last_month_start <= d <= last_month_end:
            clm.append(comp_entry(p))

    project_cards, recent_movements = [], []
    recent_cutoff = now - timedelta(days=RECENT_MOVEMENT_DAYS)

    for p in open_projects:
        gid = p["gid"]
        member = TEAM[p["owner"]["gid"]]
        su = p.get("current_status_update") or {}
        health = su.get("status_type") or "none"
        su_created = su.get("created_at")
        su_days = (today - to_rd_date(su_created)).days if su_created else None
        due = p.get("due_on")
        overdue = bool(due and due < str(today))

        try:
            tasks = asana_get_all("tasks", {
                "project": gid,
                "opt_fields": "name,completed,completed_at,modified_at,due_on,assignee.name",
            })
        except Exception as exc:                       # noqa: BLE001
            tasks = []
            print(f"  ! task fetch failed for {p['name']!r}: {exc}", file=sys.stderr)

        incomplete = [t for t in tasks if not t.get("completed")]
        overdue_tasks = [t for t in incomplete if t.get("due_on") and t["due_on"] < str(today)]

        moved = []
        for t in tasks:
            mdt = parse_dt(t.get("modified_at"))
            if mdt and mdt >= recent_cutoff:
                verb = "completed" if t.get("completed") else "updated"
                moved.append((mdt, t.get("name", "(untitled)"), verb, t.get("assignee") or {}))
        moved.sort(key=lambda x: x[0], reverse=True)

        proj_movements = []
        for mdt, tname, verb, assignee in moved[:6]:
            entry = {
                "project": p["name"], "projectUrl": p.get("permalink_url"),
                "member": member, "task": tname, "change": verb,
                "actor": assignee.get("name") or member,
                "when": mdt.astimezone(RD_TZ).isoformat(),
            }
            proj_movements.append({"task": tname, "change": verb, "actor": entry["actor"], "when": entry["when"]})
            recent_movements.append(entry)

        roadblocks = []
        if health in ATTENTION_HEALTH:
            roadblocks.append(f"Owner flagged health as {HEALTH_LABEL[health]}")
        elif health == "on_hold":
            roadblocks.append("Project is on hold")
        if overdue:
            roadblocks.append(f"Project is past its due date ({due})")
        if overdue_tasks:
            sample = ", ".join(t["name"] for t in overdue_tasks[:3])
            more = f" (+{len(overdue_tasks) - 3} more)" if len(overdue_tasks) > 3 else ""
            roadblocks.append(f"{len(overdue_tasks)} overdue task(s): {sample}{more}")
        if not su:
            roadblocks.append("No status update posted yet")
        elif su_days is not None and su_days > STALE_STATUS_DAYS:
            roadblocks.append(f"Status update is stale ({su_days} days old)")

        needs_attention = bool(roadblocks) and not (
            len(roadblocks) == 1 and roadblocks[0].startswith("Project is on hold")
        )

        # Deterministic fallback summary (used only if aiSummary is not written).
        bits = [f"{HEALTH_LABEL[health]}.", f"{len(incomplete)} open task(s)"]
        done_recent = sum(1 for _, _, v, _ in moved if v == "completed")
        if done_recent:
            bits.append(f"{done_recent} completed in the last {RECENT_MOVEMENT_DAYS} days")
        if overdue_tasks:
            bits.append(f"{len(overdue_tasks)} overdue")
        fallback = bits[0] + " " + ", ".join(bits[1:]) + "."

        project_cards.append({
            "gid": gid, "name": p["name"], "url": p.get("permalink_url"),
            "member": member, "memberGid": p["owner"]["gid"],
            "health": health, "healthLabel": HEALTH_LABEL[health],
            "dueOn": due, "overdue": overdue,
            "openTasks": len(incomplete), "tasksTruncated": len(tasks) >= 100,
            "needsAttention": needs_attention, "hasStatusUpdate": bool(su),
            "statusUpdate": {
                "title": su.get("title"), "text": su.get("text"),
                "createdAt": su_created, "daysOld": su_days,
            } if su else None,
            "fallbackSummary": fallback,
            "aiSummary": None,          # <- one-line brief, written by the AI stage
            "aiDetail": None,           # <- 2-4 plain-English bullets, written by the AI stage
            "roadblocks": roadblocks,
            "recentMovements": proj_movements,
        })

    recent_movements.sort(key=lambda m: m["when"], reverse=True)

    by_health = defaultdict(int)
    for c in project_cards:
        by_health[c["health"]] += 1
    needs_attention_total = sum(1 for c in project_cards if c["needsAttention"])

    by_member = {}
    for gid in TEAM_ORDER:
        mine = [c for c in project_cards if c["memberGid"] == gid]
        by_member[gid] = {
            "name": TEAM[gid], "open": len(mine),
            "attention": sum(1 for c in mine if c["needsAttention"]),
            "completedThisWeek": sum(1 for c in ctw if c["memberGid"] == gid),
            "completedThisMonth": sum(1 for c in ctm if c["memberGid"] == gid),
        }

    summary = {
        "totalOpen": len(open_projects), "byHealth": dict(by_health),
        "needsAttention": needs_attention_total,
        "recentMovementCount": len(recent_movements),
        "completedThisWeek": len(ctw), "completedThisMonth": len(ctm),
        "completedLastMonth": len(clm), "byMember": by_member,
    }

    live = {
        "updatedAt": now.isoformat(), "generatedFor": str(today),
        "portfolioGid": PORTFOLIO_GID,
        "team": [{"gid": g, "name": TEAM[g]} for g in TEAM_ORDER],
        "summary": summary,
        "aiOverview": None,             # <- written by the AI stage
        "recentMovements": recent_movements[:40],
        "completed": {"thisWeek": ctw, "thisMonth": ctm, "lastMonth": clm},
        "projects": sorted(project_cards, key=lambda c: (TEAM_ORDER.index(c["memberGid"]), not c["needsAttention"], c["name"])),
    }
    return live


def update_history(summary, date_str, now):
    history = supabase_get_data(HISTORY_ROW_ID) or {"snapshots": [], "retentionDays": HISTORY_RETENTION_DAYS}
    snapshot = {
        "date": date_str, "updatedAt": now.isoformat(),
        "totalOpen": summary["totalOpen"], "byHealth": summary["byHealth"],
        "needsAttention": summary["needsAttention"],
        "completedThisWeek": summary["completedThisWeek"],
        "completedThisMonth": summary["completedThisMonth"],
        "byMember": {g: {"name": m["name"], "open": m["open"], "attention": m["attention"]}
                     for g, m in summary["byMember"].items()},
    }
    snaps = [s for s in history.get("snapshots", []) if s.get("date") != date_str]
    snaps.append(snapshot)
    cutoff = str((now - timedelta(days=HISTORY_RETENTION_DAYS)).date())
    snaps = sorted((s for s in snaps if s.get("date", "") >= cutoff), key=lambda s: s.get("date", ""))
    history["snapshots"] = snaps
    history["retentionDays"] = HISTORY_RETENTION_DAYS
    history["lastPruned"] = date_str
    return history


# ------------------------------------------------------------- stages ---------
def _require_env():
    for var in ("ASANA_PAT", "SUPABASE_URL", "SUPABASE_KEY"):
        if not os.environ.get(var):
            print(f"FATAL: missing environment variable {var}", file=sys.stderr)
            sys.exit(2)


def stage_collect(out_path):
    _require_env()
    print("Collecting EPMO data from Asana …")
    live = build()
    s = live["summary"]
    print(f"  open: {s['totalOpen']} | attention: {s['needsAttention']} | "
          f"movements: {s['recentMovementCount']} | "
          f"completed w/m/lm: {s['completedThisWeek']}/{s['completedThisMonth']}/{s['completedLastMonth']}")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(live, fh, ensure_ascii=False, indent=2)
    print(f"  wrote raw payload -> {out_path}")
    print("  NEXT: fill each project's aiSummary and aiDetail plus the top-level aiOverview, then run `publish`.")


def stage_publish(in_path):
    _require_env()
    with open(in_path, "r", encoding="utf-8") as fh:
        live = json.load(fh)
    missing = [p["name"] for p in live.get("projects", []) if not p.get("aiSummary")]
    if missing:
        print(f"  note: {len(missing)} project(s) have no aiSummary; dashboard will use the fallback for those.")
    missing_detail = [p["name"] for p in live.get("projects", []) if not p.get("aiDetail")]
    if missing_detail:
        print(f"  note: {len(missing_detail)} project(s) have no aiDetail; dashboard will show the raw Asana status ('From Asana') for those.")
    now = datetime.now(RD_TZ)
    live["updatedAt"] = now.isoformat()
    supabase_upsert(LIVE_ROW_ID, live)
    print(f"  wrote live snapshot -> app_state id={LIVE_ROW_ID}")
    history = update_history(live["summary"], live["generatedFor"], now)
    supabase_upsert(HISTORY_ROW_ID, history)
    print(f"  wrote history ({len(history['snapshots'])} days) -> app_state id={HISTORY_ROW_ID}")
    print(f"Done. EPMO digest for {live['generatedFor']} published.")


def stage_all():
    """Deterministic end-to-end (no AI). Dashboard uses fallback summaries."""
    _require_env()
    print("Building EPMO digest (deterministic, no AI) …")
    live = build()
    now = datetime.now(RD_TZ)
    supabase_upsert(LIVE_ROW_ID, live)
    history = update_history(live["summary"], live["generatedFor"], now)
    supabase_upsert(HISTORY_ROW_ID, history)
    print(f"Done. EPMO digest for {live['generatedFor']} published (rows 6 & 7).")


def main(argv):
    stage = argv[1] if len(argv) > 1 else "all"

    def opt(flag, default=None):
        return argv[argv.index(flag) + 1] if flag in argv else default

    if stage == "collect":
        stage_collect(opt("--out", "/tmp/epmo_digest.json"))
    elif stage == "publish":
        stage_publish(opt("--in", "/tmp/epmo_digest.json"))
    elif stage == "all":
        stage_all()
    else:
        print(f"Unknown stage {stage!r}. Use: collect | publish | all", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
