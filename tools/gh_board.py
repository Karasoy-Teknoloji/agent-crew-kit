#!/usr/bin/env python3
"""Cheap GitHub Projects v2 helper: cached ids, batched board updates, rate display.

  python gh_board.py ids [--refresh]            # cache project/field/option ids in ~/.claude/gh-board-ids.json
  python gh_board.py set --project N --status "AI queue" --priority low URL [URL...] [--dry-run]
  python gh_board.py set --file items.json      # [{"url":..,"project":9,"status":..,"priority":..}, ...]
  python gh_board.py status-count --project N     # item count per Status value (read-only)
  python gh_board.py items --status "AI queue" [--project N ...]
                                                # read-only JSON list of the issues in a Status (every cached board that has
                                                # it, or the given ones): repo, number, title, url, state, labels, assignees,
                                                # Priority name and rank (0 = most urgent on that board)
  python gh_board.py add-status --project N "AI queue" [--color GRAY] [--description ""] [--before NAME] [--dry-run]
                                                # add a Status option (at the end, or before option NAME), keeping all existing options AND their ids
  python gh_board.py sync-management [--dry-run]
                                                # management view: every org issue labelled `management-tracking` (open + closed in
                                                # the last 30 days) is mirrored into the org project "Management Tracking" with
                                                # "Progress %" computed from the issue itself and its sub-issues (compute_management); creates the label
                                                # in every repo, the project and its fields when missing; removes items whose
                                                # label is gone or that closed over 30 days ago. Idempotent, GraphQL-batched
  python gh_board.py rate                       # GraphQL / REST remaining (REST call, free)

`set` resolves issue node ids via REST, then sends up to 20 items per single GraphQL request (aliases).
Needs the gh CLI logged in with the `project` scope, and the config key GH_ORG (env, ~/.config/claude-shared/org or
<clone>/team-config/org; tools/settings.py) = the GitHub org that owns the boards. Stdlib only.
"""
import argparse
import json
import re
import subprocess
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # python -I drops the script dir
from hidden_loader import hidden  # noqa: E402  (one hidden-launch helper)
import settings  # noqa: E402  (the one config-key reader)
import time
from pathlib import Path

OWNER = settings.value("org", "GH_ORG")   # config key GH_ORG / org (README, Configuration)
CACHE = Path.home() / ".claude" / "gh-board-ids.json"
BATCH = 20
MIN_GQL = 200
BOARD_Q = """query($p:ID!){node(id:$p){...on ProjectV2{id title fields(first:50){nodes{
  ...on ProjectV2SingleSelectField{id name options{id name}}}}}}}"""


def gh(*args, inp=None):
    r = hidden.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", input=inp)
    if r.returncode:
        sys.exit(r.stderr.strip() or r.stdout.strip())
    return r.stdout


def gql(query, variables=None):
    out = json.loads(gh("api", "graphql", "--input", "-", inp=json.dumps({"query": query, "variables": variables or {}})))
    if out.get("errors"):
        sys.exit(json.dumps(out["errors"], ensure_ascii=False))
    return out["data"]


def fetch_ids():
    projects = json.loads(gh("project", "list", "--owner", OWNER, "--format", "json", "--limit", "100"))["projects"]
    boards = {}
    for p in projects:
        node = gql(BOARD_Q, {"p": p["id"]})["node"]
        fields = {}
        for f in node["fields"]["nodes"]:
            if f and f.get("name") in ("Status", "Priority"):
                fields[f["name"]] = {"id": f["id"], "options": {o["name"].lower(): o["id"] for o in f["options"]}}
        boards[str(p["number"])] = {"id": node["id"], "title": node["title"], "fields": fields}
    data = {"owner": OWNER, "fetched": int(time.time()), "boards": boards}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def load_ids(refresh=False):
    if refresh or not CACHE.exists():
        return fetch_ids()
    return json.loads(CACHE.read_text(encoding="utf-8"))


def lookup(ids, project, field, option):
    b = ids["boards"].get(str(project))
    if not b:
        return None
    if field is None:
        return b["id"]
    f = b["fields"].get(field)
    if not f:
        return None
    if not option:
        return None
    norm = lambda t: re.sub(r"^[^\w]+", "", t.lower()).strip()  # ignore leading emoji
    hit = next((v for k, v in f["options"].items() if norm(k) == norm(option)), None)
    return f["id"], hit


