#!/usr/bin/env python3
"""CoE portfolio dashboard update routine. Writes Supabase app_state rows 4 (live) and 5 (history)."""
import os, sys, json, urllib.request, urllib.error
from datetime import datetime, date, timedelta

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ASANA_PAT    = os.environ["ASANA_PAT"]
PORTFOLIO_GID = "1214057614427291"
LIVE_ROW_ID, HISTORY_ROW_ID = 4, 5
TERMINAL = {"Completed", "Rejected", "Deployed", "Project Support Log"}
WRITE = "--write" in sys.argv

SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
TODAY = date.today()
TODAY_STR = TODAY.strftime("%Y-%m-%d")

def fail(msg):
    print("FATAL:", msg)
    sys.exit(1)

def http_json(req, label):
    try:
        return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
    except urllib.error.HTTPError as e:
        deny = e.headers.get("x-deny-reason")
        body = e.read()[:400]
        fail(f"{label}: HTTP {e.code} x-deny-reason={deny} body={body!r}")
    except Exception as e:
        fail(f"{label}: {type(e).__name__}: {e}")

# ---------- STEP 1: previous live snapshot (row 4) ----------
req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/app_state?id=eq.{LIVE_ROW_ID}&select=data", headers=SB_HEADERS)
previous = http_json(req, "Supabase read row4")
prev_data = previous[0]["data"] if previous and previous[0].get("data") else None
print(f"[1] prev_data: {'present' if prev_data else 'NONE (first run)'}"
      + (f" updatedAt={prev_data['updatedAt']}" if prev_data else ""))

# ---------- STEP 2: fetch Asana portfolio items ----------
fields = ("name,owner.name,permalink_url,current_status_update.title,current_status_update.text,"
          "current_status_update.status_type,current_status_update.created_at,custom_fields.name,"
          "custom_fields.display_value,custom_fields.type,custom_fields.date_value,due_on,start_on,created_at")
url = f"https://app.asana.com/api/1.0/portfolios/{PORTFOLIO_GID}/items?opt_fields={fields}&limit=100"
items, pages = [], 0
while url:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {ASANA_PAT}"})
    d = http_json(req, "Asana portfolio items")
    items += d.get("data", [])
    pages += 1
    nxt = d.get("next_page")
    url = nxt["uri"] if nxt else None
print(f"[2] fetched {len(items)} items across {pages} page(s)")

def cf_map(item):
    return {cf.get("name"): cf for cf in item.get("custom_fields", [])}

def dv(m, name):
    cf = m.get(name)
    if not cf: return None
    v = cf.get("display_value")
    return v if v not in ("", None) else None

def dval(m, name):
    """date custom field -> YYYY-MM-DD or None"""
    cf = m.get(name)
    if not cf: return None
    d = cf.get("date_value")
    if d and d.get("date"): return d["date"]
    return None

def days_between(d_str, end=TODAY):
    if not d_str: return None
    try:
        return (end - date.fromisoformat(d_str[:10])).days
    except Exception:
        return None

projects_list = []
for it in items:
    m = cf_map(it)
    sprint_raw = dv(m, "Sprint #")
    sprint = None
    if sprint_raw is not None:
        try: sprint = int(float(sprint_raw))
        except Exception: sprint = None
    status = it.get("current_status_update") or None
    stage = dv(m, "CoE Stage")
    owner = (it.get("owner") or {}).get("name")
    p = {
        "gid": it["gid"],
        "url": it.get("permalink_url"),
        "name": it.get("name"),
        "dueOn": it.get("due_on"),
        "owner": owner,
        "stage": stage,
        "sprint": sprint,
        "startOn": it.get("start_on"),
        "isActive": stage not in TERMINAL,
        "priority": dv(m, "Priority"),
        "uatStart": dval(m, "UAT Start"),
        "createdAt": it.get("created_at"),
        "startDate": dval(m, "Start Date"),
        "submitter": dv(m, "Submitter Name"),
        "baAssigned": dval(m, "BA Assigned"),
        "department": dv(m, "Project Department"),
        "pmAssigned": dv(m, "PM Assigned"),
        "statusType": (status or {}).get("status_type"),
        "firstReview": dv(m, "First Review"),
        "statusTitle": (status or {}).get("title"),
        "deployedDate": dval(m, "Deployed"),
        "receivedDate": dval(m, "Received Date"),
        "completedDate": dval(m, "Completed Date"),
        "haloSubmitted": dval(m, "Halo Submitted"),
        "projectPaused": dval(m, "Project Paused"),
        "classification": dv(m, "CoE Classification"),
        "firstReviewDate": dval(m, "First Review Date"),
        "scopingCallDate": dval(m, "Scoping Call Date"),
        "statusCreatedAt": (status or {}).get("created_at"),
        "classificationDate": dval(m, "Classification Date"),
        "itPrioritizationDate": dval(m, "IT Prioritization Date"),
    }
    projects_list.append(p)

