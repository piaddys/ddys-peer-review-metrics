#!/usr/bin/env python3
"""Normalize raw PR review data into CSVs. Reads raw/ only; no network.

Outputs (UTF-8, one header row, rows sorted by repo order in the manifest then PR number):
  data/prs.csv       one row per PR
  data/reviews.csv   one row per review record from /pulls/{n}/reviews
  data/findings.csv  one row per candidate finding; rows that are not findings carry excluded_reason

Exclusion rules (rows are never dropped; `excluded_reason` is written instead):
  promotion        base=test, head=develop or align/develop-to-test-*. Not a peer review.
  duplicate_of:N   test-side PR of a develop/test pair; N is the develop PR that is kept
                   (prs.csv, reviews.csv). Its findings are NOT excluded: they are merged into PR N.
  author_comment   COMMENTED review by the PR's own author (reviews.csv).
  author_reply     inline comment by the PR's own author (findings.csv).
  bot              comment by a Bot account, e.g. sonarqubecloud[bot] (findings.csv).
  issue_comment    human conversation comment; not an inline review comment (findings.csv).
  A review/finding on an excluded PR also inherits the PR's reason, joined with ';'
  (except duplicate_of on findings, see below).

Duplicate pairs count once, as the develop PR, with findings and rounds from both copies:
  On the kept develop row, human_inline_comments and bot_comments are summed over both copies,
  had_changes_requested is OR-ed over both, and rounds counts review exchanges over the merged
  pair (see below). findings_from_duplicate is the number of findings (findings.csv rows with
  empty excluded_reason) and rounds_from_duplicate the number of exchanges that exist only on
  the test copy. On every other row both are 0, and the excluded test row keeps its own
  unmerged values (its rounds are exchanges on that copy alone). Timestamps (first_review_at, approved_at),
  approver and seconds_between_approval_and_merge are NOT merged; they describe the develop PR.
  findings.csv: `number` is the PR the comment/review was made on; `counted_on` is the PR it
  counts toward (the develop PR for test-copy rows, otherwise the same as `number`).

Definitions:
  rounds            review exchanges on the PR (both copies of a duplicate pair, merged in
                    submitted_at order). Only non-author reviews take part, using original state:
                      first_review          the first non-author review counts as one exchange;
                      changes_requested     every CHANGES_REQUESTED review is an exchange;
                      after_change_request  an APPROVED review following a change request;
                      after_push            an APPROVED review following a push after a previous
                                            review. A push = the review's commit_id differs from
                                            the previous review's commit_id on the same copy
                                            (the timeline carries no push times);
                      mirror_of:ID          an APPROVED review by the same reviewer within 10 min
                                            of counted approval ID, on either copy: counted once;
                      not_counted           anything else (later COMMENTED reviews, approvals with
                                            no change request or push since the last exchange).
                    reviews.csv `round_rule` records the rule applied to each non-author review.
  first_review_at   earliest non-author review submitted_at.
  approved_at       earliest non-author review whose original state is APPROVED
                    (time to approval = created_at -> approved_at).
  findings          human inline review comments + CHANGES_REQUESTED reviews.
  "Original state": /reviews reports a dismissed review as DISMISSED. The state it had before
  dismissal comes from the review_dismissed timeline event (dismissed_review.state) and is
  used for approved_at, had_changes_requested and CHANGES_REQUESTED findings.
  approver / seconds_between_approval_and_merge use the last approval that still stands
  (state APPROVED, not dismissed) at or before merged_at, i.e. the approval the merge relied on.
"""
from __future__ import annotations

import csv
from collections import defaultdict
import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw"
DATA = ROOT / "data"

PROMOTION_HEAD = re.compile(r"^(develop|align/develop-to-test-.*)$")
TWIN_HEAD_SUFFIX = re.compile(r"([_-]develop|-test)$")
TWIN_TITLE_SUFFIX = re.compile(r"\s*\(develop\)\s*$")

PR_COLUMNS = [
    "repo", "number", "pr_url", "author", "created_at", "first_review_at", "approved_at",
    "merged_at", "rounds", "additions", "deletions", "changed_files", "human_inline_comments",
    "bot_comments", "had_changes_requested", "approver", "merged_by",
    "seconds_between_approval_and_merge", "findings_from_duplicate", "rounds_from_duplicate",
    "excluded_reason",
]
REVIEW_COLUMNS = [
    "repo", "number", "review_id", "reviewer", "reviewer_type", "is_pr_author", "state",
    "original_state", "submitted_at", "commit_id", "body_length", "review_url", "round_rule",
    "excluded_reason",
]
FINDING_COLUMNS = [
    "repo", "number", "counted_on", "kind", "id", "user", "user_type", "created_at", "path", "line",
    "pull_request_review_id", "in_reply_to_id", "body", "url", "excluded_reason",
]