def rates():
    r = json.loads(gh("api", "rate_limit"))["resources"]
    return r["graphql"], r["core"]


def fmt(x):
    return f"{x['remaining']}/{x['limit']} reset {time.strftime('%H:%M:%S', time.localtime(x['reset']))}"


def cmd_rate(_):
    g, c = rates()
    print(f"GraphQL {fmt(g)} | REST {fmt(c)}")


def cmd_ids(a):
    d = load_ids(a.refresh)
    for n, b in sorted(d["boards"].items(), key=lambda kv: int(kv[0])):
        st = ", ".join(b["fields"].get("Status", {}).get("options", {}))
        print(f"{n:>3} {b['title']}  Status: {st}")
    print(f"cache: {CACHE}")


def node_id(url):
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)", url)
    if not m:
        sys.exit(f"bad issue url: {url}")
    o, r, n = m.groups()
    return json.loads(gh("api", f"repos/{o}/{r}/issues/{n}"))["node_id"]


def resolve(ids, it):
    """Return (project_id, [(field_id, option_id, label)]) refreshing the cache once on a miss."""
    nonlocal_ids = ids
    for attempt in (0, 1):
        pid = lookup(nonlocal_ids, it["project"], None, None)
        sets, ok = [], pid is not None
        for field, key in (("Status", "status"), ("Priority", "priority")):
            if it.get(key):
                fo = lookup(nonlocal_ids, it["project"], field, it[key])
                if not fo or not fo[1]:
                    ok = False
                else:
                    sets.append((fo[0], fo[1]))
        if ok:
            return nonlocal_ids, pid, sets
        if attempt == 0:
            nonlocal_ids = load_ids(True)
    sys.exit(f"unknown project/status/priority for {it['url']}: {it['project']} {it.get('status')} {it.get('priority')}")


def cmd_set(a):
    if a.file:
        items = json.loads(Path(a.file).read_text(encoding="utf-8"))
    else:
        if not a.project or not (a.status or a.priority) or not a.urls:
            sys.exit("need --project, --status and/or --priority, and urls (or --file)")
        items = [{"url": u, "project": a.project, "status": a.status, "priority": a.priority} for u in a.urls]
    g, _ = rates()
    if g["remaining"] < MIN_GQL and not a.dry_run:
        sys.exit(f"GraphQL remaining {g['remaining']} < {MIN_GQL} (reset {time.strftime('%H:%M:%S', time.localtime(g['reset']))}); stopping, use --dry-run or wait")
    ids = load_ids()
    prepared = []
    for it in items:
        ids, pid, sets = resolve(ids, it)
        prepared.append((it, pid, sets, node_id(it["url"])))
    for i in range(0, len(prepared), BATCH):
        chunk = prepared[i:i + BATCH]
        parts, vars_, decl = [], {}, []
        for k, (it, pid, sets, nid) in enumerate(chunk):
            decl.append(f"$c{k}:ID!")
            vars_[f"c{k}"] = nid
            parts.append(f'a{k}:addProjectV2ItemById(input:{{projectId:"{pid}",contentId:$c{k}}}){{item{{id}}}}')
        if a.dry_run:
            for it, pid, sets, nid in chunk:
                print(f"DRY {it['url']} -> project {it['project']} {it.get('status') or ''} {it.get('priority') or ''}")
            continue
        res = gql("mutation(" + ",".join(decl) + "){" + " ".join(parts) + "}", vars_)
        parts, vars_, decl = [], {}, []
        for k, (it, pid, sets, nid) in enumerate(chunk):
            item_id = res[f"a{k}"]["item"]["id"]
            for j, (fid, oid) in enumerate(sets):
                parts.append(f'u{k}_{j}:updateProjectV2ItemFieldValue(input:{{projectId:"{pid}",itemId:"{item_id}",fieldId:"{fid}",value:{{singleSelectOptionId:"{oid}"}}}}){{projectV2Item{{id}}}}')
        if parts:
            gql("mutation{" + " ".join(parts) + "}")
        for it, *_ in chunk:
            print(f"OK {it['url']} -> project {it['project']} {it.get('status') or ''} {it.get('priority') or ''}".rstrip())