# ----- summary counts -----
def counter():
    from collections import Counter
    return Counter()

byStage, byPriority, byDepartment, byClassification, byFirstReview = (counter() for _ in range(5))
active = [p for p in projects_list if p["isActive"]]
for p in projects_list:
    byStage[p["stage"] or "None"] += 1
    byPriority[p["priority"] or "None"] += 1
    byDepartment[p["department"] or "None"] += 1
    byClassification[p["classification"] or "None"] += 1
    fr = p["firstReview"]
    if fr:
        for tok in [t.strip() for t in fr.split(",") if t.strip()]:
            byFirstReview[tok] += 1
    else:
        byFirstReview["None"] += 1

# avg days in pipeline (received -> today, active only)
pipe = [days_between(p["receivedDate"]) for p in active]
pipe = [d for d in pipe if d is not None]
avgDaysInPipeline = round(sum(pipe)/len(pipe), 1) if pipe else 0
awaitingNextSprint = sum(1 for p in projects_list if (p["stage"] or "") == "Awaiting Next Sprint")
all_sprint_vals = [p["sprint"] for p in projects_list if p["sprint"] is not None]
current_sprint = max(all_sprint_vals) if all_sprint_vals else None

# ----- flags (active projects only) -----
flags_list = []
def add_flag(p, reason):
    flags_list.append({"gid": p["gid"], "url": p["url"], "project": p["name"], "reason": reason})

for p in active:
    st = (p["statusType"] or "").lower()
    if st == "at_risk": add_flag(p, "At risk")
    elif st == "off_track": add_flag(p, "Off track")
    elif st == "dropped": add_flag(p, "Dropped")
    # overdue / approaching due
    dd = days_between(p["dueOn"]) if p["dueOn"] else None  # >0 means past due
    if p["dueOn"]:
        overdue_days = (TODAY - date.fromisoformat(p["dueOn"][:10])).days
        if overdue_days > 0:
            add_flag(p, f"Overdue ({overdue_days} days)")
        elif 0 <= -overdue_days <= 7:
            add_flag(p, f"Due soon ({-overdue_days} days)")
    # status freshness
    if not p["statusCreatedAt"]:
        add_flag(p, "No status update")
    else:
        sdays = days_between(p["statusCreatedAt"])
        if sdays is not None and sdays > 7:
            add_flag(p, f"Stale status ({sdays} days)")
    # stuck in triage / new request
    if p["stage"] in ("Triage", "New Request"):
        sdays = days_between(p["receivedDate"]) or days_between(p["createdAt"])
        if sdays is not None and sdays > 5:
            add_flag(p, f"Stuck in {p['stage']} ({sdays} days)")
    # missing fields
    if not p["classification"]: add_flag(p, "No classification")
    if not p["priority"]:       add_flag(p, "No priority")

needsAttention = len({f["gid"] for f in flags_list})

summary_dict = {
    "byStage": dict(byStage),
    "byPriority": dict(byPriority),
    "totalActive": len(active),
    "byDepartment": dict(byDepartment),
    "byFirstReview": dict(byFirstReview),
    "currentSprint": current_sprint,
    "totalProjects": len(projects_list),
    "needsAttention": needsAttention,
    "byClassification": dict(byClassification),
    "avgDaysInPipeline": avgDaysInPipeline,
    "awaitingNextSprint": awaitingNextSprint,
}
print(f"[2] totalActive={len(active)} totalProjects={len(projects_list)} needsAttention={needsAttention} "
      f"avgDays={avgDaysInPipeline} currentSprint={current_sprint} flags={len(flags_list)}")

# ---------- STEP 2.5: cumulative sprint membership + sprintSummary ----------
membership = {}
for gid, sprints in (prev_data or {}).get("sprintMembership", {}).items():
    membership[gid] = set(sprints)
# backfill from history (row 5)
req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/app_state?id=eq.{HISTORY_ROW_ID}&select=data", headers=SB_HEADERS)
_h = http_json(req, "Supabase read row5")
hist_data = _h[0]["data"] if _h and _h[0].get("data") else {"snapshots": [], "retentionDays": 30}
for snap in hist_data.get("snapshots", []):
    for cp in snap.get("projects", []):
        if cp.get("sprint") is not None and cp.get("gid"):
            membership.setdefault(cp["gid"], set()).add(int(cp["sprint"]))
