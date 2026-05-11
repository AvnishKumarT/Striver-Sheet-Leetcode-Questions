# PROJECT_CONTEXT.md

> **Purpose**: a single source of truth that any new chat / Claude session can
> read first to instantly understand the project. Keep prompts short by
> referencing this file (e.g. "see PROJECT_CONTEXT.md §3"). Update sections
> when the project evolves.

---

## 1. Project Goal (One-Liner)

Scrape every problem from the **Striver A2Z DSA Sheet**
(`https://takeuforward.org/dsa/strivers-a2z-sheet-learn-dsa-a-to-z`), persist
the structured data as JSON, generate a per-problem C++ folder skeleton, and
track local solving progress.

Lives under `CPP_Practice/`, so the long-term loop is: **scrape → organise →
solve in C++ → track**.

---

## 2. Repository Layout

```
striver-a2z-sheet-questions/
├── .venv/                       Python 3.13 venv (Playwright 1.57.0)
├── scrape_takeuforward.py       Playwright scraper → problems.json
├── enrich_practice_urls.py      Pass 1: fill missing practice_url via LeetCode fuzzy match
├── enrich_from_codolio.py       Pass 2: fill remaining via Codolio community mapping
├── enrich_practice_urls_semantic.py  Pass 4 (LLM judge, see §3): semantic problem-statement matching
├── enrich_from_curated.py       Pass 5: apply hand-curated Striver → LC/GFG mappings
├── curated_mappings.json        The mappings, hand-verified and URL-validated
├── _striver_descriptions/       Cached article texts (gitignored)
├── _lc_descriptions/            Cached LC problem statements (gitignored)
├── _llm_verdicts.json           Cached LLM judgements per (problem_id, candidate_set, model)
├── .env                         GROQ_API_KEY / GEMINI_API_KEY (gitignored)
├── .env.example                 Template for the above
├── generate_skeleton.py         problems.json → solutions/ tree (C++ stubs)
├── generate_html.py             problems.json → problems.html (browseable list)
├── progress.py                  CLI: mark / show / list / stats — writes progress.json
├── problems.json                Latest scrape output (18 steps, 62 lectures, 474 problems)
├── problems.html                Self-contained HTML view (search + difficulty filters)
├── _leetcode_cache.json         Cached LeetCode catalogue (3163 free problems)
├── _codolio_sheet.json          Cached Codolio Striver A2Z mapping (455 entries)
├── progress.json                Per-problem status + notes (id-keyed)
├── solutions/                   Generated C++ folder tree (step → lecture → problem)
├── requirements.txt             playwright==1.57.0
├── README.md                    User-facing setup + usage
└── PROJECT_CONTEXT.md           ← this file
```

Not a git repo (intentional, current state).

---

## 3. How the Code Works (file-by-file)

### `scrape_takeuforward.py`

End-to-end flow:

1. Launch headless Chromium (or `--headed`) with a real desktop user-agent and
   1440×900 viewport.
2. `page.goto(...)` with `wait_until="networkidle"`.
3. **Dismiss the "Session expired" Radix dialog** by clicking the
   "Continue without login" button (`_dismiss_session_modal`).
4. **Expand every step accordion** — 18 elements matching
   `[data-slot='accordion-trigger']` (`_expand_all_steps`).
5. Belt-and-braces: if any `.tuf-subrow` panel has 0 rendered rows, click its
   button to force-render (`_expand_all_subrows_if_needed`).
6. **Extract everything in a single `page.evaluate(PAGE_EXTRACTOR_JS)` call** —
   one DOM walk in JS, no per-element Playwright roundtrips. Returns the full
   structured list of steps → lectures → problems.
7. Build `{scraped_at, source, totals, steps}` dict.
8. Sanity-check via `_verify()`: 18 steps, 400 ≤ total problems ≤ 500, no
   double-prepended URLs.
9. **Incremental merge** via `_merge_with_previous()`: if the new payload
   (minus `scraped_at`) equals the old payload, keep the old `scraped_at` so
   the file stays byte-identical.
10. Write JSON.

Retries: `_scrape_with_retries()` runs up to `--retries + 1` attempts with
1s/2s/4s/… exponential backoff.

CLI flags: `--output`, `--headed`, `--step N`, `--retries N`.

### `PAGE_EXTRACTOR_JS` (inside `scrape_takeuforward.py`)

JS function executed in the page context. Per `<tr>` row it pulls:

| Field                | Source                                                                       |
|----------------------|------------------------------------------------------------------------------|
| `id`                 | integer `id` on the status `<input type="checkbox">` in cell 0               |
| `slug`               | last path segment of `plus_problem_url`, else `slugify(title)`               |
| `title`              | text of the first `<a>` in cell 1                                            |
| `difficulty`         | regex on the `difficulty-badge--{easy\|medium\|hard}` class in cell 8        |
| `article_url`        | href of the PostLink `<a>` (cell 4); falls back to the title anchor in cell 1 |
| `youtube_url`        | href of the YouTube `<a>` in cell 4 (matched by URL or `alt='YouTube'`)      |
| `plus_problem_url`   | href of the first `<a>` in cell 2 ("Solve")                                  |
| `plus_editorial_url` | href of the first `<a>` in cell 3 ("Editorial")                              |
| `practice_url`       | href of the first `<a>` in cell 5 (external LeetCode/GFG/CN), else `null`    |

### `generate_skeleton.py`

Walks `problems.json` and ensures `solutions/step_NN_<slug>/lecture_NN_<slug>/NNN_<slug>/`
exists for every problem. Writes:

- `solution.cpp` — created only if missing (your work is never overwritten,
  unless you pass `--force-cpp`).
- `README.md` — rewritten every run with the latest article/YouTube/practice
  links so they stay fresh.

CLI flags: `--problems`, `--root`, `--force-cpp`.

### `enrich_practice_urls.py`

Fixes a source-data limitation: the takeuforward.org Practice column is `---`
(empty) for 212 of 474 problems. This script fetches the full LeetCode
catalogue once (cached to `_leetcode_cache.json`) and fuzzy-matches Striver
titles against it. To avoid the false-positive class that bit us once
(`Subsets I → Subsets II`, `Word Break → Word Break II`), matching:

- preserves trailing numeric/roman suffixes (`I`, `II`, `2`...) in the key
- requires the suffix to match (treats `X` and `X I` as compatible variant 1)
- requires LC difficulty to be within one level of Striver difficulty
- uses threshold 0.93 on `difflib.SequenceMatcher` for fuzzy fallback

Idempotent — re-running clears any prior `leetcode-*` source before re-matching.
Sets `practice_url`, `practice_url_source` (`takeuforward` / `leetcode-exact` /
`leetcode-fuzzy`), and `practice_url_confidence`.

Current coverage after this pass: 262 scraped + 11 exact + 1 fuzzy = **274 / 474**
with a LeetCode URL.

### `enrich_from_codolio.py`

Pass 2 of practice-URL enrichment. Uses Codolio's public API
(`https://node.codolio.com/api/question-tracker/v2/sheet/get-sheet-data-by-slug/strivers-a2z-dsa-sheet`)
which exposes a community-maintained Striver A2Z mapping with 455 entries
across platforms (leetcode 274, tuf 172, hackerrank 5, interviewbit 3, spoj 1).

Filters:

- Only fills problems that have no `practice_url` after the LeetCode pass.
- **Skips** Codolio's `tuf` platform (we explicitly don't want takeuforward
  URLs as practice links — that's what the user pushed back on).
- **Skips** URLs containing `/contests/` (private HackerRank contest URLs).
- Default: exact title match only. Pass `--threshold 0.85` to enable fuzzy.

Cached to `_codolio_sheet.json`; `--refresh-cache` re-fetches.

Final coverage after both passes: **289 / 474 (61%)** with an external
practice URL. Breakdown by host: LeetCode 284, HackerRank 2, InterviewBit 2,
SPOJ 1. The 185 still-unlinked problems are mostly: 24 theory rows, 22
Step-1 "Pattern N" prints, and ~140 concept/intro problems that only exist
on GFG. GFG has no usable public API and no Codolio-mapped URL — see §6 N7.

### `enrich_practice_urls_semantic.py`  (Stage 4 — LLM judge)

For each problem still without `practice_url`, this script does:

1. Fetches the Striver article body via Playwright (cached to disk).
2. Picks the **top-5 LC candidates** by title fuzzy similarity.
3. Fetches each candidate's LC problem statement via GraphQL (cached).
4. Sends **one batched LLM call**: "given this Striver problem and these 5 LC
   candidates, pick the one that asks the SAME question (same input, same
   output, same algorithmic logic) or say none — return JSON
   `{slug, confidence, reason}`."
5. If `confidence ≥ threshold` AND the slug is in the candidate set AND the
   LC difficulty is within one level of the Striver difficulty, writes
   `practice_url`, `practice_url_source = "leetcode-semantic"`,
   `practice_url_confidence`, `practice_url_reason`.

**Calibration first** (`--calibrate`): 30 known-correct + 30 random-mismatch
pairs measure the model's confidence distribution. The script auto-picks the
threshold as `max(min_threshold=0.97, max_negative_leak + 0.001)` — i.e.
zero known-bad-leakage by construction.

**LLM provider auto-detection**: routes to Groq (OpenAI-compatible) if model
name starts with `llama-` etc., or Gemini (generateContent) if it starts
with `gemini-`. Default is whichever key is set in the env (`.env`):
prefers Gemini when both are present.