STATUS_Q = """query($p:ID!){node(id:$p){...on ProjectV2{fields(first:50){nodes{
  ...on ProjectV2SingleSelectField{id name options{id name color description}}}}}}}"""
ITEMS_Q = """query($p:ID!,$c:String){node(id:$p){...on ProjectV2{items(first:100,after:$c){
  pageInfo{hasNextPage endCursor} nodes{fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{optionId name}}}}}}}"""


def status_field(pid):
    nodes = gql(STATUS_Q, {"p": pid})["node"]["fields"]["nodes"]
    f = next((x for x in nodes if x and x.get("name") == "Status"), None)
    if not f:
        sys.exit("board has no Status field")
    return f


def count_status(pid):
    """{option name or None: count}; 'None' key = items without status."""
    counts, cur = {}, None
    while True:
        it = gql(ITEMS_Q, {"p": pid, "c": cur})["node"]["items"]
        for n in it["nodes"]:
            v = n.get("fieldValueByName")
            k = v["name"] if v else None
            counts[k] = counts.get(k, 0) + 1
        if not it["pageInfo"]["hasNextPage"]:
            return counts
        cur = it["pageInfo"]["endCursor"]


ITEMS_FULL_Q = """query($p:ID!,$c:String){node(id:$p){...on ProjectV2{items(first:100,after:$c){
  pageInfo{hasNextPage endCursor} nodes{
  status:fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{name updatedAt}}
  priority:fieldValueByName(name:"Priority"){...on ProjectV2ItemFieldSingleSelectValue{name}}
  content{...on Issue{number title url state repository{nameWithOwner}
    labels(first:30){nodes{name}} assignees(first:10){nodes{login}}}}}}}}}"""


def norm_name(t):
    return re.sub(r"^[^\w]+", "", (t or "").lower()).strip()   # ignore leading emoji, like lookup()


def list_items(ids, project, status):
    """Issues (not PRs or drafts) of one board whose Status matches `status`, as plain dicts. Priority rank = position of the
    Priority option in the board's own option list (0 = first = most urgent); no priority = 99."""
    board = ids["boards"][str(project)]
    prio_order = [norm_name(k) for k in board["fields"].get("Priority", {}).get("options", {})]
    out, cur = [], None
    while True:
        page = gql(ITEMS_FULL_Q, {"p": board["id"], "c": cur})["node"]["items"]
        for n in page["nodes"]:
            st, c = n.get("status"), n.get("content") or {}
            if not st or norm_name(st["name"]) != norm_name(status) or not c.get("url"):
                continue
            pr = (n.get("priority") or {}).get("name")
            rank = prio_order.index(norm_name(pr)) if pr and norm_name(pr) in prio_order else 99
            out.append({"project": int(project), "repo": c["repository"]["nameWithOwner"], "number": c["number"], "title": c["title"],
                        "url": c["url"], "state": c["state"].lower(), "labels": [x["name"] for x in c["labels"]["nodes"]],
                        "assignees": [x["login"] for x in c["assignees"]["nodes"]], "priority": pr, "rank": rank,
                        "status_at": st.get("updatedAt")})   # when the Status value was last set (UTC ISO); weekly Done counts use it
        if not page["pageInfo"]["hasNextPage"]:
            return out
        cur = page["pageInfo"]["endCursor"]


def cmd_items(a):
    ids = load_ids()
    projects = a.project or [int(k) for k, b in ids["boards"].items()
                             if any(norm_name(o) == norm_name(a.status) for o in b["fields"].get("Status", {}).get("options", {}))]
    g, _ = rates()
    if g["remaining"] < MIN_GQL:
        sys.exit(f"GraphQL remaining {g['remaining']} < {MIN_GQL}; stopping")
    res = []
    for p in sorted(projects):
        res += list_items(ids, p, a.status)
    print(json.dumps(res, ensure_ascii=False))


