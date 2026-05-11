# striver-a2z-sheet-questions

A local toolkit to scrape, organise, and solve the **Striver A2Z DSA Sheet**
from [takeuforward.org](https://takeuforward.org/dsa/strivers-a2z-sheet-learn-dsa-a-to-z)
in C++.

> Full project context lives in [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md). Read
> that first if you want a deeper map of the codebase.

---

## What you get

- `scrape_takeuforward.py` — Playwright scraper that emits **`problems.json`**:
  18 steps, 62 lectures, **474 problems**, each with id, slug, title,
  difficulty, article URL, YouTube URL, Plus problem URL, Plus editorial URL,
  and external practice URL.
- `enrich_practice_urls.py` — pass 1 of practice-URL enrichment: fuzzy-match
  unfilled rows against the full LeetCode catalogue.
- `enrich_from_codolio.py` — pass 2: cross-reference the Codolio community
  Striver A2Z mapping to fill more URLs (LeetCode / HackerRank /
  InterviewBit / SPOJ). Skips takeuforward URLs — those aren't practice links.
- `generate_skeleton.py` — turns `problems.json` into a folder tree under
  `solutions/` with a `solution.cpp` stub and a `README.md` per problem.
- `generate_html.py` — renders `problems.json` to a self-contained
  `problems.html` viewer (search by title, filter by difficulty). Each title
  links **only** to its external practice URL; problems without one are
  shown as plain text with a "no external link" tag.
- `progress.py` — tiny CLI to track which problems you've solved, with notes
  and a per-problem timestamp, persisted to `progress.json`.

Final coverage: **289 / 474** problems have an external practice URL
(LeetCode 284, HackerRank 2, InterviewBit 2, SPOJ 1). The remaining 185 are
theory rows / step-1 fundamentals / GFG-only problems for which no public
mapping data exists.

`solution.cpp` files are **never overwritten** on re-runs (your work is safe).
Re-running the scraper preserves the previous `scraped_at` when nothing on
the source page changed, so the JSON stays byte-identical.

---

## Setup (Windows / PowerShell)

```powershell
# Create + activate venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install Python deps
pip install -r requirements.txt

# Install the Chromium build Playwright needs (one-time per machine)
python -m playwright install chromium
```

For bash/zsh on Linux/macOS, swap `Activate.ps1` for `source .venv/bin/activate`.

---

## Usage

### 1. Scrape the sheet

```powershell
python scrape_takeuforward.py
```

Useful flags:

```powershell
python scrape_takeuforward.py --headed              # watch Chromium do its thing
python scrape_takeuforward.py --step 3              # emit only step 3
python scrape_takeuforward.py --output custom.json  # write somewhere else
python scrape_takeuforward.py --retries 5           # up to 6 attempts
```

### 1b. Enrich practice URLs (LeetCode + Codolio community mapping)

```powershell
python enrich_practice_urls.py    # fills missing LC URLs via fuzzy match
python enrich_from_codolio.py     # fills more via Codolio's community-maintained mapping
```

Both are safe to re-run; both write back to `problems.json` in place.
`takeuforward.org` URLs are never used as practice links — that was an
explicit user requirement.

### 2. Generate the C++ folder skeleton

```powershell
python generate_skeleton.py
```

Produces, for example:

```
solutions/
  step_01_learn-the-basics/
    lecture_01_things-to-know-in-c-java-python-or-any-l/
      001_input-output/
        solution.cpp
        README.md
```

### 3. Render the browseable HTML view

```powershell
python generate_html.py
# then double-click problems.html
```

The page lists every problem grouped by step/lecture. Each title links to
its external practice URL (LeetCode mostly, plus HackerRank / InterviewBit /
SPOJ for a few) — **never** to takeuforward.org. Problems with no external
URL appear as plain italic text tagged "no external link". Search filters
by title; difficulty chips filter by Easy/Medium/Hard.

### 4. Track your progress

```powershell
python progress.py stats
python progress.py mark 425 solved --note "trivial cin/cout"
python progress.py show 425
python progress.py list --status solved
```

---

## Output schema

See [PROJECT_CONTEXT.md §3](PROJECT_CONTEXT.md) for the canonical schema
description. In short:

```json
{
  "scraped_at": "ISO-8601",
  "source": "https://takeuforward.org/...",
  "totals": { "steps": 18, "lectures": 62, "problems": 474 },
  "steps": [
    {
      "step_no": 1,
      "step_title": "Learn the basics",
      "lectures": [
        {
          "lecture_no": 1,
          "lecture_title": "Things to Know in C++/Java/Python or any language",
          "problem_count_label": "0 / 9",
          "problems": [
            {
              "id": 425,
              "slug": "input-output",
              "title": "Input Output",
              "difficulty": "Easy",
              "article_url": "https://takeuforward.org/c/c-basic-input-output/",
              "youtube_url": "https://youtu.be/EAR7De6Goz4?t=250",
              "plus_problem_url": "https://takeuforward.org/plus/dsa/problems/input-output?source=strivers-a2z-dsa-track",
              "plus_editorial_url": "https://takeuforward.org/plus/dsa/problems/input-output?tab=editorial&source=strivers-a2z-dsa-track",
              "practice_url": null
            }
          ]
        }
      ]
    }
  ]
}
```

---

## Known notes

- The takeuforward.org page renders behind a "Session expired" Radix dialog
  when you're not logged in. The scraper auto-dismisses it via the
  "Continue without login" button.
- Some problems share a slug on the source site (e.g. two "Cpp" problems in
  step 1). The folder generator disambiguates with a numeric prefix; the
  canonical `id` field in `problems.json` is always unique.