# today's live
for p in projects_list:
    if p["sprint"] is not None:
        membership.setdefault(p["gid"], set()).add(int(p["sprint"]))
sprint_membership_out = {gid: sorted(s) for gid, s in membership.items()}

proj_by_gid = {p["gid"]: p for p in projects_list}
all_sprints = sorted({n for gid in proj_by_gid for n in membership.get(gid, [])})
cur_sprint = max(all_sprints) if all_sprints else None

def is_overflow(p, snum):
    return cur_sprint is not None and snum < cur_sprint and p.get("stage") not in TERMINAL

sprint_summary = {}
for snum in sorted(all_sprints, reverse=True):
    members = [proj_by_gid[g] for g in proj_by_gid if snum in membership.get(g, [])]
    if not members: continue
    sprint_summary[str(snum)] = {
        "sprintNumber": snum,
        "total": len(members),
        "completed": sum(1 for p in members if p.get("stage") == "Completed"),
        "inProgress": sum(1 for p in members if p.get("stage") not in TERMINAL),
        "overflowed": sum(1 for p in members if is_overflow(p, snum)),
        "otherTerminal": sum(1 for p in members if p.get("stage") in TERMINAL and p.get("stage") != "Completed"),
        "projects": [
            {"name": p["name"], "url": p["url"], "stage": p.get("stage"),
             "priority": p.get("priority"), "completedDate": p.get("completedDate"),
             "isOverflow": is_overflow(p, snum)}
            for p in members
        ],
    }
print(f"[2.5] sprints={all_sprints} membership_size={len(sprint_membership_out)}")

# ---------- STEP 3: delta ----------
if prev_data:
    prev_projects = {p["gid"]: p for p in prev_data.get("projects", [])}
    new_gids, prev_gids = set(proj_by_gid), set(prev_projects)
    delta = {
        "previousDate": prev_data["updatedAt"],
        "newProjects": [{"name": proj_by_gid[g]["name"], "url": proj_by_gid[g]["url"], "stage": proj_by_gid[g]["stage"]}
                        for g in new_gids - prev_gids],
        "removedProjects": [{"name": prev_projects[g]["name"], "stage": prev_projects[g].get("stage")}
                            for g in prev_gids - new_gids],
        "stageChanges": [], "priorityChanges": [], "newFlags": [], "resolvedFlags": [],
        "summaryDelta": {},
    }
    for g in new_gids & prev_gids:
        n, o = proj_by_gid[g], prev_projects[g]
        if n.get("stage") != o.get("stage"):
            delta["stageChanges"].append({"name": n["name"], "url": n["url"], "from": o.get("stage"), "to": n.get("stage")})
        if n.get("priority") != o.get("priority"):
            delta["priorityChanges"].append({"name": n["name"], "from": o.get("priority"), "to": n.get("priority")})
    new_flagset = {(f["gid"], f["reason"]) for f in flags_list}
    prev_flagset = {(f["gid"], f["reason"]) for f in prev_data.get("flags", [])}
    name_by_gid = {**{p["gid"]: p["name"] for p in prev_data.get("projects", [])}, **{g: proj_by_gid[g]["name"] for g in proj_by_gid}}
    for (g, r) in new_flagset - prev_flagset:
        delta["newFlags"].append({"project": name_by_gid.get(g, g), "reason": r})
    for (g, r) in prev_flagset - new_flagset:
        delta["resolvedFlags"].append({"project": name_by_gid.get(g, g), "reason": r})
    ps = prev_data.get("summary", {})
    delta["summaryDelta"] = {
        "totalActive": summary_dict["totalActive"] - ps.get("totalActive", 0),
        "needsAttention": summary_dict["needsAttention"] - ps.get("needsAttention", 0),
        "avgDaysInPipeline": round(summary_dict["avgDaysInPipeline"] - ps.get("avgDaysInPipeline", 0), 1),
        "awaitingNextSprint": summary_dict["awaitingNextSprint"] - ps.get("awaitingNextSprint", 0),
    }
else:
    delta = None

if delta:
    print(f"[3] delta: new={len(delta['newProjects'])} removed={len(delta['removedProjects'])} "
          f"stageChanges={len(delta['stageChanges'])} priorityChanges={len(delta['priorityChanges'])} "
          f"newFlags={len(delta['newFlags'])} resolvedFlags={len(delta['resolvedFlags'])} summaryDelta={delta['summaryDelta']}")
    for np in delta["newProjects"]: print("      NEW:", np["name"], "->", np["stage"])
    for sc in delta["stageChanges"]: print("      STAGE:", sc["name"], sc["from"], "->", sc["to"])
