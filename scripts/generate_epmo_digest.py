#!/usr/bin/env python3
"""
EPMO daily digest generator.

Twin of the CoE Reviewer routine, for the "PMO Projects Portfolio". Reads the
portfolio from Asana, keeps only OPEN projects (not completed, not archived)
whose OWNER is one of the EPMO team members, reads each project's status update
and tasks, and writes two rows to Supabase:

  - app_state id = 6  -> live snapshot (what the EPMO dashboard tab renders)
  - app_state id = 7  -> rolling history (drives the historical charts)

The dashboard reads these rows live in the browser, exactly like the CoE tab
reads rows 4 (live) and 5 (history).

Credentials come from environment variables (same contract as CoE Reviewer):
  ASANA_PAT     - Asana Personal Access Token (read access to the portfolio)
  SUPABASE_URL  - e.g. https://niqzkombzncxxihhulqq.supabase.co
  SUPABASE_KEY  - Supabase key with write access to app_state

Outbound HTTPS honours HTTPS_PROXY / the CA bundle at /root/.ccr/ca-bundle.crt
when present, so it runs both inside a Claude Code web session and in a plain
routine environment with direct egress.
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
RECENT_MOVEMENT_DAYS = 3                     # window for "recent movements"
STALE_STATUS_DAYS = 14                       # status update older than this is stale
RD_TZ = timezone(timedelta(hours=-4))        # America/Santo_Domingo (UTC-4 all year)

TEAM = {
    "1209512777279096": "Jhara Ochoa",
    "1209702001489520": "Maria Garcia",
    "1214644877862651": "Laura Urena",
    "1210847964982552": "Thiago Santuzzi",
    "1210175205608295": "Nicolas Cavalcanti",
}

HEALTH_LABEL = {
    "on_track": "On track",
    "at_risk": "At risk",
    "off_track": "Off track",
    "on_hold": "On hold",
    "none": "No status",
    None: "No status",
}
ATTENTION_HEALTH = {"at_risk", "off_track"}   # health values that always flag attention

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
    """GET with automatic pagination over Asana's next_page cursor."""
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
        "apikey": key,
        "Authorization": f"Bearer {key}",
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
    week_start = today - timedelta(days=today.weekday())          # Monday
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

    # ---- completed-window buckets (by completion date, RD tz) ----
    def comp_entry(p):
        return {
            "name": p["name"],
            "url": p.get("permalink_url"),
            "member": TEAM[p["owner"]["gid"]],
            "memberGid": p["owner"]["gid"],
            "completedDate": str(to_rd_date(p["completed_at"])),
        }

    completed_this_week, completed_this_month, completed_last_month = [], [], []
    for p in sorted(completed, key=lambda x: x["completed_at"], reverse=True):
        d = to_rd_date(p["completed_at"])
        if not d:
            continue
        if d >= week_start:
            completed_this_week.append(comp_entry(p))
        if d >= month_start:
            completed_this_month.append(comp_entry(p))
        elif last_month_start <= d <= last_month_end:
            completed_last_month.append(comp_entry(p))

    # ---- per-project cards ----
    project_cards = []
    recent_movements = []
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

        # tasks (best-effort; skip project on task-fetch failure)
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
            proj_movements.append(f"{tname} — {verb}")
            recent_movements.append({
                "project": p["name"],
                "projectUrl": p.get("permalink_url"),
                "member": member,
                "task": tname,
                "change": verb,
                "assignee": assignee.get("name"),
                "when": mdt.astimezone(RD_TZ).isoformat(),
            })

        # roadblocks (rule-based, derived from real signals)
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

        # daily summary (deterministic narrative from real signals + status text)
        bits = [f"{HEALTH_LABEL[health]}."]
        bits.append(f"{len(incomplete)} open task(s)")
        done_recent = sum(1 for _, _, v, _ in moved if v == "completed")
        if done_recent:
            bits.append(f"{done_recent} completed in the last {RECENT_MOVEMENT_DAYS} days")
        if overdue_tasks:
            bits.append(f"{len(overdue_tasks)} overdue")
        summary_line = bits[0] + " " + ", ".join(bits[1:]) + "."
        snippet = first_sentences(su.get("text"))
        if snippet:
            summary_line += f' Status update: "{snippet}"'
        elif not su:
            summary_line += " No status update on record."

        project_cards.append({
            "gid": gid,
            "name": p["name"],
            "url": p.get("permalink_url"),
            "member": member,
            "memberGid": p["owner"]["gid"],
            "health": health,
            "healthLabel": HEALTH_LABEL[health],
            "dueOn": due,
            "overdue": overdue,
            "openTasks": len(incomplete),
            "tasksTruncated": len(tasks) >= 100,
            "needsAttention": needs_attention,
            "hasStatusUpdate": bool(su),
            "statusUpdate": {
                "title": su.get("title"),
                "text": su.get("text"),
                "createdAt": su_created,
                "daysOld": su_days,
            } if su else None,
            "dailySummary": summary_line,
            "roadblocks": roadblocks,
            "recentMovements": proj_movements,
        })

    recent_movements.sort(key=lambda m: m["when"], reverse=True)

    # ---- aggregates ----
    by_health = defaultdict(int)
    for c in project_cards:
        by_health[c["health"]] += 1
    needs_attention_total = sum(1 for c in project_cards if c["needsAttention"])

    by_member = {}
    for gid, name in TEAM.items():
        mine = [c for c in project_cards if c["memberGid"] == gid]
        by_member[gid] = {
            "name": name,
            "open": len(mine),
            "attention": sum(1 for c in mine if c["needsAttention"]),
            "completedThisWeek": sum(1 for c in completed_this_week if c["memberGid"] == gid),
            "completedThisMonth": sum(1 for c in completed_this_month if c["memberGid"] == gid),
        }

    # ---- written team-level summary ----
    attn_projects = sorted(
        [c for c in project_cards if c["needsAttention"]],
        key=lambda c: (c["health"] not in ATTENTION_HEALTH, c["name"]),
    )
    lines = [
        f"- {len(open_projects)} open projects across the EPMO team "
        f"({by_health.get('on_track', 0)} on track, {by_health.get('at_risk', 0)} at risk, "
        f"{by_health.get('off_track', 0)} off track, {by_health.get('on_hold', 0)} on hold, "
        f"{by_health.get('none', 0)} without a status update).",
        f"- {needs_attention_total} project(s) need attention today; "
        f"{len(recent_movements)} task movements in the last {RECENT_MOVEMENT_DAYS} days.",
        f"- Completed: {len(completed_this_week)} this week, {len(completed_this_month)} this month, "
        f"{len(completed_last_month)} last month.",
    ]
    if attn_projects:
        top = ", ".join(f"{c['name']} ({c['healthLabel']})" for c in attn_projects[:4])
        lines.append(f"- Watchlist: {top}.")
    busiest = max(by_member.values(), key=lambda m: m["open"], default=None)
    if busiest and busiest["open"]:
        lines.append(f"- Heaviest load: {busiest['name']} with {busiest['open']} open projects.")
    written_summary = "\n".join(lines)

    summary = {
        "totalOpen": len(open_projects),
        "byHealth": dict(by_health),
        "needsAttention": needs_attention_total,
        "recentMovementCount": len(recent_movements),
        "completedThisWeek": len(completed_this_week),
        "completedThisMonth": len(completed_this_month),
        "completedLastMonth": len(completed_last_month),
        "byMember": by_member,
    }

    live = {
        "updatedAt": now.isoformat(),
        "generatedFor": str(today),
        "portfolioGid": PORTFOLIO_GID,
        "team": [{"gid": g, "name": n} for g, n in TEAM.items()],
        "summary": summary,
        "writtenSummary": written_summary,
        "recentMovements": recent_movements[:40],
        "completed": {
            "thisWeek": completed_this_week,
            "thisMonth": completed_this_month,
            "lastMonth": completed_last_month,
        },
        "projects": sorted(project_cards, key=lambda c: (not c["needsAttention"], c["member"], c["name"])),
    }
    return live, summary, str(today), now