def show_counts(c):
    return ", ".join(f"{k or '(no status)'}={v}" for k, v in sorted(c.items(), key=lambda kv: str(kv[0])))


def cmd_status_count(a):
    pid = lookup(load_ids(), a.project, None, None) or sys.exit("unknown project")
    print(show_counts(count_status(pid)))


def add_status_option(ids, project, name, color="GRAY", description="", before=None, dry_run=False):
    """Add a Status option keeping every existing option and its id; `before` = existing option name to insert in front of."""
    pid = lookup(ids, project, None, None) or sys.exit("unknown project")
    f = status_field(pid)
    norm = lambda t: re.sub(r"^[^\w]+", "", t.lower()).strip()
    if any(norm(o["name"]) == norm(name) for o in f["options"]):
        sys.exit(f"refusing: Status option '{name}' already exists on project {project}")
    before_counts = count_status(pid)
    # updateProjectV2Field REPLACES the list: send every existing option with its id (ProjectV2SingleSelectFieldOptionInput.id)
    opts = [{"id": o["id"], "name": o["name"], "color": o["color"], "description": o["description"] or ""} for o in f["options"]]
    pos = next((i for i, o in enumerate(opts) if before and norm(o["name"]) == norm(before)), len(opts))
    opts.insert(pos, {"name": name, "color": color, "description": description})
    print(f"project {project}: {len(f['options'])} options kept with ids, adding '{name}' ({color}) at position {pos}; before: {show_counts(before_counts)}")
    if dry_run:
        print("DRY: nothing changed")
        return
    g, _ = rates()
    if g["remaining"] < MIN_GQL:
        sys.exit(f"GraphQL remaining {g['remaining']} < {MIN_GQL}; stopping")
    res = gql("""mutation($f:ID!,$o:[ProjectV2SingleSelectFieldOptionInput!]!){updateProjectV2Field(input:{fieldId:$f,singleSelectOptions:$o}){
      projectV2Field{...on ProjectV2SingleSelectField{options{id name}}}}}""", {"f": f["id"], "o": opts})
    kept = {o["id"] for o in res["updateProjectV2Field"]["projectV2Field"]["options"]}
    lost = [o["name"] for o in f["options"] if o["id"] not in kept]
    after = count_status(pid)
    load_ids(True)
    print(f"after: {show_counts(after)}")
    if lost or {k: v for k, v in before_counts.items()} != {k: v for k, v in after.items() if k != name}:
        sys.exit(f"WARNING: option ids lost {lost} or counts differ - inspect the board now")
    print("OK counts identical, all option ids preserved")


def add_cmd_status(a):
    add_status_option(load_ids(), a.project, a.name, a.color, a.description, a.before, a.dry_run)


# ---- management view: sync-management -------------------------------------------------------------------------------------
LABEL = "management-tracking"
LABEL_COLOR = "5319E7"
LABEL_DESC = "Work whose progress management tracks"
YT_TITLE = "Management Tracking"
YT_PCT = "Progress %"
YT_NOTE = "Note"
YT_UPDATED = "Last updated"   # the built-in "Updated" field is not offered on new projects, so the sync fills this date
YT_VIEWS = (("Table", "table"), ("Board", "board"))   # (name, layout) of the views the project must have
NO_CHECKLIST = "no checklist"
CLOSED_DAYS = 30
# config key GH_BOARD_TEST_QUEUE / test-queue: number of the org test-queue project (0 = none); never a source for the management Status
_TEST_QUEUE = settings.value("test-queue", "GH_BOARD_TEST_QUEUE", "0")
SHARED_PROJECT = int(_TEST_QUEUE) if _TEST_QUEUE.isdigit() else 0
YT_STATUSES = ["Todo", "In Progress", "In review", "Done"]   # same names as the product boards, so the product Status maps 1:1

CHECKBOX = re.compile(r"^(?:[-*+]|\d+[.)])\s+\[([ xX])\](?:\s|$)")


def checklist_counts(body):
    """(checked, total) of markdown task-list items in an issue body; fenced code blocks are ignored."""
    done = total = 0
    fenced = False
    for line in (body or "").splitlines():
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            fenced = not fenced
            continue
        m = None if fenced else CHECKBOX.match(s)
        if m:
            total += 1
            done += m.group(1) in "xX"
    return done, total