def pages(directory: Path) -> list:
    items = []
    for f in sorted(directory.glob("p*.json")):
        if not f.name.endswith(".meta.json"):
            items += json.loads(f.read_bytes())
    return items


def ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")


def reasons(*parts: str) -> str:
    return ";".join(p for p in parts if p)


def load(org: str, repos: list[str]) -> list[dict]:
    prs = []
    for repo in repos:
        base = RAW / org / repo
        inline = pages(base / "pulls_comments")
        issue = pages(base / "issues_comments")
        for d in sorted((base / "pr").iterdir()):
            p = json.loads((d / "pull.json").read_bytes())
            n = p["number"]
            prs.append({
                "repo": repo, "pull": p,
                "reviews": pages(d / "reviews"),
                "timeline": pages(d / "timeline"),
                "inline": [c for c in inline if c["pull_request_url"].endswith(f"/pulls/{n}")],
                "issue": [c for c in issue if c["issue_url"].endswith(f"/issues/{n}")],
            })
    return prs


def is_promotion(p: dict) -> bool:
    return p["base"]["ref"] == "test" and bool(PROMOTION_HEAD.match(p["head"]["ref"]))


def pair_duplicates(prs: list[dict]) -> dict[tuple[str, int], int]:
    """Map (repo, test PR number) -> develop PR number for develop/test twins.

    Twins: same repo and author, one into develop and one into test, and either the head branch
    names match once a _develop/-develop/-test suffix is stripped, or the titles match once a
    trailing ' (develop)' is stripped. Each PR is paired at most once.
    """
    def keys(p):
        return (TWIN_HEAD_SUFFIX.sub("", p["head"]["ref"]), TWIN_TITLE_SUFFIX.sub("", p["title"]))

    develop = [x for x in prs if x["pull"]["base"]["ref"] == "develop"]
    used: set[int] = set()
    pairs = {}
    for x in prs:
        t = x["pull"]
        if t["base"]["ref"] != "test" or is_promotion(t):
            continue
        t_head, t_title = keys(t)
        for y in develop:
            d = y["pull"]
            if y["repo"] != x["repo"] or d["user"]["login"] != t["user"]["login"] or id(y) in used:
                continue
            d_head, d_title = keys(d)
            if d_head == t_head or d_title == t_title:
                pairs[(x["repo"], t["number"])] = d["number"]
                used.add(id(y))
                break
    return pairs


