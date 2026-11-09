#!/usr/bin/env python3
"""Compute the five peer-review appraisal metrics from data/prs.csv + data/findings.csv.

Writes docs/metrics.md. Reads only the normalized CSVs (run normalize.py first).

Population: prs.csv rows with empty excluded_reason AND a non-empty approved_at
(every such PR is merged). Kept PRs without an approval are counted but not measured.

Definitions (as given):
  rounds            prs.csv `rounds` = review exchanges on the merged develop/test pair: the first
                    non-author review, each change request, and each approval that follows a change
                    request or a push; mirrored approvals (same reviewer, within 10 min) count once.
                    reviews.csv `round_rule` records the rule per review (see normalize.py).
  findings (per PR) findings.csv rows with empty excluded_reason, grouped by (repo, counted_on):
                    human inline review comments + CHANGES_REQUESTED reviews, both duplicate copies.
  time to approval  created_at -> approved_at (first APPROVED), in hours.
  comment-free      approval where the approver left no inline comment counted on that PR
                    (findings.csv kind=inline_comment, user=approver) and had_changes_requested=false.
  size              additions + deletions of the kept PR. Buckets 0-50, 51-200, 201-500,
                    501-1000, 1001+ (inclusive bounds).
  density           pooled ratios: sum(findings) / (sum(size)/1000) and
                    sum(findings) / sum(time-to-approval hours). Not means of per-PR ratios.
Quantiles use the inclusive method (statistics.quantiles(method="inclusive")).
Any group with fewer than THIN PRs is flagged.
"""
from __future__ import annotations

import csv
import re
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "docs" / "metrics.md"
THIN = 10
BUCKETS = [("0-50", 0, 50), ("51-200", 51, 200), ("201-500", 201, 500),
           ("501-1000", 501, 1000), ("1001+", 1001, float("inf"))]

# PRs to verify by hand against GitHub: (label, repo, number)
VERIFY = [
    ("Change request", "ddys-definition", 81),
    ("Comment-free approval", "ddys-report", 78),
    ("Duplicate pair (kept develop PR; findings came from the test copy)", "ddys-task-assignment", 73),
    ("Large PR", "ddys-portal", 636),
    ("Manually dismissed change request", "ddys-portal", 693),
]


def ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")


def bucket_of(size: int) -> str:
    return next(name for name, lo, hi in BUCKETS if lo <= size <= hi)


def load():
    prs = list(csv.DictReader(open(DATA / "prs.csv", encoding="utf-8")))
    findings = list(csv.DictReader(open(DATA / "findings.csv", encoding="utf-8")))
    counted = [f for f in findings if not f["excluded_reason"]]
    per_pr = Counter((f["repo"], f["counted_on"]) for f in counted)
    approver_inline = {(f["repo"], f["counted_on"], f["user"]) for f in counted if f["kind"] == "inline_comment"}
    kept = [r for r in prs if not r["excluded_reason"]]
    rows = []
    for r in kept:
        if not r["approved_at"]:
            continue
        key = (r["repo"], r["number"])
        size = int(r["additions"]) + int(r["deletions"])
        rows.append({
            "repo": r["repo"], "number": int(r["number"]), "url": r["pr_url"],
            "rounds": int(r["rounds"]), "rounds_from_duplicate": int(r["rounds_from_duplicate"]), "size": size, "bucket": bucket_of(size),
            "findings": per_pr[key],
            "hours": (ts(r["approved_at"]) - ts(r["created_at"])).total_seconds() / 3600,
            "comment_free": r["had_changes_requested"] == "false"
                            and (r["repo"], r["number"], r["approver"]) not in approver_inline,
        })
    unapproved = [r for r in kept if not r["approved_at"]]
    reviews = list(csv.DictReader(open(DATA / "reviews.csv", encoding="utf-8")))
    # A test-copy review (excluded_reason exactly duplicate_of:N) counts toward kept PR N.
    def counted_on(v):
        why = v["excluded_reason"]
        if not why:
            return int(v["number"])
        return int(why.split(":")[1]) if re.fullmatch(r"duplicate_of:\d+", why) else None
    measured = {(r["repo"], r["number"]) for r in rows}
    rules = Counter(v["round_rule"].split(":")[0] for v in reviews
                    if v["is_pr_author"] == "false" and (v["repo"], counted_on(v)) in measured)
    return prs, findings, kept, rows, unapproved, per_pr, rules


# ---------- formatting helpers ----------

def q(values, n=4):
    v = sorted(values)
    if len(v) == 1:
        return [v[0]] * (n - 1)
    return st.quantiles(v, n=n, method="inclusive")


def num(x, d=1):
    """Ints print as ints; everything else (medians, means, ratios) with d decimals."""
    if x is None:
        return "–"
    return f"{x:,}" if isinstance(x, int) else f"{x:,.{d}f}"


def median(values):
    return float(st.median(values))


def pct(a, b):
    return f"{a} ({100 * a / b:.1f}%)" if b else "–"


def thin(n):
    return " ⚠ thin" if n < THIN else ""


