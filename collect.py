#!/usr/bin/env python3
"""Raw GitHub PR review data collector (CMMI L3 Peer Review evidence).

Raw collection phase ONLY: every API response body is written to disk byte-for-byte
as received. No parsing into derived files, no metrics.

Layout
  raw/piaddys/{repo}/pulls/p001.json            /pulls list pages (updated desc, early-stop)
  raw/piaddys/{repo}/pulls_comments/p001.json   /pulls/comments?since=cutoff
  raw/piaddys/{repo}/issues_comments/p001.json  /issues/comments?since=cutoff
  raw/piaddys/{repo}/pr/{nnnn}/pull.json        /pulls/{n}
  raw/piaddys/{repo}/pr/{nnnn}/reviews/p001.json
  raw/piaddys/{repo}/pr/{nnnn}/timeline/p001.json
  Each body X.json has a sidecar X.json.meta.json (request URL, status, fetched_at,
  selected response headers). Error responses (404/410/451) get only the sidecar.

  raw/_meta/inventory.json           tier for every repo in inventory.tsv
  raw/_meta/checkpoints/{repo}.json  written when a repo finishes
  raw/_meta/sample_manifest.json     cutoff, repo list, per-repo PR counts, API calls
  raw/_meta/teams.json               (--teams) org teams + members; pages in raw/_meta/teams/
  logs/collect.log                   one line per repo
"""
from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw"
META = RAW / "_meta"
CHECKPOINTS = META / "checkpoints"
LOG_FILE = ROOT / "logs" / "collect.log"

API = "https://api.github.com"
API_VERSION = "2022-11-28"
PER_PAGE = 100
MAX_CONCURRENCY = 6
RATE_LIMIT_FLOOR = 50
EMPTY_STATUSES = {404, 410, 451}
MAX_RETRIES = 6

SAMPLE_REPOS = [
    "ddys-portal", "ddys-application", "ddys-bpmn-engine", "application-model",
    "ddys-notification", "ddys-report", "definition-model", "ddys-definition",
    "notification-model", "ddys-task-assignment",
]

# Evaluated in order; first match wins.
TIER_PATTERNS = [
    ("kubernetes", "ddys-kubernetes-*"),
    ("model", "*-model"),
    ("common", "*-common"),
    ("parent", "*-parent"),
    ("adapter", "*-adapter"),
]

KEPT_HEADERS = [
    "date", "etag", "last-modified", "link", "x-github-request-id",
    "x-github-api-version-selected", "x-ratelimit-remaining", "x-ratelimit-reset",
    "x-ratelimit-resource", "content-type",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def tier_for(repo: str) -> str:
    for tier, pattern in TIER_PATTERNS:
        if fnmatch.fnmatchcase(repo, pattern):
            return tier
    return "core"


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_json(path: Path, obj) -> None:
    atomic_write(path, (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode())


def log(line: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamped = f"{iso(utcnow())} {line}"
    with open(LOG_FILE, "a") as f:
        f.write(stamped + "\n")
    print(stamped, flush=True)


def next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        url, *params = part.split(";")
        if any(p.strip() == 'rel="next"' for p in params):
            return url.strip().strip("<>")
    return None


class Client:
    """httpx wrapper: concurrency cap, rate-limit pacing, retries, call accounting."""

    def __init__(self, token: str):
        self.http = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "piaddys-peer-review-collector",
            },
            timeout=httpx.Timeout(60.0),
        )
        self.sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self.pause_lock = asyncio.Lock()
        self.calls = 0
        self.remaining: int | None = None
        self.reset_at: int | None = None

    async def close(self):
        await self.http.aclose()

    async def _respect_rate_limit(self):
        async with self.pause_lock:
            if self.remaining is not None and self.remaining < RATE_LIMIT_FLOOR and self.reset_at:
                wait = max(0, self.reset_at - time.time()) + 5
                log(f"RATELIMIT remaining={self.remaining} sleeping {wait:.0f}s until reset")
                await asyncio.sleep(wait)
                self.remaining = None

    def _track(self, r: httpx.Response):
        if "x-ratelimit-remaining" in r.headers:
            self.remaining = int(r.headers["x-ratelimit-remaining"])
        if "x-ratelimit-reset" in r.headers:
            self.reset_at = int(r.headers["x-ratelimit-reset"])

    async def get(self, url: str) -> httpx.Response:
        delay = 5.0
        for attempt in range(1, MAX_RETRIES + 1):
            await self._respect_rate_limit()
            async with self.sem:
                try:
                    r = await self.http.get(url)
                except httpx.TransportError as e:
                    self.calls += 1
                    log(f"WARN transport error {type(e).__name__} {url} attempt={attempt}")
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
            self.calls += 1
            self._track(r)
            if r.status_code in (403, 429):
                retry_after = r.headers.get("retry-after")
                if retry_after:
                    wait = int(retry_after) + 1
                elif r.headers.get("x-ratelimit-remaining") == "0" and self.reset_at:
                    wait = max(0, self.reset_at - time.time()) + 5
                elif r.status_code == 429 or "rate limit" in r.text.lower():
                    wait = 60  # secondary rate limit without retry-after
                else:
                    return r  # genuine 403 (permissions) -> caller handles
                log(f"RATELIMIT {r.status_code} {url} sleeping {wait:.0f}s attempt={attempt}")
                await asyncio.sleep(wait)
                continue
            if r.status_code >= 500:
                log(f"WARN {r.status_code} {url} attempt={attempt}")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return r
        raise RuntimeError(f"giving up after {MAX_RETRIES} attempts: {url}")


class Fetcher:
    """Fetches one URL into one file; skips with zero network calls if already on disk."""

    def __init__(self, client: Client):
        self.client = client
        self.reused = 0

    async def fetch(self, url: str, path: Path) -> tuple[bytes | None, dict]:
        """Return (body or None if empty-status, meta). Reads from disk when present."""
        meta_path = path.with_name(path.name + ".meta.json")
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            if meta["status"] in EMPTY_STATUSES:
                self.reused += 1
                return None, meta
            if path.exists():
                self.reused += 1
                return path.read_bytes(), meta

        r = await self.client.get(url)
        meta = {
            "url": url,
            "status": r.status_code,
            "fetched_at": iso(utcnow()),
            "headers": {h: r.headers[h] for h in KEPT_HEADERS if h in r.headers},
        }
        if r.status_code in EMPTY_STATUSES:
            meta["error_body"] = r.text
            write_json(meta_path, meta)
            log(f"EMPTY {r.status_code} {url}")
            return None, meta
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} for {url}: {r.text[:300]}")
        # Sidecar first, body last: body presence implies a complete pair.
        write_json(meta_path, meta)
        atomic_write(path, r.content)
        return r.content, meta

    async def paginate(self, first_url: str, directory: Path, stop=None) -> list:
        """Follow rel=next. `stop(items)` -> True ends pagination after that page."""
        items_all: list = []
        url, page = first_url, 1
        while url:
            body, meta = await self.fetch(url, directory / f"p{page:03d}.json")
            if body is None:
                break
            items = json.loads(body)
            items_all.extend(items)
            if stop and stop(items):
                break
            url = next_link(meta["headers"].get("link"))
            page += 1
        return items_all