def normalize(prs: list[dict]) -> tuple[list, list, list]:
    twins = pair_duplicates(prs)
    pr_rows, review_rows, finding_rows = [], [], []

    for x in prs:
        p, repo = x["pull"], x["repo"]
        n, author = p["number"], p["user"]["login"]
        if is_promotion(p):
            pr_reason = "promotion"
        elif (repo, n) in twins:
            pr_reason = f"duplicate_of:{twins[(repo, n)]}"
        else:
            pr_reason = ""

        original = {e["dismissed_review"]["review_id"]: e["dismissed_review"]["state"].upper()
                    for e in x["timeline"] if e["event"] == "review_dismissed"}

        def orig_state(r):
            return original.get(r["id"], r["state"]) if r["state"] == "DISMISSED" else r["state"]

        others = sorted((r for r in x["reviews"] if r["user"]["login"] != author),
                        key=lambda r: r["submitted_at"])
        approvals = [r for r in others if orig_state(r) == "APPROVED"]
        standing = [r for r in others if r["state"] == "APPROVED"
                    and (not p["merged_at"] or r["submitted_at"] <= p["merged_at"])]
        last_ok = standing[-1] if standing else None

        human_inline = [c for c in x["inline"] if c["user"]["type"] != "Bot" and c["user"]["login"] != author]
        bot_comments = [c for c in x["inline"] + x["issue"] if c["user"]["type"] == "Bot"]

        pr_rows.append({
            "repo": repo, "number": n, "pr_url": p["html_url"], "author": author,
            "created_at": p["created_at"],
            "first_review_at": others[0]["submitted_at"] if others else "",
            "approved_at": approvals[0]["submitted_at"] if approvals else "",
            "merged_at": p["merged_at"] or "",
            "rounds": 0,  # set by count_rounds()
            "additions": p["additions"], "deletions": p["deletions"], "changed_files": p["changed_files"],
            "human_inline_comments": len(human_inline),
            "bot_comments": len(bot_comments),
            "had_changes_requested": str(any(orig_state(r) == "CHANGES_REQUESTED" for r in others)).lower(),
            "approver": last_ok["user"]["login"] if last_ok else "",
            "merged_by": (p["merged_by"] or {}).get("login", ""),
            "seconds_between_approval_and_merge":
                int((ts(p["merged_at"]) - ts(last_ok["submitted_at"])).total_seconds())
                if p["merged_at"] and last_ok else "",
            "findings_from_duplicate": 0,
            "rounds_from_duplicate": 0,
            "excluded_reason": pr_reason,
        })

        for r in sorted(x["reviews"], key=lambda r: r["submitted_at"]):
            own = r["user"]["login"] == author
            review_rows.append({
                "repo": repo, "number": n, "review_id": r["id"], "reviewer": r["user"]["login"],
                "reviewer_type": r["user"]["type"], "is_pr_author": str(own).lower(),
                "state": r["state"], "original_state": orig_state(r), "submitted_at": r["submitted_at"],
                "commit_id": r["commit_id"], "body_length": len((r["body"] or "").strip()),
                "review_url": r["html_url"], "round_rule": "",
                "excluded_reason": reasons(pr_reason, "author_comment" if own and r["state"] == "COMMENTED" else ""),
            })
            if not own and orig_state(r) == "CHANGES_REQUESTED":
                finding_rows.append({
                    "repo": repo, "number": n, "counted_on": n, "kind": "changes_requested", "id": r["id"],
                    "user": r["user"]["login"], "user_type": r["user"]["type"],
                    "created_at": r["submitted_at"], "path": "", "line": "",
                    "pull_request_review_id": r["id"], "in_reply_to_id": "",
                    "body": r["body"] or "", "url": r["html_url"], "excluded_reason": pr_reason,
                })

        for kind, comments in (("inline_comment", x["inline"]), ("issue_comment", x["issue"])):
            for c in comments:
                bot = c["user"]["type"] == "Bot"
                if bot:
                    why = "bot"
                elif kind == "issue_comment":
                    why = "issue_comment"
                elif c["user"]["login"] == author:
                    why = "author_reply"
                else:
                    why = ""
                finding_rows.append({
                    "repo": repo, "number": n, "counted_on": n, "kind": "bot_comment" if bot else kind, "id": c["id"],
                    "user": c["user"]["login"], "user_type": c["user"]["type"],
                    "created_at": c["created_at"], "path": c.get("path", ""),
                    "line": c.get("line") if c.get("line") is not None else "",
                    "pull_request_review_id": c.get("pull_request_review_id") or "",
                    "in_reply_to_id": c.get("in_reply_to_id") or "",
                    "body": c["body"], "url": c["html_url"], "excluded_reason": reasons(pr_reason, why),
                })

    merge_duplicates(pr_rows, finding_rows, twins)
    count_rounds(pr_rows, review_rows, twins)
    order = {r: i for i, r in enumerate(dict.fromkeys(x["repo"] for x in prs))}
    finding_rows.sort(key=lambda f: (order[f["repo"]], f["number"], f["created_at"], str(f["id"])))
    return pr_rows, review_rows, finding_rows


def merge_duplicates(pr_rows: list[dict], finding_rows: list[dict], twins: dict) -> None:
    """Fold the test copy of each develop/test pair into the kept develop row (see module doc)."""
    rows = {(r["repo"], r["number"]): r for r in pr_rows}
    for (repo, test_n), dev_n in twins.items():
        test, dev = rows[(repo, test_n)], rows[(repo, dev_n)]
        dev["human_inline_comments"] += test["human_inline_comments"]
        dev["bot_comments"] += test["bot_comments"]
        if test["had_changes_requested"] == "true":
            dev["had_changes_requested"] = "true"
        for f in finding_rows:
            if f["repo"] == repo and f["number"] == test_n:
                f["counted_on"] = dev_n
                f["excluded_reason"] = reasons(*(part for part in f["excluded_reason"].split(";")
                                                 if not part.startswith("duplicate_of:")))
                if not f["excluded_reason"]:
                    dev["findings_from_duplicate"] += 1