def update_history(summary, date_str, now):
    history = supabase_get_data(HISTORY_ROW_ID) or {"snapshots": [], "retentionDays": HISTORY_RETENTION_DAYS}
    snapshot = {
        "date": date_str,
        "updatedAt": now.isoformat(),
        "totalOpen": summary["totalOpen"],
        "byHealth": summary["byHealth"],
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


def main():
    for var in ("ASANA_PAT", "SUPABASE_URL", "SUPABASE_KEY"):
        if not os.environ.get(var):
            print(f"FATAL: missing environment variable {var}", file=sys.stderr)
            return 2

    print("Building EPMO digest from Asana …")
    live, summary, date_str, now = build()
    print(f"  open projects: {summary['totalOpen']} | needs attention: {summary['needsAttention']} | "
          f"movements: {summary['recentMovementCount']} | "
          f"completed w/m/lm: {summary['completedThisWeek']}/{summary['completedThisMonth']}/{summary['completedLastMonth']}")

    supabase_upsert(LIVE_ROW_ID, live)
    print(f"  wrote live snapshot -> app_state id={LIVE_ROW_ID}")

    history = update_history(summary, date_str, now)
    supabase_upsert(HISTORY_ROW_ID, history)
    print(f"  wrote history ({len(history['snapshots'])} days) -> app_state id={HISTORY_ROW_ID}")

    print(f"Done. EPMO digest for {date_str} published.")
    print("Dashboard: https://tudobempm.github.io/pmdashboard/ -> EPMO tab")
    return 0


if __name__ == "__main__":
    sys.exit(main())
