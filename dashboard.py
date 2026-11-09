#!/usr/bin/env python3
"""Build the self-contained, offline peer-review dashboard.

  python dashboard.py              -> docs/dashboard.html     (English, default)
  python dashboard.py --lang tr    -> docs/dashboard-tr.html  (Turkish)
  python dashboard.py --lang all   -> both

One script, one data source, one HTML template: the languages differ only in the TEXT string
tables and the Locale number/date formatting below.

Inputs: data/prs.csv, data/findings.csv, data/reviews.csv (via metrics.load) and the sample
manifest for the window. Measurements only; commentary lives in internal_notes.py.
All values are computed here and embedded; the HTML has no external requests. Charts are
inline SVG, so no chart library is needed. Re-run after normalize.py / metrics.py changes.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import metrics as M

ROOT = Path(__file__).resolve().parent
OUTPUTS = {"en": ROOT / "docs" / "dashboard.html", "tr": ROOT / "docs" / "dashboard-tr.html"}
RAW = ROOT / "raw"
BNAMES = [b[0] for b in M.BUCKETS]
STATE_COLORS = {"APPROVED": "var(--c-approve)", "CHANGES_REQUESTED": "var(--c-change)",
                "COMMENTED": "var(--c-comment)"}

esc = html.escape


def fmt_utc(s: str) -> str:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%d %H:%M UTC")


class Locale:
    """Number and date formatting. English: 1,234.5 / 84.6% / 2026-11-04. Turkish: 1.234,5 / %84,6 / 04.11.2026."""

    def __init__(self, lang: str):
        self.lang = lang
        self.js = {"en": "en-US", "tr": "tr-TR"}[lang]

    def num(self, x: float, d: int = 1) -> str:
        s = f"{x:,.{d}f}"
        return s if self.lang == "en" else s.translate(str.maketrans(",.", ".,"))

    def int(self, x: int) -> str:
        return self.num(x, 0)

    def pct(self, x: float, d: int = 1) -> str:
        return f"{self.num(x, d)}%" if self.lang == "en" else f"%{self.num(x, d)}"

    def utc(self, s: str) -> str:
        fmt = "%Y-%m-%d %H:%M UTC" if self.lang == "en" else "%d.%m.%Y %H:%M UTC"
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").strftime(fmt)

    def day(self, d) -> str:
        """Short week label on the chart axis."""
        return d.strftime("%b %d") if self.lang == "en" else d.strftime("%d.%m")


def link(url: str, text: str) -> str:
    return f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(text)}</a>'


# ---------- data ----------

def build():
    prs, findings, kept, rows, unapproved, per_pr, _ = M.load()
    manifest = json.loads((RAW / "_meta" / "sample_manifest.json").read_text())
    by_key = {(r["repo"], int(r["number"])): r for r in prs}
    for r in rows:
        src = by_key[(r["repo"], r["number"])]
        r.update(author=src["author"], approver=src["approver"], merged_by=src["merged_by"],
                 secs=int(src["seconds_between_approval_and_merge"]) if src["seconds_between_approval_and_merge"] else None,
                 files=int(src["changed_files"]), inline=int(src["human_inline_comments"]))
    measured = {(r["repo"], r["number"]) for r in rows}

    # Reviews counted in `rounds` of measured PRs (test-copy reviews count toward the develop PR).
    reviews = list(csv.DictReader(open(M.DATA / "reviews.csv", encoding="utf-8")))
    counted_reviews = []
    for v in reviews:
        why = v["excluded_reason"]
        m = re.fullmatch(r"duplicate_of:(\d+)", why)
        target = int(v["number"]) if not why else int(m.group(1)) if m else None
        if target is not None and v["is_pr_author"] == "false" and (v["repo"], target) in measured:
            counted_reviews.append(v)

    return prs, findings, kept, rows, manifest, by_key, counted_reviews


def q(values):
    return M.q(values)


# ---------- SVG charts ----------

def weekly_chart(reviews, loc: Locale, t: dict) -> str:
    def monday(s):
        d = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").date()
        return d - timedelta(days=d.weekday())
    weeks = sorted({monday(v["submitted_at"]) for v in reviews})
    states = ["APPROVED", "CHANGES_REQUESTED", "COMMENTED"]
    counts = {w: Counter() for w in weeks}
    for v in reviews:
        counts[monday(v["submitted_at"])][v["original_state"]] += 1
    W, H, L, B, T = 560, 260, 40, 40, 16
    top = max(sum(c.values()) for c in counts.values())
    top = int(-(-top // 20) * 20)
    pw, ph = W - L - 10, H - B - T
    bw = pw / len(weeks)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{t["aria_weekly"]}">']
    for i in range(0, top + 1, top // 4):
        y = T + ph - ph * i / top
        out.append(f'<line x1="{L}" x2="{W-10}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{L-6}" y="{y+4:.1f}" class="ax" text-anchor="end">{loc.int(i)}</text>')
    for k, w in enumerate(weeks):
        x = L + k * bw + bw * 0.18
        y0 = T + ph
        total = sum(counts[w].values())
        for s in states:
            n = counts[w][s]
            if not n:
                continue
            h = ph * n / top
            y0 -= h
            out.append(f'<rect x="{x:.1f}" y="{y0:.1f}" width="{bw*0.64:.1f}" height="{h:.1f}" '
                       f'style="fill:{STATE_COLORS[s]}"><title>{t["state_tip"][s]}: {loc.int(n)}</title></rect>')
        out.append(f'<text x="{x + bw*0.32:.1f}" y="{y0-5:.1f}" class="val" text-anchor="middle">{loc.int(total)}</text>')
        out.append(f'<text x="{x + bw*0.32:.1f}" y="{H-B+16}" class="ax" text-anchor="middle">{loc.day(w)}</text>')
    out.append("</svg>")
    legend = "".join(f'<span><i style="background:{STATE_COLORS[s]}"></i>{t["state_legend"][s]}</span>'
                     for s in states)
    return "\n".join(out), legend, weeks


def bucket_chart(by_bucket, loc: Locale, t: dict) -> str:
    """Two aligned panels over the same five buckets: comment-free rate, then findings per PR."""
    W, L, R = 560, 46, 10
    pw = W - L - R
    bw = pw / len(BNAMES)
    out = [f'<svg viewBox="0 0 {W} 330" role="img" aria-label="{t["aria_bucket"]}">']
    # Panel A: comment-free rate, 0-100%
    T, ph = 40, 100
    out.append(f'<text x="{L}" y="12" class="panel">{t["panel_rate"]}</text>')
    for p in (0, 50, 100):
        y = T + ph - ph * p / 100
        out.append(f'<line x1="{L}" x2="{W-R}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{L-6}" y="{y+4:.1f}" class="ax" text-anchor="end">{loc.pct(p, 0)}</text>')
    for k, b in enumerate(BNAMES):
        rs = by_bucket[b]
        rate = 100 * sum(r["comment_free"] for r in rs) / len(rs)
        x = L + k * bw + bw * 0.2
        h = ph * rate / 100
        out.append(f'<rect x="{x:.1f}" y="{T+ph-h:.1f}" width="{bw*0.6:.1f}" height="{h:.1f}" class="bar-key">'
                   f'<title>{t["tip_rate"].format(b=b, rate=loc.pct(rate), n=loc.int(len(rs)))}</title></rect>'
                   f'<text x="{x+bw*0.3:.1f}" y="{T+ph-h-5:.1f}" class="val" text-anchor="middle">{loc.pct(rate)}</text>')
    # Panel B: findings per PR, median and mean
    T2, ph2 = 182, 100
    means = {b: st.mean(r["findings"] for r in by_bucket[b]) for b in BNAMES}
    meds = {b: float(st.median(r["findings"] for r in by_bucket[b])) for b in BNAMES}
    top = max(1.0, max(means.values()) * 1.15)
    out.append(f'<text x="{L}" y="{T2-10}" class="panel">{t["panel_findings"]}</text>')
    for v in (0, top / 2, top):
        y = T2 + ph2 - ph2 * v / top
        out.append(f'<line x1="{L}" x2="{W-R}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{L-6}" y="{y+4:.1f}" class="ax" text-anchor="end">{loc.num(v)}</text>')
    for k, b in enumerate(BNAMES):
        x = L + k * bw + bw * 0.14
        for j, (val, cls, name) in enumerate(((meds[b], "bar-med", t["median"]), (means[b], "bar-mean", t["mean"]))):
            h = ph2 * val / top
            xx = x + j * bw * 0.37
            out.append(f'<rect x="{xx:.1f}" y="{T2+ph2-h:.1f}" width="{bw*0.34:.1f}" height="{max(h, 0):.1f}" class="{cls}">'
                       f'<title>{t["tip_findings"].format(b=b, name=name, val=loc.num(val, 2))}</title></rect>'
                       f'<text x="{xx+bw*0.17:.1f}" y="{T2+ph2-h-4:.1f}" class="val sm" text-anchor="middle">{loc.num(val)}</text>')
        out.append(f'<text x="{L + k*bw + bw/2:.1f}" y="{T2+ph2+18}" class="ax" text-anchor="middle">{b}</text>'
                   f'<text x="{L + k*bw + bw/2:.1f}" y="{T2+ph2+32}" class="ax dim" text-anchor="middle">n={loc.int(len(by_bucket[b]))}</text>')
    out.append(f'<text x="{L + pw/2:.1f}" y="328" class="ax" text-anchor="middle">{t["x_lines"]}</text>')
    out.append("</svg>")
    return "\n".join(out), meds, means


def spark(rates, loc: Locale, t: dict) -> str:
    W, H = 220, 64
    bw = W / len(rates)
    out = [f'<svg viewBox="0 0 {W} {H+16}" class="spark" role="img" aria-label="{t["aria_spark"]}">']
    for k, (b, r) in enumerate(rates):
        h = (H - 14) * r / 100
        x = k * bw + bw * 0.15
        out.append(f'<rect x="{x:.1f}" y="{H-h:.1f}" width="{bw*0.7:.1f}" height="{h:.1f}" class="bar-key">'
                   f'<title>{t["tip_spark"].format(b=b, rate=loc.pct(r))}</title></rect>'
                   f'<text x="{x+bw*0.35:.1f}" y="{H-h-3:.1f}" class="val sm" text-anchor="middle">{loc.pct(r, 0)}</text>'
                   f'<text x="{x+bw*0.35:.1f}" y="{H+12}" class="ax sm" text-anchor="middle">{t["short_bucket"][b]}</text>')
    out.append("</svg>")
    return "".join(out)


# ---------- page ----------

def render(lang: str, data) -> Path:
    prs, findings, kept, rows, manifest, by_key, reviews = data
    loc, t0 = Locale(lang), TEXT[lang]
    by_bucket = M.groups(rows, lambda r: r["bucket"])
    n = len(rows)
    end = max(v["completed_utc"] for v in manifest["per_repo"].values())

    cf = sum(r["comment_free"] for r in rows)
    hours = [r["hours"] for r in rows]
    h_q1, h_med, h_q3 = q(hours)
    over24 = sum(h > 24 for h in hours)
    rates = [(b, 100 * sum(r["comment_free"] for r in by_bucket[b]) / len(by_bucket[b])) for b in BNAMES]
    tot_f = sum(r["findings"] for r in rows)
    kloc = sum(r["size"] for r in rows) / 1000
    one = sum(r["rounds"] == 1 for r in rows)

    rv = list(csv.DictReader(open(M.DATA / "reviews.csv", encoding="utf-8")))
    reasons = Counter(r["excluded_reason"].split(":")[0] for r in prs if r["excluded_reason"])

    weekly_svg, weekly_legend, weeks = weekly_chart(reviews, loc, t0)
    bucket_svg, meds, means = bucket_chart(by_bucket, loc, t0)
    partial = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ").date() < weeks[-1] + timedelta(days=6)

    # Every number that appears in prose, formatted for this locale.
    v = dict(
        n=loc.int(n), n_kept=loc.int(len(kept)), n_all=loc.int(len(prs)), days=manifest["window_days"],
        n_repos=len(manifest["repos"]), window=f"{loc.utc(manifest['cutoff_utc'])} → {loc.utc(end)}",
        one=loc.int(one), one_pct=loc.pct(100 * one / n), multi=loc.int(n - one),
        cf=loc.int(cf), h_q1=loc.num(h_q1), h_q3=loc.num(h_q3), over24=loc.int(over24),
        b_first=rates[0][0], b_last=rates[-1][0], r_first=loc.pct(rates[0][1]), r_last=loc.pct(rates[-1][1]),
        tot_f=loc.int(tot_f), kloc=loc.num(kloc), f_per_h=loc.num(tot_f / sum(hours), 3),
        n_reviews=loc.int(len(reviews)), week=loc.day(weeks[-1]), end=loc.utc(end),
        n_promo=loc.int(reasons["promotion"]), n_dup=loc.int(reasons["duplicate_of"]),
        n_unapproved=loc.int(len(kept) - n), n_findings=loc.int(tot_f),
        n_author_comments=loc.int(sum("author_comment" in r["excluded_reason"] for r in rv)),
        n_sonar=loc.int(sum(1 for f in findings if f["user"] == "sonarqubecloud[bot]")),
    )
    v["partial_note"] = t0["partial"].format(**v) if partial else ""
    # Page strings get the prose values; chart/JS templates (tip_*, count_js) keep their own placeholders.
    t = {k: x.format(**v) if isinstance(x, str) and k != "count_js" and not k.startswith("tip_") else x
         for k, x in t0.items()}

    cards = [
        dict(key="rounds", big=loc.num(float(st.median(r["rounds"] for r in rows))), primary=False),
        dict(key="cf", big=loc.pct(100 * cf / n), primary=True),
        dict(key="time", big=loc.num(h_med), primary=False),
        dict(key="depth", big=f"{loc.pct(rates[0][1], 0)} → {loc.pct(rates[-1][1])}", primary=True, cls="tight",
             extra=spark(rates, loc, t0)),
        dict(key="density", big=loc.num(tot_f / kloc, 2), primary=False),
    ]
    card_html = "\n".join(f'''
      <article class="card{' primary' if c['primary'] else ''}">
        <div class="tagline">{t['tagline'] if c['primary'] else '&nbsp;'}</div>
        <h2 class="title">{esc(t['card_' + c['key'] + '_title'])}</h2>
        <div class="desc">{esc(t['card_' + c['key'] + '_desc'])}</div>
        <div class="big {c.get('cls', '')}">{esc(c['big'])}<span class="unit">{esc(t['card_' + c['key'] + '_unit'])}</span></div>
        {c.get('extra', '')}
        <p class="reading">{esc(t['card_' + c['key'] + '_reading'])}</p>
      </article>''' for c in cards)

    bucket_rows = "".join(
        f"<tr><td>{b}</td><td class='num'>{loc.int(len(by_bucket[b]))}</td><td class='num'>{loc.pct(rate)}</td>"
        f"<td class='num'>{loc.num(meds[b])}</td><td class='num'>{loc.num(means[b], 2)}</td></tr>" for b, rate in rates)

    table_data = [dict(repo=r["repo"], n=r["number"], url=r["url"], author=r["author"], approver=r["approver"],
                       rounds=r["rounds"], hours=round(r["hours"], 1), merge_s=r["secs"], lines=r["size"],
                       findings=r["findings"])
                  for r in sorted(rows, key=lambda r: (r["repo"], r["number"]))]
    repos = list(dict.fromkeys(r["repo"] for r in prs))

    page = TEMPLATE.format(
        t=t, cards=card_html,
        repo_options="".join(f'<option value="{esc(r)}">{esc(r)}</option>' for r in repos),
        weekly_svg=weekly_svg, weekly_legend=weekly_legend, bucket_svg=bucket_svg, bucket_rows=bucket_rows,
        js_locale=loc.js, count_js=t0["count_js"],
        data_json=json.dumps(table_data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"),
    )
    out = OUTPUTS[lang]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)} ({out.stat().st_size/1024:.0f} KB): {n} PRs, {len(reviews)} reviews")
    return out


def main():
    ap = argparse.ArgumentParser(description="Build the peer-review dashboard.")
    ap.add_argument("--lang", choices=["en", "tr", "all"], default="en",
                    help="en -> docs/dashboard.html, tr -> docs/dashboard-tr.html, all -> both (default en)")
    args = ap.parse_args()
    data = build()
    for lang in (["en", "tr"] if args.lang == "all" else [args.lang]):
        render(lang, data)



# ---------- strings ----------
# Every user-facing string, per language. Values may use {placeholders} from render()'s `v`
# (already locale-formatted). Turkish keeps technical terms in English (PR, review, reviewer,
# approval, approve, merge, change request, commit, branch, develop, test, inline comment) and
# gives defined concepts as "Türkçe (English term)".

TEXT = {
    "en": dict(
        html_lang="en",
        page_title="Peer Review Evidence",
        h1="Peer Review Evidence — piaddys",
        sub="CMMI Level 3 · Peer Review process area · GitHub pull request reviews",
        meta_window="Window (PR <code>updated_at</code>): <b>{window}</b>",
        meta_repos="Repositories: <b>{n_repos}</b>",
        meta_prs="PRs measured: <b>{n}</b> <span>(of {n_kept} kept, {n_all} collected)</span>",
        note="This is a {days}-day sample of {n_repos} repositories, not the full organization.",
        cards_aria="Measurements",
        tagline="Primary measure",
        card_rounds_title="Review rounds",
        card_rounds_desc="Review exchanges before approval",
        card_rounds_unit="median",
        card_rounds_reading="{one} of {n} PRs ({one_pct}) were approved in one round; {multi} had two or more.",
        card_cf_title="Comment-free approvals",
        card_cf_desc="Approvals with no reviewer comment and no change request",
        card_cf_unit="",
        card_cf_reading="{cf} of {n} approvals.",
        card_time_title="Time to approval",
        card_time_desc="From PR creation to first approval",
        card_time_unit="hours, median",
        card_time_reading="Q1 {h_q1} h, Q3 {h_q3} h; {over24} of {n} PRs took over 24 h.",
        card_depth_title="Review depth by change size",
        card_depth_desc="How comment-free rate varies with lines changed",
        card_depth_unit="",
        card_depth_reading="Comment-free rate is {r_first} for PRs of {b_first} lines and {r_last} for PRs of {b_last} lines.",
        card_density_title="Finding density",
        card_density_desc="Findings per thousand lines reviewed",
        card_density_unit="per KLOC",
        card_density_reading="{tot_f} findings over {kloc} KLOC; {f_per_h} per hour of time to approval.",
        table_h2="Measured pull requests",
        table_sub="Every PR behind the numbers above. Click a column to sort; links open the PR on GitHub.",
        filter_label="Repository",
        filter_all="All repositories",
        th_repo="Repository", th_n="PR", th_author="Author", th_approver="Approver", th_rounds="Rounds",
        th_hours="Hours to approval", th_merge="Seconds approval → merge", th_lines="Lines changed",
        th_findings="Findings",
        count_js="`${rows.length} of ${DATA.length} PRs`",
        noscript="The table needs JavaScript. All values are also in data/prs.csv.",
        charts_h2="Review activity and depth",
        weekly_h3="Reviews submitted per week",
        weekly_caption="{n_reviews} non-author reviews on measured PRs, grouped by the Monday of the week they were\n"
                       "    submitted (UTC). Approvals that were later dismissed are shown as approvals.{partial_note}",
        partial=" The last bar (week of {week}) is partial: collection ended {end}.",
        aria_weekly="Reviews submitted per week, by state",
        state_legend={"APPROVED": "Approved", "CHANGES_REQUESTED": "Changes Requested", "COMMENTED": "Commented"},
        state_tip={"APPROVED": "approved", "CHANGES_REQUESTED": "changes requested", "COMMENTED": "commented"},
        bucket_h3="Findings and comment-free rate by PR size",
        legend_rate="Comment-free rate", legend_med="Median findings", legend_mean="Mean findings",
        mt_lines="Lines", mt_prs="PRs", mt_cf="Comment-free", mt_med="Median findings", mt_mean="Mean findings",
        bucket_caption="The median is 0 findings in every bucket. The mean is shown alongside so the bars\n"
                       "    are not all empty.",
        aria_bucket="Comment-free rate and findings per PR by size bucket",
        panel_rate="Comment-free approval rate", panel_findings="Findings per PR",
        tip_rate="{b} lines: {rate} of {n} approvals", tip_findings="{b} lines, {name}: {val}",
        median="median", mean="mean", x_lines="Lines changed (additions + deletions)",
        aria_spark="Comment-free rate by size bucket", tip_spark="{b} lines: {rate}",
        short_bucket={"0-50": "≤50", "51-200": "≤200", "201-500": "≤500", "501-1000": "≤1k", "1001+": ">1k"},
        footer="""  <h3>Exclusion rules applied</h3>
  <ul>
    <li><b>Promotion PRs</b> ({n_promo} excluded). “Develop to test” branch promotions, opened and merged by
        the same person. These are not peer reviews.</li>
    <li><b>Duplicate pairs</b> ({n_dup} excluded). When the same change was opened into both
        <code>develop</code> and <code>test</code>, it is counted once, as the develop PR. Reviews and findings
        from both copies are counted on that PR.</li>
    <li><b>Author self-comments</b> ({n_author_comments} reviews excluded). A COMMENTED review by the PR's own
        author is a reply, not a review. The author's inline replies are not findings either.</li>
    <li><b>Sonar comments</b> ({n_sonar} comments). Comments by <code>sonarqubecloud[bot]</code> are not
        counted as findings.</li>
    <li><b>Not approved</b> ({n_unapproved} PRs). Kept PRs with no approval (drafts, abandoned or open) are not
        measured. None of them was merged.</li>
  </ul>
  <h3>Metric definitions</h3>
  <ul>
    <li><b>Rounds</b>: review exchanges on the PR, with both copies of a duplicate pair counted together. The
        first review counts, and so does every change request and every approval that follows a change request
        or a new push. When the same reviewer approves both copies within 10 minutes, that counts once.</li>
    <li><b>Time to approval</b>: from the PR's <code>created_at</code> to the <code>submitted_at</code> of its
        first approval.</li>
    <li><b>Findings</b>: human inline review comments plus CHANGES_REQUESTED reviews ({n_findings} on
        measured PRs). A comment-free approval is one with no inline comment from the approver and no change
        request on the PR.</li>
  </ul>
  <p>Source: GitHub REST API (version 2022-11-28), collected verbatim into <code>raw/</code>, normalized into
     <code>data/*.csv</code>. Data collected up to {end}; page generated by <code>dashboard.py</code>.</p>""",
    ),
    "tr": dict(
        html_lang="tr",
        page_title="Peer Review Kanıtları",
        h1="Peer Review Kanıtları — piaddys",
        sub="CMMI Seviye 3 · Peer Review süreç alanı · GitHub pull request review'ları",
        meta_window="Zaman aralığı (PR <code>updated_at</code>): <b>{window}</b>",
        meta_repos="Repo sayısı: <b>{n_repos}</b>",
        meta_prs="Ölçülen PR: <b>{n}</b> <span>(hariç tutulmayan {n_kept}, toplanan {n_all} PR içinden)</span>",
        note="Bu, organizasyonun tamamı değil; {n_repos} repo'dan alınmış {days} günlük bir örneklemdir.",
        cards_aria="Ölçümler",
        tagline="Temel ölçüt",
        card_rounds_title="İnceleme turu (review rounds)",
        card_rounds_desc="Approval'a kadar gerçekleşen review alışverişleri",
        card_rounds_unit="medyan",
        card_rounds_reading="{n} PR'ın {one} tanesi ({one_pct}) tek turda approve edildi; {multi} tanesinde iki "
                            "veya daha fazla tur oldu.",
        card_cf_title="Yorumsuz onay oranı (comment-free approvals)",
        card_cf_desc="Reviewer yorumu ve change request içermeyen approval'lar",
        card_cf_unit="",
        card_cf_reading="{n} approval'ın {cf} tanesi.",
        card_time_title="Onay süresi (time to approval)",
        card_time_desc="PR'ın açılmasından ilk approval'a kadar geçen süre",
        card_time_unit="saat, medyan",
        card_time_reading="Q1 {h_q1} saat, Q3 {h_q3} saat; {n} PR'ın {over24} tanesi 24 saatten uzun sürdü.",
        card_depth_title="Değişiklik boyutuna göre inceleme derinliği (review depth by change size)",
        card_depth_desc="Yorumsuz onay oranının değişen satır sayısına göre değişimi",
        card_depth_unit="",
        card_depth_reading="Yorumsuz onay oranı {b_first} satırlık PR'larda {r_first}, {b_last} satırlık "
                           "PR'larda {r_last}.",
        card_density_title="Bulgu yoğunluğu (finding density)",
        card_density_desc="İncelenen bin satır başına bulgu (finding) sayısı",
        card_density_unit="KLOC başına",
        card_density_reading="{kloc} KLOC üzerinde {tot_f} bulgu; onay süresinin saati başına {f_per_h}.",
        table_h2="Ölçülen pull request'ler",
        table_sub="Yukarıdaki sayıların dayandığı tüm PR'lar. Sıralamak için bir sütun başlığına tıklayın; "
                  "bağlantılar PR'ı GitHub'da açar.",
        filter_label="Repo",
        filter_all="Tüm repo'lar",
        th_repo="Repo", th_n="PR", th_author="Yazar", th_approver="Approver", th_rounds="Tur",
        th_hours="Onay süresi (saat)", th_merge="Approval → merge (saniye)", th_lines="Değişen satır",
        th_findings="Bulgu",
        count_js="`${rows.length} / ${DATA.length} PR`",
        noscript="Tablo JavaScript gerektirir. Tüm değerler data/prs.csv dosyasında da bulunur.",
        charts_h2="Review aktivitesi ve derinliği",
        weekly_h3="Haftalık gönderilen review'lar",
        weekly_caption="Ölçülen PR'larda, PR yazarı dışındaki kişilerin gönderdiği {n_reviews} review; gönderildikleri\n"
                       "    haftanın pazartesi gününe göre gruplanmıştır (UTC). Sonradan geçersiz kılınan (dismissed) "
                       "approval'lar approval olarak gösterilir.{partial_note}",
        partial=" Son çubuk ({week} haftası) eksiktir: veri toplama {end} itibarıyla sona erdi.",
        aria_weekly="Haftalık gönderilen review'lar, duruma göre",
        state_legend={"APPROVED": "Approval", "CHANGES_REQUESTED": "Change request", "COMMENTED": "Yorum (commented)"},
        state_tip={"APPROVED": "approval", "CHANGES_REQUESTED": "change request", "COMMENTED": "yorum (commented)"},
        bucket_h3="PR boyutuna göre bulgular ve yorumsuz onay oranı",
        legend_rate="Yorumsuz onay oranı", legend_med="Medyan bulgu", legend_mean="Ortalama bulgu",
        mt_lines="Satır", mt_prs="PR", mt_cf="Yorumsuz onay", mt_med="Medyan bulgu", mt_mean="Ortalama bulgu",
        bucket_caption="Medyan bulgu sayısı her grupta 0'dır. Çubukların hepsi boş görünmesin diye ortalama da\n"
                       "    yanında gösterilmiştir.",
        aria_bucket="Boyut grubuna göre yorumsuz onay oranı ve PR başına bulgu",
        panel_rate="Yorumsuz onay oranı", panel_findings="PR başına bulgu",
        tip_rate="{b} satır: {n} approval'ın {rate} kadarı", tip_findings="{b} satır, {name}: {val}",
        median="medyan", mean="ortalama", x_lines="Değişen satır (eklenen + silinen)",
        aria_spark="Boyut grubuna göre yorumsuz onay oranı", tip_spark="{b} satır: {rate}",
        short_bucket={"0-50": "≤50", "51-200": "≤200", "201-500": "≤500", "501-1000": "≤1 bin", "1001+": ">1 bin"},
        footer="""  <h3>Uygulanan hariç tutma kuralları (exclusion rules)</h3>
  <ul>
    <li><b>Ortam aktarım PR'ları (promotion PRs)</b> ({n_promo} PR hariç tutuldu). Aynı kişinin açıp merge
        ettiği “Develop to test” branch aktarımlarıdır. Bunlar peer review değildir.</li>
    <li><b>Mükerrer PR çiftleri (duplicate pairs)</b> ({n_dup} PR hariç tutuldu). Aynı değişiklik hem
        <code>develop</code> hem <code>test</code> branch'ine ayrı PR olarak açıldığında tek sayılır ve develop
        PR'ı esas alınır. İki kopyadaki review'lar ve bulgular bu PR'a sayılır.</li>
    <li><b>Yazarın kendi yorumları (author self-comments)</b> ({n_author_comments} review hariç tutuldu). PR
        yazarının kendi PR'ında bıraktığı COMMENTED durumundaki review bir yanıttır, review değildir. Yazarın
        inline comment olarak verdiği yanıtlar da bulgu sayılmaz.</li>
    <li><b>Sonar yorumları (Sonar comments)</b> ({n_sonar} yorum). <code>sonarqubecloud[bot]</code> tarafından
        yazılan yorumlar bulgu sayılmaz.</li>
    <li><b>Approval almamış PR'lar (not approved)</b> ({n_unapproved} PR). Hariç tutulmayan PR'lardan approval
        almamış olanlar (draft, yarım bırakılmış veya hâlâ açık) ölçüme dahil edilmez. Bunların hiçbiri merge
        edilmemiştir.</li>
  </ul>
  <h3>Metrik tanımları (metric definitions)</h3>
  <ul>
    <li><b>İnceleme turu (rounds)</b>: PR üzerindeki review alışverişlerinin sayısıdır; mükerrer bir çiftin iki
        kopyası birlikte değerlendirilir. İlk review bir tur sayılır. Ayrıca her change request ve bir change
        request'ten ya da yeni bir push'tan (yeni commit gönderiminden) sonra gelen her approval birer turdur.
        Aynı reviewer iki kopyayı 10 dakika içinde approve ettiğinde bu tek tur sayılır.</li>
    <li><b>Onay süresi (time to approval)</b>: PR'ın <code>created_at</code> zamanından ilk approval'ın
        <code>submitted_at</code> zamanına kadar geçen süredir.</li>
    <li><b>Bulgu (findings)</b>: insanların yazdığı inline review comment'ler ile CHANGES_REQUESTED durumundaki
        review'ların toplamıdır (ölçülen PR'larda {n_findings}). <b>Yorumsuz onay (comment-free approval)</b>,
        approver'ın PR'a hiç inline comment bırakmadığı ve PR'da change request bulunmayan approval'dır.</li>
  </ul>
  <p>Kaynak: GitHub REST API (sürüm 2022-11-28). Veriler <code>raw/</code> klasörüne değiştirilmeden toplanmış,
     <code>data/*.csv</code> dosyalarına normalize edilmiştir. Veriler {end} itibarıyla toplanmıştır; sayfa
     <code>dashboard.py</code> ile üretilmiştir.</p>""",
    ),
}


TEMPLATE = """<!doctype html>
<html lang="{t[html_lang]}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t[page_title]}</title>
<style>
:root {{
  --bg:#f6f7f9; --surface:#fff; --text:#1b1f24; --muted:#5b6470; --line:#dfe3e8; --grid:#e9ecf0;
  --accent:#1f5fbf; --accent-soft:#e8f0fb; --c-approve:#3f7fd9; --c-change:#d9822b; --c-comment:#9aa5b1;
  --bar-med:#9aa5b1; --bar-mean:#2f9e8f;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg:#111418; --surface:#1a1e24; --text:#e6e9ed; --muted:#9aa4b0; --line:#2c323a; --grid:#262b32;
    --accent:#6ea1ec; --accent-soft:#1c2a3d; --c-approve:#5b95e6; --c-change:#e59a4c; --c-comment:#6c7682;
    --bar-med:#6c7682; --bar-mean:#45b5a5;
  }}
}}
:root[data-theme="dark"] {{
  --bg:#111418; --surface:#1a1e24; --text:#e6e9ed; --muted:#9aa4b0; --line:#2c323a; --grid:#262b32;
  --accent:#6ea1ec; --accent-soft:#1c2a3d; --c-approve:#5b95e6; --c-change:#e59a4c; --c-comment:#6c7682;
  --bar-med:#6c7682; --bar-mean:#45b5a5;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif; }}
main {{ max-width:1240px; margin:0 auto; padding:28px 16px 48px; }}
a {{ color:var(--accent); }}
h1 {{ font-size:26px; margin:0 0 6px; letter-spacing:-.01em; }}
h2 {{ font-size:18px; margin:40px 0 4px; }}
.sub {{ color:var(--muted); margin:0 0 14px; }}
.meta {{ display:flex; flex-wrap:wrap; gap:8px 22px; color:var(--muted); font-size:14px; }}
.meta b {{ color:var(--text); font-weight:600; }}
.note {{ margin-top:10px; padding:8px 12px; border-left:3px solid var(--accent); background:var(--accent-soft);
  font-size:14px; border-radius:0 6px 6px 0; }}
.cards {{ display:grid; grid-template-columns:1fr 1.45fr 1fr 1.45fr 1fr; gap:14px; margin-top:22px; }}
.card {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:16px 16px 14px;
  display:flex; flex-direction:column; gap:6px; }}
.card.primary {{ border:2px solid var(--accent); box-shadow:0 0 0 4px var(--accent-soft); }}
.tagline {{ font-size:11px; font-weight:600; color:var(--accent); text-transform:uppercase; letter-spacing:.06em; min-height:14px; }}
.card .title {{ font-size:16px; margin:0; line-height:1.25; }}
.desc {{ font-size:13px; color:var(--muted); margin-bottom:4px; }}
.big {{ font-size:34px; font-weight:700; line-height:1.1; font-variant-numeric:tabular-nums; }}
.card.primary .big {{ font-size:42px; }}
.card.primary .big.tight {{ font-size:32px; white-space:nowrap; }}
.unit {{ font-size:15px; font-weight:500; color:var(--muted); margin-left:6px; white-space:nowrap; display:inline-block; }}
.reading {{ font-size:13.5px; margin:0; }}
.spark {{ width:100%; max-width:260px; height:auto; }}
.panelbox {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:16px; }}
.controls {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin:10px 0; font-size:14px; }}
select, input {{ font:inherit; padding:5px 8px; border:1px solid var(--line); border-radius:6px;
  background:var(--surface); color:var(--text); }}
.tablewrap {{ max-height:560px; overflow:auto; border:1px solid var(--line); border-radius:10px; background:var(--surface); }}
table {{ border-collapse:collapse; width:100%; font-size:14px; }}
th, td {{ padding:7px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }}
th {{ position:sticky; top:0; background:var(--surface); font-weight:600; z-index:1; }}
#prs th {{ cursor:pointer; user-select:none; }}
#prs th:hover {{ color:var(--accent); }}
th .arrow {{ color:var(--accent); font-size:11px; margin-left:3px; }}
td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
tbody tr:hover {{ background:var(--accent-soft); }}
.cards > *, .charts > * {{ min-width:0; }}
.panelbox {{ overflow-x:auto; }}
.meta span {{ overflow-wrap:anywhere; }}
.charts {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-top:12px; }}
.charts svg {{ width:100%; height:auto; display:block; }}
.charts h3 {{ margin:0 0 2px; font-size:15px; }}
.caption {{ font-size:13px; color:var(--muted); margin:8px 0 0; }}
.legend {{ display:flex; gap:14px; font-size:13px; color:var(--muted); margin:6px 0 4px; flex-wrap:wrap; }}
.legend i {{ display:inline-block; width:11px; height:11px; border-radius:2px; margin-right:5px; vertical-align:-1px; }}
svg .grid {{ stroke:var(--grid); stroke-width:1; }}
svg .ax {{ fill:var(--muted); font-size:11px; }}
svg .ax.dim {{ font-size:10px; opacity:.8; }}
svg .val {{ fill:var(--text); font-size:11.5px; font-weight:600; }}
svg .val.sm, svg .ax.sm {{ font-size:10px; }}
svg .panel {{ fill:var(--text); font-size:12.5px; font-weight:600; }}
svg .bar-key {{ fill:var(--accent); }}
svg .bar-med {{ fill:var(--bar-med); }}
svg .bar-mean {{ fill:var(--bar-mean); }}
.minitable {{ margin-top:10px; font-size:13px; }}
.minitable th, .minitable td {{ padding:4px 8px; }}
footer {{ margin-top:44px; padding-top:18px; border-top:1px solid var(--line); font-size:13.5px; color:var(--muted); }}
footer h3 {{ color:var(--text); font-size:14px; margin:14px 0 4px; }}
footer ul {{ margin:4px 0; padding-left:20px; }}
footer b {{ color:var(--text); }}
@media (max-width:1100px) {{ .cards {{ grid-template-columns:repeat(2, 1fr); }} }}
@media (max-width:820px) {{ .charts {{ grid-template-columns:1fr; }} }}
@media (max-width:560px) {{ .cards {{ grid-template-columns:1fr; }} h1 {{ font-size:22px; }} }}
@media print {{ .tablewrap {{ max-height:none; overflow:visible; }} .controls {{ display:none; }} }}
</style>
</head>
<body>
<main>

<header>
  <h1>{t[h1]}</h1>
  <p class="sub">{t[sub]}</p>
  <div class="meta">
    <span>{t[meta_window]}</span>
    <span>{t[meta_repos]}</span>
    <span>{t[meta_prs]}</span>
  </div>
  <div class="note">{t[note]}</div>
</header>

<section class="cards" aria-label="{t[cards_aria]}">
{cards}
</section>

<h2>{t[table_h2]}</h2>
<p class="sub">{t[table_sub]}</p>
<div class="controls">
  <label>{t[filter_label]} <select id="repo"><option value="">{t[filter_all]}</option>{repo_options}</select></label>
  <span id="count"></span>
</div>
<div class="tablewrap">
<table id="prs">
  <thead><tr>
    <th data-k="repo">{t[th_repo]}</th><th data-k="n" class="num">{t[th_n]}</th><th data-k="author">{t[th_author]}</th>
    <th data-k="approver">{t[th_approver]}</th><th data-k="rounds" class="num">{t[th_rounds]}</th>
    <th data-k="hours" class="num">{t[th_hours]}</th>
    <th data-k="merge_s" class="num">{t[th_merge]}</th><th data-k="lines" class="num">{t[th_lines]}</th>
    <th data-k="findings" class="num">{t[th_findings]}</th>
  </tr></thead>
  <tbody></tbody>
</table>
</div>
<noscript><p class="caption">{t[noscript]}</p></noscript>

<h2>{t[charts_h2]}</h2>
<div class="charts">
  <div class="panelbox">
    <h3>{t[weekly_h3]}</h3>
    <div class="legend">{weekly_legend}</div>
    {weekly_svg}
    <p class="caption">{t[weekly_caption]}</p>
  </div>
  <div class="panelbox">
    <h3>{t[bucket_h3]}</h3>
    <div class="legend"><span><i style="background:var(--accent)"></i>{t[legend_rate]}</span>
      <span><i style="background:var(--bar-med)"></i>{t[legend_med]}</span>
      <span><i style="background:var(--bar-mean)"></i>{t[legend_mean]}</span></div>
    {bucket_svg}
    <table class="minitable">
      <thead><tr><th>{t[mt_lines]}</th><th class="num">{t[mt_prs]}</th><th class="num">{t[mt_cf]}</th>
      <th class="num">{t[mt_med]}</th><th class="num">{t[mt_mean]}</th></tr></thead>
      <tbody>{bucket_rows}</tbody>
    </table>
    <p class="caption">{t[bucket_caption]}</p>
  </div>
</div>

<footer>
{t[footer]}
</footer>

</main>
<script>
const DATA = {data_json};
const cols = ["repo","n","author","approver","rounds","hours","merge_s","lines","findings"];
const numeric = new Set(["n","rounds","hours","merge_s","lines","findings"]);
let sortKey = "repo", sortDir = 1;
const tbody = document.querySelector("#prs tbody");
const repoSel = document.getElementById("repo");
const count = document.getElementById("count");
const esc = s => String(s).replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c]));
const LOC = "{js_locale}";
const fmt = (k, v) => v === null ? "–" : k === "lines" || k === "merge_s" ? v.toLocaleString(LOC)
  : k === "hours" ? v.toLocaleString(LOC, {{minimumFractionDigits: 1, maximumFractionDigits: 1}}) : v;
function render() {{
  const repo = repoSel.value;
  const rows = DATA.filter(r => !repo || r.repo === repo).sort((a, b) => {{
    const x = a[sortKey], y = b[sortKey];
    const c = numeric.has(sortKey) ? x - y : String(x).localeCompare(String(y));
    return (c || a.repo.localeCompare(b.repo) || a.n - b.n) * sortDir;
  }});
  tbody.innerHTML = rows.map(r => "<tr>" + cols.map(k => {{
    if (k === "n") return `<td class="num"><a href="${{esc(r.url)}}" target="_blank" rel="noopener">#${{r.n}}</a></td>`;
    return `<td${{numeric.has(k) ? ' class="num"' : ""}}>${{esc(fmt(k, r[k]))}}</td>`;
  }}).join("") + "</tr>").join("");
  count.textContent = {count_js};
  document.querySelectorAll("#prs th").forEach(th => {{
    th.querySelector(".arrow")?.remove();
    if (th.dataset.k === sortKey) th.insertAdjacentHTML("beforeend", `<span class="arrow">${{sortDir > 0 ? "▲" : "▼"}}</span>`);
    th.setAttribute("aria-sort", th.dataset.k === sortKey ? (sortDir > 0 ? "ascending" : "descending") : "none");
  }});
}}
document.querySelectorAll("#prs th").forEach(th => th.addEventListener("click", () => {{
  const k = th.dataset.k;
  if (k === sortKey) sortDir = -sortDir; else {{ sortKey = k; sortDir = numeric.has(k) ? -1 : 1; }}
  render();
}}));
repoSel.addEventListener("change", render);
render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