MIRROR_WINDOW_S = 600


def exchanges(reviews: list[dict]) -> dict:
    """Apply the round rules (module doc) to non-author review rows in time order.

    Returns {review_id: rule}. `reviews` are reviews.csv rows; `number` identifies the copy.
    """
    rules: dict = {}
    counted_approvals: list[dict] = []
    last_commit: dict = {}          # copy number -> commit_id of the previous review on that copy
    pending = None                  # why the next approval would count, if anything
    any_counted = False
    for r in sorted(reviews, key=lambda r: (r["submitted_at"], r["review_id"])):
        copy, state = r["number"], r["original_state"]
        if copy in last_commit and last_commit[copy] != r["commit_id"] and pending is None:
            pending = "after_push"
        last_commit[copy] = r["commit_id"]
        if state == "CHANGES_REQUESTED":
            rules[r["review_id"]] = "changes_requested"
            pending, any_counted = "after_change_request", True
        elif state == "APPROVED":
            mirror = next((a for a in counted_approvals if a["reviewer"] == r["reviewer"]
                           and abs((ts(r["submitted_at"]) - ts(a["submitted_at"])).total_seconds())
                           <= MIRROR_WINDOW_S), None)
            if mirror:
                rules[r["review_id"]] = f"mirror_of:{mirror['review_id']}"
            elif not any_counted or pending:
                rules[r["review_id"]] = "first_review" if not any_counted else pending
                counted_approvals.append(r)
                pending, any_counted = None, True
            else:
                rules[r["review_id"]] = "not_counted"
        elif not any_counted:
            rules[r["review_id"]] = "first_review"
            any_counted = True
        else:
            rules[r["review_id"]] = "not_counted"
    return rules


def count_rounds(pr_rows: list[dict], review_rows: list[dict], twins: dict) -> None:
    """Set rounds / rounds_from_duplicate and annotate reviews.csv round_rule (see module doc)."""
    by_pr = defaultdict(list)
    for v in review_rows:
        if v["is_pr_author"] == "false":
            by_pr[(v["repo"], v["number"])].append(v)
    counted = lambda rules: sum(not (x.startswith("mirror_of") or x == "not_counted") for x in rules.values())
    test_of = {(repo, dev): test for (repo, test), dev in twins.items()}
    for row in pr_rows:
        key = (row["repo"], row["number"])
        own = exchanges(by_pr[key])
        row["rounds"] = counted(own)
        if key in test_of:
            test_key = (row["repo"], test_of[key])
            pair = exchanges(by_pr[key] + by_pr[test_key])
            # A counted test-copy approval mirrored on the develop copy is attributed to develop.
            for v in by_pr[key]:
                m = pair[v["review_id"]]
                if m.startswith("mirror_of:"):
                    src = int(m.split(":")[1])
                    if any(t["review_id"] == src for t in by_pr[test_key]):
                        pair[v["review_id"]], pair[src] = pair[src], f"mirror_of:{v['review_id']}"
            test_ids = {t["review_id"] for t in by_pr[test_key]}
            row["rounds"] = counted(pair)
            row["rounds_from_duplicate"] = counted({i: x for i, x in pair.items() if i in test_ids})
            for v in by_pr[key] + by_pr[test_key]:
                v["round_rule"] = pair[v["review_id"]]
        elif key not in twins:
            for v in by_pr[key]:
                v["round_rule"] = own[v["review_id"]]


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def main() -> None:
    manifest = json.loads((RAW / "_meta" / "sample_manifest.json").read_text())
    prs = load(manifest["org"], manifest["repos"])
    pr_rows, review_rows, finding_rows = normalize(prs)
    write_csv(DATA / "prs.csv", PR_COLUMNS, pr_rows)
    write_csv(DATA / "reviews.csv", REVIEW_COLUMNS, review_rows)
    write_csv(DATA / "findings.csv", FINDING_COLUMNS, finding_rows)
    kept = lambda rows: sum(not r["excluded_reason"] for r in rows)
    print(f"prs.csv      {len(pr_rows)} rows, {kept(pr_rows)} not excluded")
    print(f"reviews.csv  {len(review_rows)} rows, {kept(review_rows)} not excluded")
    print(f"findings.csv {len(finding_rows)} rows, {kept(finding_rows)} not excluded")


if __name__ == "__main__":
    main()