def compute_management(issue):
    """(percent, note, Status name) for one issue dict from NODES_Q. The single % rule:
    closed issue or a product-board Status Done = 100 (Status comes only from product boards: the management project and the shared test-queue project are ignored); else (checked boxes + sum of the sub-issues' own % / 100) /
    (all boxes + number of sub-issues), recursive over the fetched sub-issue levels (deeper levels count closed/open as 1/0);
    no checklist and no sub-issues = 0 with the note 'no checklist'."""
    boards = [(n["project"]["number"], (n.get("status") or {}).get("name")) for n in issue["projectItems"]["nodes"] if n]
    names = {norm_name(st) for num, st in boards if st and num not in (issue.get("_self_project"), SHARED_PROJECT)}
    if issue["state"] == "CLOSED" or "done" in names:
        return 100, "", "Done"
    # "awaiting reply" = work blocked only on an outside reply: still started, so it maps to In Progress
    status = "In review" if "in review" in names else "In Progress" if names & {"in progress", "awaiting reply"} else "Todo"
    done, total = checklist_counts(issue["body"])
    summary = issue.get("subIssuesSummary") or {"total": 0, "completed": 0}
    fetched = (issue.get("subIssues") or {}).get("nodes")
    if fetched is None:      # deepest fetched level: closed/open only
        done, total = done + summary["completed"], total + summary["total"]
    else:
        for child in fetched:
            child["_self_project"] = issue.get("_self_project")
            done, total = done + compute_management(child)[0] / 100, total + 1
        total += max(0, summary["total"] - len(fetched))   # sub-issues beyond the fetched page count as not started
    if not total:
        return 0, NO_CHECKLIST, status
    return round(100 * done / total), "", status


_ISSUE_FIELDS = """id number state body subIssuesSummary{total completed}
  projectItems(first:20){nodes{project{number} status:fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{name}}}}"""
# root -> sub-issues -> their sub-issues (3 levels); the level below that comes from subIssuesSummary
NODES_Q = ("query($ids:[ID!]!){nodes(ids:$ids){...on Issue{" + _ISSUE_FIELDS + " url title updatedAt repository{nameWithOwner} assignees(first:10){nodes{login}} "
           "subIssues(first:30){nodes{" + _ISSUE_FIELDS + " subIssues(first:30){nodes{" + _ISSUE_FIELDS + "}}}}}}}")
YT_FIELDS_Q = """query($p:ID!){node(id:$p){...on ProjectV2{fields(first:50){nodes{
  ...on ProjectV2FieldCommon{id name dataType}...on ProjectV2SingleSelectField{options{id name}}}}}}}"""
YT_ITEMS_Q = """query($p:ID!,$c:String){node(id:$p){...on ProjectV2{items(first:100,after:$c){pageInfo{hasNextPage endCursor} nodes{id
  content{...on Issue{id url}}
  pct:fieldValueByName(name:"Progress %"){...on ProjectV2ItemFieldNumberValue{number}}
  upd:fieldValueByName(name:"Last updated"){...on ProjectV2ItemFieldDateValue{date}}
  note:fieldValueByName(name:"Note"){...on ProjectV2ItemFieldTextValue{text}}
  status:fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{optionId}}}}}}}"""


def ensure_labels(dry):
    repos = gh("repo", "list", OWNER, "--limit", "400", "--no-archived", "--json", "name", "--jq", ".[].name").split()
    made = 0
    for r in repos:
        if dry:
            continue
        p = hidden.run(["gh", "api", "-X", "POST", f"repos/{OWNER}/{r}/labels", "-f", f"name={LABEL}", "-f", f"color={LABEL_COLOR}",
                            "-f", f"description={LABEL_DESC}"], capture_output=True, text=True, encoding="utf-8")
        if p.returncode == 0:
            made += 1
        elif "already_exists" not in p.stdout + p.stderr:
            print(f"WARN label {r}: {(p.stderr or p.stdout).strip()[:150]}")
    print(f"labels: {len(repos)} repos checked, {made} created" + (" (dry-run: not touched)" if dry else ""))