def table(header, rows, text_cols=1):
    """Markdown table; the first `text_cols` columns are left-aligned, the rest right-aligned."""
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("---" if i < text_cols else "--:" for i in range(len(header))) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out)


def groups(rows, key):
    g = defaultdict(list)
    for r in rows:
        g[key(r)].append(r)
    return g


# ---------- metrics ----------

def m1_rounds(rs):
    c = Counter(min(r["rounds"], 3) for r in rs)
    n = len(rs)
    return [n, num(median(r["rounds"] for r in rs)), num(float(st.mean(r["rounds"] for r in rs)), 2),
            pct(c[1], n), pct(c[2], n), pct(c[3], n)]


def m2_comment_free(rs):
    cf = sum(r["comment_free"] for r in rs)
    return [len(rs), cf, f"{100 * cf / len(rs):.1f}%"]


def m3_time(rs):
    h = [r["hours"] for r in rs]
    q1, med, q3 = q(h)
    p90 = q(h, 10)[-1] if len(h) > 1 else h[0]
    return [len(h), num(q1), num(med), num(q3), num(p90), num(max(h)),
            sum(x > 24 for x in h), sum(x > 72 for x in h), sum(x > 168 for x in h)]


def m4_bucket(rs):
    n = len(rs)
    cf = sum(r["comment_free"] for r in rs)
    return [f"{n}{thin(n)}", num(median(r["rounds"] for r in rs)), num(median(r["findings"] for r in rs)),
            num(median(r["hours"] for r in rs)), f"{100 * cf / n:.1f}%"]


def m5_density(rs):
    f = sum(r["findings"] for r in rs)
    kloc = sum(r["size"] for r in rs) / 1000
    hrs = sum(r["hours"] for r in rs)
    return [f"{len(rs)}{thin(len(rs))}", f, num(kloc), num(f / kloc, 2) if kloc else "–",
            num(hrs, 0), num(f / hrs, 3) if hrs else "–"]


