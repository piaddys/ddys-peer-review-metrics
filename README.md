# peer-review-metrics

Evidence collection and measurement of GitHub pull request reviews in the `piaddys` organization,
for the CMMI Level 3 Peer Review process area. The current data is a **sample**: 10 repositories,
a 30-day window on PR `updated_at`.

## Run it

Requirements: Python 3.13 with `httpx` (already in `.venv/`), and the GitHub CLI logged in
(`gh auth login`) with access to the org. The token is read from `gh auth token`.

```sh
.venv/bin/python run.py              # regenerate data/ and docs/ from the existing raw/
.venv/bin/python run.py --refresh    # archive raw/ to raw_archive/<UTC stamp>/, then collect a new sample
```

`run.py` prints one line per stage with its timing. It stops with a non-zero exit if a stage fails, or if a
stage's output is missing, empty or wasn't rewritten. Collection is skipped when `raw/` already
holds a complete sample.

The pipeline is deterministic: the same `raw/` always produces byte-identical files in `data/` and
`docs/`. If you need to rebuild from scratch, delete `data/` and the generated files in `docs/`, then run
`run.py`. Do not delete `docs/recon.md`, because it is written by hand (see below).

## Scripts

| Script | Reads | Writes | What it does |
|---|---|---|---|
| `collect.py` | GitHub REST API | `raw/`, `logs/collect.log` | Fetches PR lists, PR details, reviews, timelines and comments for the sample repos (`--days`, default 30). `--teams` fetches org teams and members. It resumes after an interruption and never re-fetches a file already on disk. |
| `normalize.py` | `raw/` | `data/prs.csv`, `data/reviews.csv`, `data/findings.csv` | Builds one row per PR, review and candidate finding. Applies the exclusion rules by writing an `excluded_reason` instead of dropping rows. |
| `metrics.py` | `data/*.csv` | `docs/metrics.md` | Computes the five metrics, overall and by repo and size bucket. Includes the PRs to verify by hand. |
| `dashboard.py` | `data/*.csv`, manifest | `docs/dashboard.html`, `docs/dashboard-tr.html` | Builds the self-contained offline dashboard. `--lang en` (default), `tr` or `all`; both languages come from one template and one data source. |
| `internal_notes.py` | `data/*.csv`, `raw/` | `docs/internal-notes.md` | Internal commentary: specific cases, approve-and-merge timing, reviewer concentration. It isn't linked from the dashboard. |
| `run.py` | — | — | Runs all of the above in order. |

The column definitions, exclusion rules and metric definitions are in each script's docstring.
They're also summarized in the dashboard footer.

## Folders

- **`raw/` is audit evidence. Do not edit it.** It holds GitHub API responses exactly as received, byte for
  byte. Each `X.json` has an `X.json.meta.json` sidecar with the request URL, HTTP status, fetch time and
  GitHub request id. `raw/_meta/` holds:
  - `inventory.tsv` and `repos.txt`: inputs; the list of all repos in the org.
  - `inventory.json`: the tier of every repo.
  - `sample_manifest.json`: the cutoff timestamp, the repo list, the PR count per repo and the total API calls. This makes the sample reproducible.
  - `checkpoints/`: one file per finished repo, used for resuming.
  - `teams.json` and `teams/`: team membership as of the fetch date. It isn't historical.

  To collect again, use `run.py --refresh`, which moves the old evidence to `raw_archive/` rather than
  overwriting it.
- **`data/`** holds the normalized CSVs. They're generated, so don't edit them.
- **`docs/`** holds the reports. `dashboard.html`, `dashboard-tr.html`, `metrics.md` and `internal-notes.md`
  are generated. `recon.md` is a hand-written reconnaissance report on the raw data. It records the state
  of the sample when it was written and isn't regenerated.
- **`logs/`** holds the collection log, with one line per repo.