def search_issue_ids():
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - CLOSED_DAYS * 86400))
    ids = {}
    for q in (f"org:{OWNER} label:{LABEL} is:issue is:open", f"org:{OWNER} label:{LABEL} is:issue is:closed closed:>={since}"):
        out = gh("api", "-X", "GET", "search/issues", "-f", f"q={q}", "-f", "per_page=100", "--paginate",
                 "--jq", ".items[] | [.node_id, .html_url] | @tsv")
        for line in out.splitlines():
            nid, url = line.split("\t")
            ids[nid] = url
    return ids


def ensure_project(ids, dry):
    """(number, board id, fields) of the management project, creating the project, fields and Status options when missing."""
    def find(d):
        return next((k for k, b in d["boards"].items() if b["title"] == YT_TITLE), None)
    num = find(ids) or find(load_ids(True))
    if not num:
        if dry:
            print(f"DRY: would create org project '{YT_TITLE}' with fields '{YT_PCT}', '{YT_NOTE}' and Status {YT_STATUSES}")
            return None
        org = json.loads(gh("api", f"orgs/{OWNER}"))["node_id"]
        gql("mutation($o:ID!,$t:String!){createProjectV2(input:{ownerId:$o,title:$t}){projectV2{id number}}}", {"o": org, "t": YT_TITLE})
        ids = load_ids(True)
        num = find(ids)
        print(f"created project #{num} '{YT_TITLE}'")
    pid = ids["boards"][num]["id"]
    fields = {f["name"]: f for f in gql(YT_FIELDS_Q, {"p": pid})["node"]["fields"]["nodes"] if f and f.get("name")}
    for name, typ in ((YT_PCT, "NUMBER"), (YT_NOTE, "TEXT"), (YT_UPDATED, "DATE")):
        if name not in fields:
            if dry:
                print(f"DRY: would create field '{name}' ({typ})")
                continue
            r = gql("mutation($p:ID!,$n:String!,$t:ProjectV2CustomFieldType!){createProjectV2Field(input:{projectId:$p,dataType:$t,name:$n}){"
                    "projectV2Field{...on ProjectV2FieldCommon{id name dataType}}}}", {"p": pid, "n": name, "t": typ})
            fields[name] = r["createProjectV2Field"]["projectV2Field"]
            print(f"created field '{name}'")
    have = {norm_name(o["name"]) for o in fields["Status"]["options"]}
    for st in YT_STATUSES:
        if norm_name(st) not in have and not dry:
            add_status_option(ids, num, st, before="Done" if st == "In review" else None)
    if not dry:
        fields["Status"] = next(f for f in gql(YT_FIELDS_Q, {"p": pid})["node"]["fields"]["nodes"] if f and f.get("name") == "Status")
        ensure_views(num, pid)
        drop_default_workflows(pid)
    return num, pid, fields


def drop_default_workflows(pid):
    """The project is a read-only mirror: 'Auto-close issue' would close an open issue the moment the sync sets Status Done, and
    'Auto-add sub-issues' would re-add sub-issues that the sync removes. The API can delete a workflow but not disable it."""
    for w in gql("query($p:ID!){node(id:$p){...on ProjectV2{workflows(first:20){nodes{id name}}}}}", {"p": pid})["node"]["workflows"]["nodes"]:
        if w["name"] in ("Auto-close issue", "Auto-add sub-issues to project"):
            gql("mutation($w:ID!){deleteProjectV2Workflow(input:{workflowId:$w}){deletedWorkflowId}}", {"w": w["id"]})
            print(f"removed project workflow '{w['name']}'")