def main():
    prs, findings, kept, rows, unapproved, per_pr, rules = load()
    repos = list(dict.fromkeys(r["repo"] for r in prs))
    by_repo = groups(rows, lambda r: r["repo"])
    by_bucket = groups(rows, lambda r: r["bucket"])
    bnames = [b[0] for b in BUCKETS]
    lines = []
    w = lines.append

    def repo_rows(fn, label_thin=True):
        out = []
        for repo in repos:
            rs = by_repo.get(repo, [])
            if rs:
                out.append([f"{repo}{thin(len(rs)) if label_thin else ''}"] + fn(rs))
        out.append(["**All**"] + fn(rows))
        return out

    w("# Peer review metrics — sample\n")
    w("Generated by `metrics.py` from `data/prs.csv` and `data/findings.csv`. "
      "Do not edit by hand; re-run the script.\n")
    w("## Population\n")
    w(f"- Kept PRs (`excluded_reason` empty): **{len(kept)}** of {len(prs)}.")
    w(f"- Measured: **{len(rows)}** kept PRs with an approval. All of them are merged.")
    unf = sum(per_pr[(r['repo'], r['number'])] for r in unapproved)
    w(f"- Not measured: {len(unapproved)} kept PRs with no approval. None of them is merged; they are "
      f"drafts, abandoned or still open. Between them they carry {unf} finding(s).")
    w(f"- Findings counted on measured PRs: **{sum(r['findings'] for r in rows)}**.")
    w(f"- Groups with fewer than {THIN} PRs are marked **⚠ thin**.\n")
    w("Definitions are in the docstring of `metrics.py`. Times are in hours.\n")

    w("## 1. Rounds to approval\n")
    w("A round is a review exchange on the PR, counting both copies of a develop/test pair together: the first "
      "review, every change request, and every approval that follows a change request or a new push. When the "
      "same reviewer approves both copies within 10 minutes, that counts as one round.\n")
    w(table(["Repo", "PRs", "Median", "Mean", "1 round", "2 rounds", "3+ rounds"], repo_rows(m1_rounds)))
    counted = sum(rules[k] for k in ("first_review", "changes_requested", "after_change_request", "after_push"))
    w(f"\n**How the {sum(r['rounds'] for r in rows)} rounds break down** "
      f"(`reviews.csv` `round_rule`, non-author reviews on measured PRs):\n")
    w(table(["Rule", "Reviews", "Counted as a round"], [
        ["First review", rules["first_review"], "yes"],
        ["Change request", rules["changes_requested"], "yes"],
        ["Approval after a change request", rules["after_change_request"], "yes"],
        ["Approval after a new push", rules["after_push"], "yes"],
        ["Mirrored approval (same reviewer within 10 min, either copy)", rules["mirror_of"], "no"],
        ["Other (later comment, or approval with nothing new since the last round)", rules["not_counted"], "no"],
    ]))
    w(f"\n{counted} reviews count as rounds. {sum(r['rounds_from_duplicate'] for r in rows)} rounds, on "
      f"{sum(r['rounds_from_duplicate'] > 0 for r in rows)} PRs, exist only on the test copy of a pair "
      f"(`rounds_from_duplicate`).")
    w("")

    w("## 2. Comment-free approval rate\n")
    w("An approval is comment-free when the approver left no inline comment on the PR (either duplicate copy) "
      "and the PR had no change request.\n")
    w(table(["Repo", "Approvals", "Comment-free", "Rate"], repo_rows(m2_comment_free)))
    w("")

    w("## 3. Time from PR creation to first approval (hours)\n")
    w(table(["Repo", "PRs", "Q1", "Median", "Q3", "P90", "Max", ">24 h", ">72 h", ">7 d"], repo_rows(m3_time)))
    w("\n**Long tail: the 10 slowest PRs**\n")
    slow = sorted(rows, key=lambda r: -r["hours"])[:10]
    w(table(["Repo", "PR", "Hours", "Days", "Size", "Rounds", "Findings"],
            [[r["repo"], f"[#{r['number']}]({r['url']})", num(r["hours"]), num(r["hours"] / 24),
              f"{r['size']:,}", r["rounds"], r["findings"]] for r in slow]))
    w("")

    w("## 4. Size vs review depth\n")
    w("Medians are taken over the PRs in each bucket.\n")
    hdr = ["Bucket (lines)", "PRs", "Median rounds", "Median findings", "Median hours to approval", "Comment-free"]
    w("**All repos**\n")
    w(table(hdr, [[b] + m4_bucket(by_bucket[b]) for b in bnames if by_bucket.get(b)]))
    w("\n**By repo**\n")
    out = []
    for repo in repos:
        g = groups(by_repo.get(repo, []), lambda r: r["bucket"])
        out += [[f"{repo} · {b}"] + m4_bucket(g[b]) for b in bnames if g.get(b)]
    w(table(["Repo · bucket"] + hdr[1:], out))
    w("")

    w("## 5. Findings density\n")
    w("Pooled ratios: total findings ÷ total KLOC, and total findings ÷ total hours from creation to approval.\n")
    hdr5 = ["Group", "PRs", "Findings", "KLOC", "Findings / KLOC", "Approval hours", "Findings / hour"]
    w("**By bucket**\n")
    w(table(hdr5, [[b] + m5_density(by_bucket[b]) for b in bnames if by_bucket.get(b)]
            + [["**All**"] + m5_density(rows)]))
    w("\n**By repo**\n")
    w(table(hdr5, [[repo] + m5_density(by_repo[repo]) for repo in repos if by_repo.get(repo)]
            + [["**All**"] + m5_density(rows)]))
    w("\n**By repo and bucket**\n")
    out = []
    for repo in repos:
        g = groups(by_repo.get(repo, []), lambda r: r["bucket"])
        out += [[f"{repo} · {b}"] + m5_density(g[b]) for b in bnames if g.get(b)]
    w(table(hdr5, out))
    w("")

    w("## Manual verification against GitHub\n")
    w("For each PR, open the URL and compare against the CSV values below.\n")
    pr_index = {(r["repo"], int(r["number"])): r for r in prs}
    m_index = {(r["repo"], r["number"]): r for r in rows}
    for label, repo, n in VERIFY:
        r = pr_index[(repo, n)]
        m = m_index.get((repo, n))
        w(f"### {label}: {repo} #{n}\n")
        w(f"<{r['pr_url']}>\n")
        cols = ["created_at", "first_review_at", "approved_at", "merged_at", "rounds", "additions", "deletions",
                "changed_files", "human_inline_comments", "bot_comments", "had_changes_requested", "approver",
                "merged_by", "seconds_between_approval_and_merge", "findings_from_duplicate", "rounds_from_duplicate", "excluded_reason"]
        w(table(["prs.csv column", "Expected"], [[c, f"`{r[c]}`" if r[c] != "" else "(empty)"] for c in cols],
                text_cols=2))
        if m:
            w(f"\nDerived: findings = **{m['findings']}**, hours to approval = **{num(m['hours'])}**, "
              f"bucket = **{m['bucket']}**, comment-free = **{str(m['comment_free']).lower()}**.")
        fs = [f for f in findings if f["repo"] == repo and f["counted_on"] == str(n) and not f["excluded_reason"]]
        if fs:
            w("\nFindings counted on this PR (`findings.csv`):\n")
            w(table(["kind", "made on PR", "user", "created_at", "url"],
                    [[f["kind"], f"#{f['number']}", f["user"], f["created_at"], f["url"]] for f in fs], text_cols=5))
        twins = [x for x in prs if x["repo"] == repo and x["excluded_reason"] == f"duplicate_of:{n}"]
        for t in twins:
            w(f"\nTest copy: <{t['pr_url']}>. Expect `excluded_reason` = `duplicate_of:{n}`, own "
              f"human_inline_comments = `{t['human_inline_comments']}`, had_changes_requested = "
              f"`{t['had_changes_requested']}`.")
        w("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(rows)} measured PRs")


if __name__ == "__main__":
    main()