def build_inventory() -> list[dict]:
    rows = []
    for line in (META / "inventory.tsv").read_text().splitlines():
        if not line.strip():
            continue
        count, repo = line.split("\t")
        rows.append({"repo": repo, "tier": tier_for(repo), "inventory_tsv_count": int(count)})
    write_json(META / "inventory.json", {
        "generated_at": iso(utcnow()),
        "source": "raw/_meta/inventory.tsv",
        "tier_rules": [{"tier": t, "pattern": p} for t, p in TIER_PATTERNS]
                      + [{"tier": "core", "pattern": "(no match)"}],
        "repos": rows,
    })
    return rows


def load_or_init_manifest(org: str, days: int, repos: list[str]) -> dict:
    path = META / "sample_manifest.json"
    if path.exists():
        m = json.loads(path.read_text())
        if m["window_days"] != days:
            sys.exit(f"sample_manifest.json has window_days={m['window_days']} "
                     f"(cutoff {m['cutoff_utc']}); refusing to mix windows with --days {days}. "
                     f"Move raw/ aside to start a new sample.")
        for r in repos:
            if r not in m["repos"]:
                m["repos"].append(r)
        return m
    now = utcnow()
    return {
        "purpose": "CMMI L3 Peer Review evidence - raw PR review data SAMPLE",
        "org": org,
        "run_started_utc": iso(now),
        "window_days": days,
        "window_field": "updated_at",
        "cutoff_utc": iso(now - timedelta(days=days)),
        "api_version": API_VERSION,
        "repos": list(repos),
        "per_repo": {},
        "total_api_calls": 0,
    }


def save_manifest(m: dict) -> None:
    m["total_api_calls"] = sum(v.get("api_calls", 0) for v in m["per_repo"].values())
    m["updated_utc"] = iso(utcnow())
    write_json(META / "sample_manifest.json", m)