def ensure_views(num, pid):
    """Create the missing views (the REST endpoint takes grouping and visible columns, GraphQL cannot set grouping)."""
    have = {v["name"] for v in gql("query($p:ID!){node(id:$p){...on ProjectV2{views(first:30){nodes{name}}}}}", {"p": pid})["node"]["views"]["nodes"]}
    todo = [(n, l) for n, l in YT_VIEWS if n not in have]
    if not todo:
        return
    rest = {f["name"]: f["id"] for f in json.loads(gh("api", f"orgs/{OWNER}/projectsV2/{num}/fields"))}
    cols = [rest[n] for n in ("Title", "Repository", "Assignees", "Status", YT_PCT, "Sub-issues progress", YT_UPDATED, YT_NOTE) if n in rest]
    for name, layout in todo:
        body = {"name": name, "layout": layout, "visible_fields": cols}
        body["group_by" if layout == "table" else "vertical_group_by"] = [rest["Status"]]
        if layout == "table":
            body["sort_by"] = [[rest[YT_PCT], "asc"]]
        gh("api", "-X", "POST", f"orgs/{OWNER}/projectsV2/{num}/views", "--input", "-", inp=json.dumps(body))
        print(f"created view '{name}' ({layout})")


def confirm_removals(candidates):
    """The search can lag or miss repos this gh login cannot see: remove an item only when its issue is readable and really
    qualifies (label gone, or closed more than CLOSED_DAYS ago); anything else is kept and reported."""
    keep, limit = [], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - CLOSED_DAYS * 86400))
    for i in range(0, len(candidates), 50):
        chunk = candidates[i:i + 50]
        nodes = gql("""query($ids:[ID!]!){nodes(ids:$ids){...on Issue{id state closedAt labels(first:100){nodes{name}}}}}""",
                    {"ids": [n for n, _ in chunk]})["nodes"]
        for (n, e), node in zip(chunk, nodes):
            if not node:
                print(f"KEEP {e['url']} (issue not readable with this login)")
            elif LABEL in [x["name"] for x in node["labels"]["nodes"]] and not (node["state"] == "CLOSED" and node["closedAt"] < limit):
                print(f"KEEP {e['url']} (still labelled, search did not return it)")
            else:
                keep.append((n, e))
    return keep