else:
    print("[3] delta: None (first run)")

# ---------- STEP 4: written summary ----------
tri = byStage.get("Triage", 0); itp = byStage.get("IT Prioritization", 0)
uat = byStage.get("UAT/Testing", 0); inprog = byStage.get("In Progress", 0)
no_class = byClassification.get("None", 0); no_prio = byPriority.get("None", 0)
if delta:
    da = delta["summaryDelta"]["needsAttention"]
    trend = "flat vs last run" if da == 0 else (f"up {da} vs last run" if da > 0 else f"down {abs(da)} vs last run")
else:
    trend = "first snapshot"
dom_dept, dom_n = max(byDepartment.items(), key=lambda kv: kv[1]) if byDepartment else (None, 0)
stale_n = sum(1 for f in flags_list if f["reason"].startswith("Stale status") or f["reason"] == "No status update")

bullets = []
bullets.append(f"- {len(active)} active projects, {needsAttention} flagged for attention — {trend}")
bullets.append(f"- Triage holds {tri}, {itp} in IT Prioritization, {uat} in UAT, {inprog} in progress")
bullets.append(f"- {no_class} lack classification, {no_prio} have no priority — triage hygiene gap delays routing")
if delta:
    bullets.append(f"- Since last update: {len(delta['newProjects'])} new, {len(delta['stageChanges'])} stage changes, {len(delta['resolvedFlags'])} flags resolved")
if dom_dept and dom_n > len(projects_list) * 0.4:
    bullets.append(f"- {dom_dept} dominates with {dom_n} of {len(projects_list)}")
bullets.append(f"- Rec: refresh status on {stale_n} stale/missing-status projects to restore reporting accuracy")
summary_text = "\n".join(bullets)
print("[4] writtenSummary:\n" + summary_text)

# ---------- assemble payload ----------
updated_at = datetime.now().isoformat()
payload = {
    "updatedAt": updated_at,
    "projects": projects_list,
    "flags": flags_list,
    "summary": summary_dict,
    "delta": delta,
    "writtenSummary": summary_text,
    "sprintSummary": sprint_summary,
    "sprintMembership": sprint_membership_out,
}

def upsert(rid, data, label):
    body = json.dumps({"id": rid, "data": data}).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/app_state", data=body, method="POST",
        headers={**SB_HEADERS, "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=representation"})
    return http_json(req, label)

# ---------- STEP 5.5 prep: history snapshot ----------
history = hist_data
compact_projects = [{
    "gid": p["gid"], "name": p["name"], "stage": p.get("stage"), "priority": p.get("priority"),
    "classification": p.get("classification"), "department": p.get("department"), "owner": p.get("owner"),
    "dueOn": p.get("dueOn"), "receivedDate": p.get("receivedDate"), "sprint": p.get("sprint"),
    "isActive": p.get("isActive"), "statusType": p.get("statusType"),
    "completedDate": p.get("completedDate"), "haloSubmitted": p.get("haloSubmitted"),
} for p in projects_list]
compact_flags = [{"gid": f["gid"], "project": f["project"], "reason": f["reason"]} for f in flags_list]
snapshot = {
    "date": TODAY_STR, "updatedAt": updated_at, "summary": summary_dict,
    "projects": compact_projects, "flags": compact_flags, "writtenSummary": summary_text,
    "sprintSummary": sprint_summary, "delta": delta,
}
history["snapshots"] = [s for s in history.get("snapshots", []) if s.get("date") != TODAY_STR]
history["snapshots"].append(snapshot)
cutoff = (TODAY - timedelta(days=30)).strftime("%Y-%m-%d")
history["snapshots"] = [s for s in history["snapshots"] if s.get("date", "") >= cutoff]
history["snapshots"].sort(key=lambda s: s.get("date", ""))
history["retentionDays"] = history.get("retentionDays", 30)
history["lastPruned"] = TODAY_STR

if not WRITE:
    print("\n[DRY RUN] Not writing. Row4 payload bytes:", len(json.dumps(payload)),
          "| Row5 snapshots after update:", len(history["snapshots"]),
          "dates:", [s["date"] for s in history["snapshots"]])
    sys.exit(0)

# ---------- STEP 5 + 5.5: write ----------
r = upsert(LIVE_ROW_ID, payload, "Supabase write row4")
print("[5] row4 written, returned id:", r[0]["id"] if r else "?")
r = upsert(HISTORY_ROW_ID, history, "Supabase write row5")
print("[5.5] row5 written, snapshots:", len(history["snapshots"]))
print("DONE")