async def collect_repo(client: Client, org: str, repo: str, cutoff: str) -> dict:
    fetcher = Fetcher(client)
    calls_before = client.calls
    t0 = time.monotonic()
    base = RAW / org / repo
    cutoff_dt = parse_iso(cutoff)

    def page_crosses_cutoff(items):
        return not items or parse_iso(items[-1]["updated_at"]) < cutoff_dt

    pulls_q = urlencode({"state": "all", "sort": "updated", "direction": "desc", "per_page": PER_PAGE})
    since_q = urlencode({"since": cutoff, "per_page": PER_PAGE})
    pulls, _, _ = await asyncio.gather(
        fetcher.paginate(f"{API}/repos/{org}/{repo}/pulls?{pulls_q}", base / "pulls", page_crosses_cutoff),
        fetcher.paginate(f"{API}/repos/{org}/{repo}/pulls/comments?{since_q}", base / "pulls_comments"),
        fetcher.paginate(f"{API}/repos/{org}/{repo}/issues/comments?{since_q}", base / "issues_comments"),
    )
    in_window = sorted({p["number"] for p in pulls if parse_iso(p["updated_at"]) >= cutoff_dt})

    page_q = urlencode({"per_page": PER_PAGE})

    async def one_pr(n: int):
        d = base / "pr" / f"{n:04d}"
        await asyncio.gather(
            fetcher.fetch(f"{API}/repos/{org}/{repo}/pulls/{n}", d / "pull.json"),
            fetcher.paginate(f"{API}/repos/{org}/{repo}/pulls/{n}/reviews?{page_q}", d / "reviews"),
            fetcher.paginate(f"{API}/repos/{org}/{repo}/issues/{n}/timeline?{page_q}", d / "timeline"),
        )

    await asyncio.gather(*(one_pr(n) for n in in_window))

    result = {
        "pr_count": len(in_window),
        "pr_numbers": in_window,
        "api_calls": client.calls - calls_before,
        "files_reused_from_disk": fetcher.reused,
        "elapsed_s": round(time.monotonic() - t0, 1),
        "completed_utc": iso(utcnow()),
        "ratelimit_remaining": client.remaining,
        "tier": tier_for(repo),
    }
    return result


async def collect_teams(client: Client, org: str) -> None:
    """Teams + members. Raw pages under raw/_meta/teams/, combined verbatim into teams.json."""
    fetcher = Fetcher(client)
    base = META / "teams"
    page_q = urlencode({"per_page": PER_PAGE})
    teams = await fetcher.paginate(f"{API}/orgs/{org}/teams?{page_q}", base / "teams")
    members = await asyncio.gather(*(
        fetcher.paginate(f"{API}/orgs/{org}/teams/{t['slug']}/members?{page_q}", base / "members" / t["slug"])
        for t in teams))
    fetched = sorted(json.loads(f.read_text())["fetched_at"] for f in base.rglob("*.meta.json"))
    write_json(META / "teams.json", {
        "org": org,
        "fetched_at_range": [fetched[0], fetched[-1]] if fetched else None,
        "note": "Current membership at fetch time, not historical. Objects are verbatim API items; "
                "source pages in raw/_meta/teams/.",
        "teams": teams,
        "members": {t["slug"]: m for t, m in zip(teams, members)},
    })
    log(f"teams={len(teams)} members={sum(len(m) for m in members)} api_calls={client.calls} "
        f"reused={fetcher.reused} ratelimit_remaining={client.remaining}")


async def main_async(args) -> int:
    token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    if args.teams:
        client = Client(token)
        try:
            await collect_teams(client, args.org)
        finally:
            await client.close()
        return 0
    build_inventory()

    repos = args.only or SAMPLE_REPOS
    manifest = load_or_init_manifest(args.org, args.days, SAMPLE_REPOS)
    cutoff = manifest["cutoff_utc"]
    save_manifest(manifest)
    log(f"RUN start org={args.org} days={args.days} cutoff={cutoff} repos={','.join(repos)}")

    client = Client(token)
    try:
        for repo in repos:
            ckpt = CHECKPOINTS / f"{repo}.json"
            if ckpt.exists():
                log(f"repo={repo} SKIP (checkpoint exists)")
                continue
            res = await collect_repo(client, args.org, repo, cutoff)
            write_json(ckpt, {"repo": repo, "cutoff_utc": cutoff, **res})
            prev_calls = manifest["per_repo"].get(repo, {}).get("api_calls", 0)
            manifest["per_repo"][repo] = {k: res[k] for k in
                                          ("pr_count", "files_reused_from_disk", "completed_utc")}
            manifest["per_repo"][repo]["api_calls"] = prev_calls + res["api_calls"]  # cumulative across resumes
            save_manifest(manifest)
            log(f"repo={repo} prs={res['pr_count']} api_calls={res['api_calls']} "
                f"reused={res['files_reused_from_disk']} elapsed={res['elapsed_s']}s "
                f"ratelimit_remaining={res['ratelimit_remaining']}")
    finally:
        await client.close()
    log(f"RUN end api_calls_this_run={client.calls}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30, help="window on PR updated_at (default 30)")
    ap.add_argument("--org", default="piaddys")
    ap.add_argument("--only", nargs="+", metavar="REPO",
                    help="collect only these repos (must be in the sample list)")
    ap.add_argument("--teams", action="store_true",
                    help="fetch org teams and members into raw/_meta/teams.json (no PR collection)")
    args = ap.parse_args()
    if args.only:
        bad = [r for r in args.only if r not in SAMPLE_REPOS]
        if bad:
            ap.error(f"not in sample scope: {bad}")
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        log("RUN interrupted (Ctrl-C); re-run to resume")
        return 130


if __name__ == "__main__":
    sys.exit(main())
