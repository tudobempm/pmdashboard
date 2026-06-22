#!/usr/bin/env python3
"""CoE portfolio dashboard update routine. Writes Supabase app_state rows 4 (live) and 5 (history)."""
import os, json, urllib.request, urllib.error, urllib.parse, sys
from datetime import datetime, timedelta, date

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ASANA_PAT    = os.environ["ASANA_PAT"]
PORTFOLIO_GID = "1214057614427291"
LIVE_ROW_ID = 4
HISTORY_ROW_ID = 5
TERMINAL = {"Completed", "Rejected", "Deployed", "Project Support Log"}
TODAY = date.today()
TODAY_STR = TODAY.strftime("%Y-%m-%d")

SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

def http_get(url, headers, label):
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        deny = e.headers.get("x-deny-reason")
        print(f"FATAL [{label}] HTTP {e.code}: {body} deny={deny}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as e:
        print(f"FATAL [{label}] {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(2)

def http_post(url, headers, body, label):
    try:
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        bd = e.read().decode(errors="replace")[:500]
        deny = e.headers.get("x-deny-reason")
        print(f"FATAL [{label}] HTTP {e.code}: {bd} deny={deny}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as e:
        print(f"FATAL [{label}] {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(2)

def parse_date(s):
    if not s: return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except Exception:
            return None

# ---------------- STEP 1: previous live snapshot ----------------
prev = http_get(f"{SUPABASE_URL}/rest/v1/app_state?id=eq.{LIVE_ROW_ID}&select=data", SB_HEADERS, "supabase read row4")
prev_data = prev[0]["data"] if prev and prev[0].get("data") else None
print(f"[step1] prev_data present={prev_data is not None}")

# ---------------- STEP 2: fetch Asana portfolio items ----------------
opt = ("name,owner.name,permalink_url,current_status_update.title,current_status_update.text,"
       "current_status_update.status_type,current_status_update.created_at,custom_fields.name,"
       "custom_fields.display_value,custom_fields.type,due_on,start_on,created_at")
items = []
url = f"https://app.asana.com/api/1.0/portfolios/{PORTFOLIO_GID}/items?opt_fields={urllib.parse.quote(opt)}&limit=100"
A_HEADERS = {"Authorization": f"Bearer {ASANA_PAT}"}
while url:
    resp = http_get(url, A_HEADERS, "asana portfolio items")
    items.extend(resp.get("data", []))
    nxt = resp.get("next_page")
    url = nxt.get("uri") if nxt else None
print(f"[step2] fetched {len(items)} portfolio items")

def cf_map(item):
    m = {}
    for cf in item.get("custom_fields", []) or []:
        nm = cf.get("name")
        if nm:
            m[nm] = cf.get("display_value")
    return m

projects_list = []
for it in items:
    m = cf_map(it)
    stage = m.get("CoE Stage")
    csu = it.get("current_status_update") or {}
    received = m.get("Received Date")
    sprint_raw = m.get("Sprint #")
    sprint = None
    if sprint_raw not in (None, ""):
        try:
            sprint = int(str(sprint_raw).strip().lstrip("#").strip())
        except Exception:
            try: sprint = int(float(sprint_raw))
            except Exception: sprint = None
    p = {
        "gid": it.get("gid"),
        "name": it.get("name"),
        "url": it.get("permalink_url"),
        "owner": (it.get("owner") or {}).get("name") if it.get("owner") else None,
        "stage": stage,
        "priority": m.get("Priority"),
        "classification": m.get("CoE Classification"),
        "department": m.get("Project Department"),
        "firstReview": m.get("First Review"),
        "pmAssigned": m.get("PM Assigned"),
        "submitterName": m.get("Submitter Name"),
        "sprint": sprint,
        "haloSubmitted": m.get("Halo Submitted"),
        "dueOn": it.get("due_on"),
        "startOn": it.get("start_on"),
        "createdOn": m.get("Created on") or it.get("created_at"),
        "receivedDate": received,
        "classificationDate": m.get("Classification Date"),
        "itPrioritizationDate": m.get("IT Prioritization Date"),
        "baAssigned": m.get("BA Assigned"),
        "firstReviewDate": m.get("First Review Date"),
        "scopingCallDate": m.get("Scoping Call Date"),
        "startDate": m.get("Start Date"),
        "projectPaused": m.get("Project Paused"),
        "uatStart": m.get("UAT Start"),
        "deployed": m.get("Deployed"),
        "completedDate": m.get("Completed Date"),
        "statusType": csu.get("status_type"),
        "statusTitle": csu.get("title"),
        "statusCreatedAt": csu.get("created_at"),
        "isActive": stage not in TERMINAL,
    }
    projects_list.append(p)

# ---------------- compute summary ----------------
active = [p for p in projects_list if p["isActive"]]
totalActive = len(active)

pipe_days = []
for p in active:
    rd = parse_date(p.get("receivedDate"))
    if rd:
        pipe_days.append((TODAY - rd).days)
avgDaysInPipeline = round(sum(pipe_days) / len(pipe_days)) if pipe_days else 0

def count_by(key, src=projects_list, active_only=False):
    d = {}
    for p in src:
        if active_only and not p["isActive"]:
            continue
        v = p.get(key) or "Unassigned"
        d[v] = d.get(v, 0) + 1
    return d

byStage = count_by("stage")
byPriority = count_by("priority", active_only=True)
byDepartment = count_by("department", active_only=True)
byClassification = count_by("classification", active_only=True)
byFirstReview = count_by("firstReview", active_only=True)
awaitingNextSprint = sum(1 for p in projects_list if (p.get("stage") or "").strip().lower() == "awaiting next sprint")

# ---------------- flags ----------------
flags_list = []
def add_flag(p, reason):
    flags_list.append({"gid": p["gid"], "project": p["name"], "url": p.get("url"), "reason": reason})

for p in projects_list:
    if not p["isActive"]:
        continue
    st = (p.get("statusType") or "")
    stage = (p.get("stage") or "")
    if st in ("at_risk", "off_track"):
        add_flag(p, "At Risk/Off Track status")
    if st == "dropped" or stage.strip().lower() == "dropped":
        add_flag(p, "Dropped status")
    due = parse_date(p.get("dueOn"))
    if due and due < TODAY:
        add_flag(p, "Overdue")
    sc = parse_date(p.get("statusCreatedAt"))
    if not p.get("statusType") and not p.get("statusCreatedAt"):
        add_flag(p, "No Status Update at all")
    elif sc and (TODAY - sc).days > 7:
        add_flag(p, "Stale Status (>7 days)")
    sl = stage.strip().lower()
    if sl in ("triage", "new request"):
        rd = parse_date(p.get("receivedDate")) or parse_date(p.get("createdOn"))
        if rd and (TODAY - rd).days > 5:
            add_flag(p, "Stuck in Triage/New Request >5 days")
    if not p.get("classification"):
        add_flag(p, "No Classification")
    if not p.get("priority"):
        add_flag(p, "No Priority")
    if due and TODAY <= due <= TODAY + timedelta(days=7):
        add_flag(p, "Approaching Due (within 7 days)")

needsAttention = len({f["gid"] for f in flags_list})

summary_dict = {
    "totalActive": totalActive,
    "needsAttention": needsAttention,
    "avgDaysInPipeline": avgDaysInPipeline,
    "awaitingNextSprint": awaitingNextSprint,
    "byStage": byStage,
    "byPriority": byPriority,
    "byDepartment": byDepartment,
    "byClassification": byClassification,
    "byFirstReview": byFirstReview,
    "totalProjects": len(projects_list),
    "totalFlags": len(flags_list),
}

# ---------------- STEP 2.5: sprint membership + summary ----------------
membership = {}
for gid, sprints in (prev_data or {}).get("sprintMembership", {}).items():
    membership[gid] = set(sprints)

hist = http_get(f"{SUPABASE_URL}/rest/v1/app_state?id=eq.{HISTORY_ROW_ID}&select=data", SB_HEADERS, "supabase read row5")
hist_data = hist[0]["data"] if hist and hist[0].get("data") else {"snapshots": [], "retentionDays": 30}
for snap in hist_data.get("snapshots", []):
    for cp in snap.get("projects", []):
        if cp.get("sprint") is not None and cp.get("gid"):
            membership.setdefault(cp["gid"], set()).add(int(cp["sprint"]))

for p in projects_list:
    if p.get("sprint") is not None:
        membership.setdefault(p["gid"], set()).add(int(p["sprint"]))

sprint_membership_out = {gid: sorted(s) for gid, s in membership.items()}

proj_by_gid = {p["gid"]: p for p in projects_list}
all_sprints = sorted({n for gid in proj_by_gid for n in membership.get(gid, [])})
current_sprint = max(all_sprints) if all_sprints else None

def _is_overflow(p, snum):
    return current_sprint is not None and snum < current_sprint and p.get("stage") not in TERMINAL

sprint_summary = {}
for snum in sorted(all_sprints, reverse=True):
    members = [proj_by_gid[g] for g in proj_by_gid if snum in membership.get(g, [])]
    if not members:
        continue
    sprint_summary[str(snum)] = {
        "sprintNumber": snum,
        "total": len(members),
        "completed": sum(1 for p in members if p.get("stage") == "Completed"),
        "inProgress": sum(1 for p in members if p.get("stage") not in TERMINAL),
        "overflowed": sum(1 for p in members if _is_overflow(p, snum)),
        "otherTerminal": sum(1 for p in members if p.get("stage") in TERMINAL and p.get("stage") != "Completed"),
        "projects": [
            {"name": p["name"], "url": p["url"], "stage": p.get("stage"),
             "priority": p.get("priority"), "completedDate": p.get("completedDate"),
             "isOverflow": _is_overflow(p, snum)}
            for p in members
        ]
    }
print(f"[step2.5] sprints={all_sprints} current={current_sprint} membership_count={len(sprint_membership_out)}")

# ---------------- STEP 3: delta ----------------
delta = None
if prev_data:
    prev_projects = {p["gid"]: p for p in prev_data.get("projects", [])}
    new_projects = proj_by_gid
    newProjects, removedProjects, stageChanges, priorityChanges = [], [], [], []
    for gid, p in new_projects.items():
        if gid not in prev_projects:
            newProjects.append({"name": p["name"], "url": p["url"], "stage": p.get("stage")})
        else:
            op = prev_projects[gid]
            if op.get("stage") != p.get("stage"):
                stageChanges.append({"name": p["name"], "url": p["url"], "from": op.get("stage"), "to": p.get("stage")})
            if op.get("priority") != p.get("priority"):
                priorityChanges.append({"name": p["name"], "from": op.get("priority"), "to": p.get("priority")})
    for gid, op in prev_projects.items():
        if gid not in new_projects:
            removedProjects.append({"name": op.get("name"), "stage": op.get("stage")})
    prev_flagset = {(f.get("gid"), f.get("reason")) for f in prev_data.get("flags", [])}
    new_flagset = {(f.get("gid"), f.get("reason")) for f in flags_list}
    fname = {f["gid"]: f["project"] for f in flags_list}
    pfname = {f.get("gid"): f.get("project") for f in prev_data.get("flags", [])}
    newFlags = [{"project": fname.get(g), "reason": r} for (g, r) in (new_flagset - prev_flagset)]
    resolvedFlags = [{"project": pfname.get(g), "reason": r} for (g, r) in (prev_flagset - new_flagset)]
    ps = prev_data.get("summary", {})
    delta = {
        "previousDate": prev_data.get("updatedAt"),
        "newProjects": newProjects,
        "removedProjects": removedProjects,
        "stageChanges": stageChanges,
        "priorityChanges": priorityChanges,
        "newFlags": newFlags,
        "resolvedFlags": resolvedFlags,
        "summaryDelta": {
            "totalActive": totalActive - ps.get("totalActive", 0),
            "needsAttention": needsAttention - ps.get("needsAttention", 0),
            "avgDaysInPipeline": avgDaysInPipeline - ps.get("avgDaysInPipeline", 0),
            "awaitingNextSprint": awaitingNextSprint - ps.get("awaitingNextSprint", 0),
        }
    }
    print(f"[step3] delta new={len(newProjects)} removed={len(removedProjects)} stageChg={len(stageChanges)} prioChg={len(priorityChanges)} newFlags={len(newFlags)} resolved={len(resolvedFlags)}")
else:
    print("[step3] first run, delta=None")

# ---------------- STEP 4: written summary ----------------
no_class = sum(1 for p in active if not p.get("classification"))
no_prio = sum(1 for p in active if not p.get("priority"))
def stage_n(name):
    return byStage.get(name, 0)
triage_n = sum(v for k, v in byStage.items() if k and k.strip().lower() in ("triage", "new request"))
itprio_n = sum(v for k, v in byStage.items() if k and "prioritization" in k.lower())
uat_n = sum(v for k, v in byStage.items() if k and "uat" in k.lower())
trend = "stable"
if delta:
    d = delta["summaryDelta"]["totalActive"]
    trend = f"up {d}" if d > 0 else (f"down {abs(d)}" if d < 0 else "flat vs last run")
bullets = []
bullets.append(f"- {totalActive} active projects, {needsAttention} flagged for attention — {trend}")
bullets.append(f"- Triage holds {triage_n}, {itprio_n} in IT Prioritization, {uat_n} in UAT, {totalActive} in progress")
bullets.append(f"- {no_class} lack classification, {no_prio} have no priority — routing/visibility gaps")
if delta:
    bullets.append(f"- Since last update: {len(delta['newProjects'])} new, {len(delta['stageChanges'])} stage changes, {len(delta['resolvedFlags'])} flags resolved")
if byDepartment:
    top_dept, top_n = max(byDepartment.items(), key=lambda kv: kv[1])
    if totalActive and top_n / totalActive >= 0.4 and top_dept != "Unassigned":
        bullets.append(f"- {top_dept} dominates with {top_n} of {totalActive}")
# Recommendation
if needsAttention and flags_list:
    rc = {}
    for f in flags_list:
        rc[f["reason"]] = rc.get(f["reason"], 0) + 1
    top_reason, tn = max(rc.items(), key=lambda kv: kv[1])
    rec = f"address '{top_reason}' affecting {tn} project(s)"
else:
    rec = "portfolio healthy — maintain status update cadence"
bullets.append(f"- Rec: {rec}")
summary_text = "\n".join(bullets)
print("[step4] writtenSummary:\n" + summary_text)

# ---------------- STEP 5: write live row 4 ----------------
payload = {
    "updatedAt": datetime.now().isoformat(),
    "projects": projects_list,
    "flags": flags_list,
    "summary": summary_dict,
    "delta": delta,
    "writtenSummary": summary_text,
    "sprintSummary": sprint_summary,
    "sprintMembership": sprint_membership_out,
}
body = json.dumps({"id": LIVE_ROW_ID, "data": payload}).encode()
hdr = {**SB_HEADERS, "Content-Type": "application/json",
       "Prefer": "resolution=merge-duplicates,return=representation"}
st, _ = http_post(f"{SUPABASE_URL}/rest/v1/app_state", hdr, body, "supabase write row4")
print(f"[step5] row4 write status={st}")

# ---------------- STEP 5.5: archive history row 5 ----------------
compact_projects = [
    {"gid": p["gid"], "name": p["name"], "stage": p.get("stage"), "priority": p.get("priority"),
     "classification": p.get("classification"), "department": p.get("department"), "owner": p.get("owner"),
     "dueOn": p.get("dueOn"), "receivedDate": p.get("receivedDate"), "sprint": p.get("sprint"),
     "isActive": p.get("isActive"), "statusType": p.get("statusType"),
     "completedDate": p.get("completedDate"), "haloSubmitted": p.get("haloSubmitted")}
    for p in projects_list
]
compact_flags = [{"gid": f["gid"], "project": f["project"], "reason": f["reason"]} for f in flags_list]
snapshot = {
    "date": TODAY_STR, "updatedAt": payload["updatedAt"], "summary": summary_dict,
    "projects": compact_projects, "flags": compact_flags, "writtenSummary": summary_text,
    "sprintSummary": sprint_summary, "delta": delta,
}
history = hist_data
history.setdefault("snapshots", [])
history.setdefault("retentionDays", 30)
history["snapshots"] = [s for s in history["snapshots"] if s.get("date") != TODAY_STR]
history["snapshots"].append(snapshot)
cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
history["snapshots"] = [s for s in history["snapshots"] if s.get("date", "") >= cutoff]
history["snapshots"].sort(key=lambda s: s.get("date", ""))
history["lastPruned"] = TODAY_STR
body = json.dumps({"id": HISTORY_ROW_ID, "data": history}).encode()
st2, _ = http_post(f"{SUPABASE_URL}/rest/v1/app_state", hdr, body, "supabase write row5")
print(f"[step5.5] row5 write status={st2} snapshots={len(history['snapshots'])}")

# ---------------- STEP 6: machine-readable summary ----------------
print("RESULT " + json.dumps({
    "totalActive": totalActive, "needsAttention": needsAttention,
    "avgDaysInPipeline": avgDaysInPipeline, "awaitingNextSprint": awaitingNextSprint,
    "totalProjects": len(projects_list), "totalFlags": len(flags_list),
    "row4_status": st, "row5_status": st2,
    "delta_new": len(delta["newProjects"]) if delta else None,
    "delta_stageChanges": len(delta["stageChanges"]) if delta else None,
    "delta_resolvedFlags": len(delta["resolvedFlags"]) if delta else None,
    "delta_newFlags": len(delta["newFlags"]) if delta else None,
    "byStage": byStage, "rec": rec,
}))