**Quality observed across 3 models in calibration:**

| Model | Verdict |
|---|---|
| `llama-3.3-70b-versatile` (Groq) | Excellent. 26/30 positives matched at 1.0; 0/30 negative leaks. **Strict and accurate.** |
| `llama-3.1-8b-instant` (Groq) | **DO NOT USE.** Rubber-stamped every LC candidate at conf=1.0 in a 143-problem run; 103/143 were hallucinated false matches. The script's idempotent rollback was used to clean them up. |
| `gemini-2.5-flash-lite` (Gemini) | Good. 14/20 positives at conf 1.0 (~70% recall); 0 hallucinations observed. Similar profile to 70B. |

**Free-tier quota reality (as of May 2026):**

| Provider | Daily cap | Throughput | Notes |
|---|---|---|---|
| Groq `llama-3.3-70b-versatile` | 100 K tokens / day | 30 RPM | ~30 problems/day in our payload |
| Groq `llama-3.1-8b-instant` | 500 K tokens / day | 30 RPM | Capacity is high BUT model hallucinates — unusable |
| Gemini `gemini-2.5-flash-lite` | 20 requests / day | 15 RPM | Project-level free cap, shared across Gemini models |

To finish the remaining ~143 unmatched problems on these free tiers:
~3 days at 50 problems/day. The script's verdict cache + atomic
`_save_partial()` (every 10 problems) makes resuming clean: re-running
picks up where the last run was capped, never re-pays for prior verdicts.

CLI flags: `--calibrate`, `--threshold N`, `--min-threshold N`, `--top-k N`,
`--model NAME`, `--limit N`, `--dry-run`, `--headed`.

### `enrich_from_curated.py`  (Stage 5 — hand-curated)

After the LLM judge (Stage 4) hit its realistic ceiling — most "near match"
LC candidates are wrong at confidence 0.8 ("Second Largest Element" → LC
"Second Largest Digit in a String"), so lowering threshold is unsafe — we
fall back to a small JSON of hand-verified mappings in
`curated_mappings.json`.

The applier:

1. Reads each `{problem_id: {url, host, note}}` entry.
2. **Validates the URL is live and points to a real problem page**:
   - LC URLs are accepted if they match `^https://leetcode.com/problems/[a-z0-9-]+/?$` (LC's CF rules 403 generic UAs, so HTTP-fetch validation is unreliable; URL pattern is enough since LC slugs are stable).
   - GFG URLs are fetched and rejected if the body contains "Page Not Found"/"404", and required to contain `problem` + (`difficulty`|`solve`) to count as a real practice page.
   - Other hosts (SPOJ/HackerRank/InterviewBit) are accepted on HTTP 2xx.
3. Sets `practice_url_source = "curated"` and `practice_url_host` to the host name.
4. Idempotent — clears prior `curated` entries before re-deriving; never touches `takeuforward`/`leetcode-*`/`codolio-*`/`leetcode-semantic`.

Coverage delta from this stage: **+58 matches** (49 GFG + 4 LC + 5 others
that already had something but got promoted to a better URL). Final
coverage: **347 / 474 (73.2%)**.

CLI flags: `--dry-run`, `--no-validate`, `--problems`, `--mappings`.

### `generate_html.py`

Renders `problems.json` to a self-contained `problems.html`. **Each title
links to `practice_url` only** (no takeuforward.org fallback — the user
explicitly rejected that). When `practice_url` is null, the title renders
as plain italic text with a "no external link" tag so it's visually clear.
Client-side JS adds title search and All/Easy/Medium/Hard difficulty filters.

CLI: `--problems`, `--output`.

Link distribution as of the last scrape + all 5 enrichment passes:
**LeetCode 293, GFG 49, HackerRank 2, InterviewBit 2, SPOJ 1, no link 127**.
That's **347 / 474 (73.2%)** linked.

### `progress.py`

Pure-Python CLI tracker keyed by problem `id`. Subcommands:

- `mark <id> <status> [--note ...]` — set status to one of
  `not-started | attempted | solved`.
- `show <id>` — show one problem's metadata + status.
- `list [--status STATUS]` — table of all (or filtered) problems.
- `stats` — total counts and percentages.

State persisted to `progress.json` — kept **separate** from `problems.json`
so re-scraping never clobbers it.

---

## 4. Target Site Structure (Domain Knowledge)

- Next.js + Radix UI client-rendered single page.
- 18 steps → each a Radix Accordion item (`[data-slot='accordion-item']`).
- Inside each step's `accordion-content`: N lectures (Striver calls them
  "lectures" or "sub-steps"), each a `<div class="tuf-subrow">` with a
  toggle button `.tuf-subrow-btn`.