def sync_cmd_management(a):
    g, _ = rates()
    if g["remaining"] < MIN_GQL and not a.dry_run:
        sys.exit(f"GraphQL remaining {g['remaining']} < {MIN_GQL}; stopping")
    ensure_labels(a.dry_run)
    wanted = search_issue_ids()
    boot = ensure_project(load_ids(), a.dry_run)
    issues = {}
    nids = list(wanted)
    for i in range(0, len(nids), 25):
        for n in gql(NODES_Q, {"ids": nids[i:i + 25]})["nodes"]:
            if n:
                n["_self_project"] = int(boot[0]) if boot else None
                issues[n["id"]] = n
    if len(issues) != len(wanted):
        sys.exit(f"search found {len(wanted)} issues but {len(issues)} could be read; nothing changed")
    computed = {nid: compute_management(n) for nid, n in issues.items()}
    if not boot:
        for nid, (pct, note, st) in computed.items():
            print(f"DRY {issues[nid]['url']} -> {pct}% {st} {note}".rstrip())
        return
    num, pid, fields = boot
    opt = {norm_name(o["name"]): o["id"] for o in fields["Status"]["options"]}
    cur, existing = None, {}
    while True:
        page = gql(YT_ITEMS_Q, {"p": pid, "c": cur})["node"]["items"]
        for n in page["nodes"]:
            c = n.get("content") or {}
            if c.get("id"):
                existing[c["id"]] = {"item": n["id"], "pct": (n.get("pct") or {}).get("number"), "note": (n.get("note") or {}).get("text") or "",
                                     "upd": (n.get("upd") or {}).get("date"),
                                     "status": (n.get("status") or {}).get("optionId"), "url": c["url"]}
        if not page["pageInfo"]["hasNextPage"]:
            break
        cur = page["pageInfo"]["endCursor"]
    to_add = [n for n in issues if n not in existing]
    to_del = [(n, e) for n, e in existing.items() if n not in issues]
    to_del = confirm_removals(to_del)
    item_ids = {n: e["item"] for n, e in existing.items()}
    for i in range(0, len(to_add), BATCH):
        chunk = to_add[i:i + BATCH]
        for n in chunk:
            print(f"{'DRY ' if a.dry_run else ''}ADD {issues[n]['url']}")
        if a.dry_run:
            continue
        decl = ",".join(f"$c{k}:ID!" for k in range(len(chunk)))
        parts = " ".join(f'a{k}:addProjectV2ItemById(input:{{projectId:"{pid}",contentId:$c{k}}}){{item{{id}}}}' for k in range(len(chunk)))
        res = gql("mutation(" + decl + "){" + parts + "}", {f"c{k}": n for k, n in enumerate(chunk)})
        for k, n in enumerate(chunk):
            item_ids[n] = res[f"a{k}"]["item"]["id"]
    muts = []
    for n, (pct, note, st) in computed.items():
        e = existing.get(n, {"pct": None, "note": "", "status": None, "upd": None})
        upd = issues[n]["updatedAt"][:10]
        iid = item_ids.get(n, "DRYRUN")
        field = lambda fid, val: (f'updateProjectV2ItemFieldValue(input:{{projectId:"{pid}",itemId:"{iid}",fieldId:"{fid}",value:{val}}})'
                                  '{clientMutationId}')
        todo = []
        if e["pct"] != pct:
            todo.append(field(fields[YT_PCT]["id"], f"{{number:{pct}}}"))
        if e["note"] != note:
            todo.append(field(fields[YT_NOTE]["id"], f"{{text:{json.dumps(note, ensure_ascii=False)}}}") if note else
                        f'clearProjectV2ItemFieldValue(input:{{projectId:"{pid}",itemId:"{iid}",fieldId:"{fields[YT_NOTE]["id"]}"}}){{clientMutationId}}')
        if e["upd"] != upd:
            todo.append(field(fields[YT_UPDATED]["id"], f'{{date:"{upd}"}}'))
        if e["status"] != opt[norm_name(st)]:
            todo.append(field(fields["Status"]["id"], f'{{singleSelectOptionId:"{opt[norm_name(st)]}"}}'))
        if todo:
            print(f"{'DRY ' if a.dry_run else ''}SET {issues[n]['url']} -> {pct}% {st} {note}".rstrip())
            muts += todo
    for n, e in to_del:
        print(f"{'DRY ' if a.dry_run else ''}DEL {e['url']} (label removed or closed over {CLOSED_DAYS} days ago)")
        muts.append(f'deleteProjectV2Item(input:{{projectId:"{pid}",itemId:"{e["item"]}"}}){{deletedItemId}}')
    if not a.dry_run:
        for i in range(0, len(muts), 30):
            gql("mutation{" + " ".join(f"m{k}:{m}" for k, m in enumerate(muts[i:i + 30])) + "}")
    print(f"project #{num}: {len(issues)} issues, {len(to_add)} added, {len(to_del)} removed, {len(muts)} mutations" + (" (dry-run)" if a.dry_run else ""))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("rate").set_defaults(f=cmd_rate)
    i = s.add_parser("ids")
    i.add_argument("--refresh", action="store_true")
    i.set_defaults(f=cmd_ids)
    t = s.add_parser("set")
    t.add_argument("--project", type=int)
    t.add_argument("--status")
    t.add_argument("--priority")
    t.add_argument("--file")
    t.add_argument("--dry-run", action="store_true")
    t.add_argument("urls", nargs="*")
    t.set_defaults(f=cmd_set)
    d = s.add_parser("status-count")
    d.add_argument("--project", type=int, required=True)
    d.set_defaults(f=cmd_status_count)
    it = s.add_parser("items")
    it.add_argument("--status", required=True)
    it.add_argument("--project", type=int, action="append")
    it.set_defaults(f=cmd_items)
    e = s.add_parser("add-status")
    e.add_argument("--project", type=int, required=True)
    e.add_argument("name")
    e.add_argument("--color", default="GRAY")
    e.add_argument("--description", default="")
    e.add_argument("--before", help="insert in front of this existing option instead of at the end")
    e.add_argument("--dry-run", action="store_true")
    e.set_defaults(f=add_cmd_status)
    y = s.add_parser("sync-management")
    y.add_argument("--dry-run", action="store_true")
    y.set_defaults(f=sync_cmd_management)
    a = p.parse_args()
    if a.cmd != "rate" and not OWNER:
        sys.exit("gh_board: GH_ORG is not set (the GitHub org that owns the project boards; README, Configuration)")
    a.f(a)


if __name__ == "__main__":
    main()