- The `<table><tbody>` for each lecture is **already in the DOM** after the
  step is expanded — the lecture panel just visually hides it via CSS. So we
  do NOT need to click each lecture button (a safety pass exists if some
  ever fail to render).
- Row layout has 9 `<td>` cells: Status, Problem, Plus, Plus Editorial,
  Resource (PostLink + YouTube), Practice (external), Note, Revision,
  Difficulty.
- A "Session expired — Continue without login" Radix dialog blocks all
  clicks on first load and must be dismissed.

---

## 5. What Works ✅

- 18/18 steps extracted, 62 lectures, 474 problems.
- All problems have `id`, `slug`, `title`, `difficulty`.
- 262/474 have external `practice_url` (LeetCode / GFG / Coding Ninjas).
- Incremental re-runs preserve `scraped_at` when content is unchanged.
- CLI flags, retries with backoff, single-step filtering.
- Skeleton generator produces the `solutions/` tree without overwriting work.
- Progress tracker round-trips correctly.
- All 13 bugs from the previous version of this doc are fixed.

## 6. What's Pending / Nice-to-Have

| # | Item | Notes |
|---|------|-------|
| N1 | Convert to a real git repo (`git init`) | Would enable normal version control + change diffs. |
| N2 | Add unit tests (e.g. `pytest` against a fixture HTML) | Currently we only sanity-check totals at runtime. |
| N3 | Auto-detect `playwright install chromium` not run | Surface a friendly error instead of Playwright's default. |
| N4 | `progress.py` import/export to/from CSV | If user wants to track outside this folder. |
| N5 | Pretty HTML report of progress (per step, per difficulty) | Optional dashboard. |
| N6 | Tag-based filtering (`--tag dp`, `--tag binary-search`) | Requires per-problem tagging, which the source page doesn't expose; would need manual annotation. |
| N7 | Add GFG enrichment for the ~150 still-unlinked algorithmic problems | No GFG public API found (sitemap returns 403, problem-list API returns 404). Options: (a) build URL from slugified title and validate by fetching `<title>` of the page; (b) ingest a hand-curated mapping from a community Striver→GFG repo. Both have accuracy trade-offs. Open question for user. |

---

## 7. Mapping — "Where Do I Look For X?" (token-saving lookup)

| If user asks about… | Read this first |
|---------------------|-----------------|
| Scraper logic, DOM selectors | `scrape_takeuforward.py` — `PAGE_EXTRACTOR_JS` and `scrape()` |
| Output schema | This file §3 (`PAGE_EXTRACTOR_JS` table) or `README.md` "Output schema" |
| Folder skeleton conventions | `generate_skeleton.py` — `_step_dir_name`, `_lecture_dir_name`, `_problem_dir_name` |
| HTML viewer / link priority | `generate_html.py` — `_primary_url`, `PAGE_TEMPLATE` |
| Progress state shape | `progress.py` — module docstring |
| Setup / how to run | `README.md` |
| Dependencies | `requirements.txt` (just `playwright==1.57.0`) |
| Site quirks (modal, accordions) | This file §4 |
| What's done vs not | This file §5–§6 |

---

## 8. Conventions for Future Edits

- Keep each top-level tool a **single file** until complexity demands a split.
- Default to `print(...)` for logging.
- Do not add error handling for cases that can't happen (Python-side).
- Do not create new `.md` files unless explicitly requested.
- When the scraper schema or selectors change, update §3 and §4 of this file
  in the **same edit** as the code change.
- Force UTF-8 on stdout for any new CLI (Windows cp1252 chokes on Unicode).
- Never overwrite `solution.cpp` files implicitly — those are user work.
- Keep `problems.json` and `progress.json` strictly separate (different
  lifecycles, different ownership).

---

## 9. Quick Commands

```powershell
# Activate venv (PowerShell)
.\.venv\Scripts\Activate.ps1

# One-time per machine (Chromium for Playwright)
python -m playwright install chromium

# --- The full pipeline (run in order after the first scrape) ---
python scrape_takeuforward.py             # 1. scrape    → problems.json
python enrich_practice_urls.py            # 2. LC fuzzy match  (free, fast, deterministic)
python enrich_from_codolio.py             # 3. Codolio community map  (free, fast, deterministic)
python enrich_practice_urls_semantic.py --calibrate            # 4a. LLM calibration  (uses API quota)
python enrich_practice_urls_semantic.py --threshold 0.97       # 4b. LLM semantic match  (uses API quota)
python enrich_from_curated.py             # 5. apply hand-curated mappings (free, fast)
python generate_html.py                   # 6. → problems.html
python generate_skeleton.py               # 7. → solutions/ folder tree (C++ stubs)

# Track progress
python progress.py stats
python progress.py mark 425 solved --note "..."
python progress.py list --status solved
```

---

_Last updated: 2026-05-11_
