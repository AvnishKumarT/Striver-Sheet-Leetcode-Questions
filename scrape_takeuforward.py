"""
Scrape every problem from the Striver A2Z DSA Sheet on takeuforward.org.

Strategy
--------
The page is a Next.js SPA that hides its content behind:
    1. a "Session expired - Continue without login" Radix dialog, and
    2. 18 Radix accordion items (one per Step).

Each Step, when expanded, fully renders its lectures (as `tuf-subrow` items)
and the underlying problem tables in the DOM. The lecture panels are only
visually collapsed, so we do NOT need to click each lecture — the `<tr>`
rows are already queryable.

Output schema (see PROJECT_CONTEXT.md §3 / §6):

    {
      "scraped_at": "<ISO-8601>",
      "source": "<TARGET_URL>",
      "totals": {"steps": 18, "lectures": <n>, "problems": <n>},
      "steps": [
        {
          "step_no": 1,
          "step_title": "Learn the basics",
          "lectures": [
            {
              "lecture_no": 1,
              "lecture_title": "...",
              "problem_count_label": "0 / 9",
              "problems": [
                {
                  "id": 425,
                  "title": "Input Output",
                  "difficulty": "Easy",
                  "article_url": "...",
                  "youtube_url": "...",
                  "plus_problem_url": "...",
                  "plus_editorial_url": "...",
                  "practice_url": null
                }, ...
              ]
            }, ...
          ]
        }, ...
      ]
    }
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# Force UTF-8 on stdout/stderr so progress logs don't crash on Windows cp1252
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TARGET_URL = "https://takeuforward.org/dsa/strivers-a2z-sheet-learn-dsa-a-to-z"
OUTPUT_FILE = Path(__file__).parent / "problems.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Expected magnitudes for sanity-check assertions
EXPECTED_STEPS = 18
MIN_TOTAL_PROBLEMS = 400
MAX_TOTAL_PROBLEMS = 500


# JavaScript run inside the page to extract everything in one DOM walk.
# Returns a list of step dicts. Doing this in JS (vs. iterating Playwright
# locators from Python) is ~100x faster and eliminates per-element timeouts.
PAGE_EXTRACTOR_JS = r"""
() => {
  const absUrl = (href) => {
    if (!href) return null;
    if (href.startsWith('http://') || href.startsWith('https://')) return href;
    if (href.startsWith('/')) return 'https://takeuforward.org' + href;
    return href;
  };
  const textOf = (el) => (el ? (el.innerText || el.textContent || '').trim() : '');

  const slugify = (s) =>
    (s || '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/(^-|-$)/g, '');

  const extractRow = (tr) => {
    const tds = tr.querySelectorAll(':scope > td');
    if (tds.length < 2) return null;

    // id from the status checkbox
    const cb = tds[0].querySelector("input[type='checkbox']");
    const idRaw = cb ? cb.id : null;
    const id = idRaw && /^\d+$/.test(idRaw) ? parseInt(idRaw, 10) : null;

    // title + title anchor
    const titleA = tds[1].querySelector('a');
    const title = textOf(titleA) || textOf(tds[1]);
    if (!title) return null;
    let articleUrl = titleA ? absUrl(titleA.getAttribute('href')) : null;

    // Plus problem (cell 2)
    const plusA = tds[2] ? tds[2].querySelector('a') : null;
    const plusProblemUrl = plusA ? absUrl(plusA.getAttribute('href')) : null;

    // Plus editorial (cell 3)
    const plusEdA = tds[3] ? tds[3].querySelector('a') : null;
    const plusEditorialUrl = plusEdA ? absUrl(plusEdA.getAttribute('href')) : null;

    // Resource (cell 4): PostLink + YouTube
    let youtubeUrl = null;
    if (tds[4]) {
      const anchors = Array.from(tds[4].querySelectorAll('a'));
      for (const a of anchors) {
        const href = a.getAttribute('href') || '';
        const img = a.querySelector('img');
        const alt = img ? (img.getAttribute('alt') || '').toLowerCase() : '';
        if (href.includes('youtu') || alt === 'youtube') {
          youtubeUrl = absUrl(href);
        } else if (href) {
          // Prefer the PostLink article over the title's link
          articleUrl = absUrl(href) || articleUrl;
        }
      }
    }

    // Practice (cell 5)
    let practiceUrl = null;
    if (tds[5]) {
      const a = tds[5].querySelector('a');
      if (a) practiceUrl = absUrl(a.getAttribute('href'));
    }

    // Difficulty badge (cell 8, or anywhere in row as fallback)
    let difficulty = null;
    let badge = tds[8] ? tds[8].querySelector('.difficulty-badge') : null;
    if (!badge) badge = tr.querySelector('.difficulty-badge');
    if (badge) {
      const m = badge.className.match(/difficulty-badge--(easy|medium|hard)/i);
      if (m) difficulty = m[1].charAt(0).toUpperCase() + m[1].slice(1).toLowerCase();
    }

    // slug from the plus problem URL (preferred — site-canonical), else
    // fall back to slugifying the title.
    const plusPath = (() => {
      if (!plusProblemUrl) return null;
      try {
        const u = new URL(plusProblemUrl);
        const parts = u.pathname.split('/').filter(Boolean);
        return parts.length ? parts[parts.length - 1] : null;
      } catch (_) {
        return null;
      }
    })();
    const slug = plusPath || slugify(title);

    return {
      id,
      slug,
      title,
      difficulty,
      article_url: articleUrl,
      youtube_url: youtubeUrl,
      plus_problem_url: plusProblemUrl,
      plus_editorial_url: plusEditorialUrl,
      practice_url: practiceUrl,
    };
  };

  const extractLecture = (subrow, lectureNo) => {
    const btn = subrow.querySelector('.tuf-subrow-btn');
    const titleSpan = btn ? btn.querySelector('span') : null;
    const title = textOf(titleSpan);
    const countEl = btn ? btn.querySelector('.tuf-subrow-count') : null;
    const countLabel = countEl ? textOf(countEl) : null;
    const rows = Array.from(subrow.querySelectorAll('table tbody tr'));
    const problems = [];
    for (const tr of rows) {
      const p = extractRow(tr);
      if (p) problems.push(p);
    }
    return {
      lecture_no: lectureNo,
      lecture_title: title,
      problem_count_label: countLabel,
      problems,
    };
  };

  const steps = [];
  const stepItems = document.querySelectorAll("[data-slot='accordion-item']");
  let stepNo = 0;
  for (const item of stepItems) {
    stepNo += 1;
    const titleEl = item.querySelector('.tuf-accordion-title');
    const stepTitle = textOf(titleEl);
    const subrows = Array.from(item.querySelectorAll('.tuf-subrow'));
    const lectures = subrows.map((sr, i) => extractLecture(sr, i + 1));
    steps.push({ step_no: stepNo, step_title: stepTitle, lectures });
  }
  return steps;
};
"""


def _dismiss_session_modal(page: Page) -> None:
    """Click the 'Continue without login' button on the session-expired
    Radix dialog, if present. No-op if the dialog isn't shown."""
    try:
        btn = page.get_by_role("button", name="Continue without login")
        btn.wait_for(state="visible", timeout=8000)
        btn.click(timeout=5000)
        page.wait_for_timeout(800)
    except PlaywrightTimeoutError:
        pass


def _expand_all_steps(page: Page) -> int:
    """Click every Step accordion trigger so its content renders. Returns
    the number of steps successfully expanded."""
    triggers = page.locator("[data-slot='accordion-trigger']")
    count = triggers.count()
    expanded = 0
    for i in range(count):
        trig = triggers.nth(i)
        # Skip if already open
        state = trig.get_attribute("data-state")
        if state == "open":
            expanded += 1
            continue
        try:
            trig.scroll_into_view_if_needed(timeout=4000)
            trig.click(timeout=4000)
            expanded += 1
            page.wait_for_timeout(120)  # let Radix animate the open
        except PlaywrightTimeoutError as e:
            print(f"  ! could not expand step {i + 1}: {e}", file=sys.stderr)
    # Final small settle so all accordion-content blocks are populated
    page.wait_for_timeout(800)
    return expanded


def _expand_all_subrows_if_needed(page: Page) -> None:
    """Some subrow tables may render only on first open. Click any subrow
    button whose underlying panel has no `<tr>` rows yet."""
    subrow_btns = page.locator(".tuf-subrow-btn")
    n = subrow_btns.count()
    clicked = 0
    for i in range(n):
        btn = subrow_btns.nth(i)
        panel = btn.locator("xpath=following-sibling::div[contains(@class,'tuf-subrow-panel')][1]")
        try:
            row_count = panel.locator("tbody tr").count()
        except PlaywrightTimeoutError:
            row_count = 0
        if row_count == 0:
            try:
                btn.scroll_into_view_if_needed(timeout=2000)
                btn.click(timeout=2000)
                clicked += 1
                page.wait_for_timeout(80)
            except PlaywrightTimeoutError:
                pass
    if clicked:
        print(f"  · clicked {clicked} subrows to force-render rows")
        page.wait_for_timeout(600)


def scrape(playwright: Playwright, *, headless: bool = True) -> dict[str, Any]:
    browser = playwright.chromium.launch(headless=headless)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, user_agent=USER_AGENT)
    page = ctx.new_page()

    print(f"→ loading {TARGET_URL}")
    page.goto(TARGET_URL, wait_until="networkidle", timeout=90000)
    page.wait_for_timeout(1500)

    print("→ dismissing session-expired modal if present")
    _dismiss_session_modal(page)

    print("→ waiting for accordion to render")
    page.wait_for_selector("[data-slot='accordion-trigger']", timeout=30000)

    print("→ expanding all step accordions")
    expanded = _expand_all_steps(page)
    print(f"  · expanded {expanded} steps")

    # Belt-and-braces: ensure every lecture's subrow panel has rows in DOM
    print("→ verifying subrow panels are populated")
    _expand_all_subrows_if_needed(page)

    print("→ extracting steps / lectures / problems (in-page DOM walk)")
    steps_out: list[dict[str, Any]] = page.evaluate(PAGE_EXTRACTOR_JS)
    for step in steps_out:
        n_lec = len(step["lectures"])
        n_prob = sum(len(l["problems"]) for l in step["lectures"])
        print(
            f"  · step {step['step_no']:>2} {step['step_title'][:55]:<55}  "
            f"{n_lec:>2} lectures, {n_prob:>3} problems"
        )

    browser.close()

    total_lectures = sum(len(s["lectures"]) for s in steps_out)
    total_problems = sum(len(l["problems"]) for s in steps_out for l in s["lectures"])

    return {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source": TARGET_URL,
        "totals": {
            "steps": len(steps_out),
            "lectures": total_lectures,
            "problems": total_problems,
        },
        "steps": steps_out,
    }


def _verify(report: dict[str, Any], *, single_step: int | None = None) -> None:
    """Sanity-check the output. Raises AssertionError on failure.
    When `single_step` is set, only structural checks run."""
    totals = report["totals"]
    if single_step is None:
        assert totals["steps"] == EXPECTED_STEPS, (
            f"expected {EXPECTED_STEPS} steps, got {totals['steps']}"
        )
        assert MIN_TOTAL_PROBLEMS <= totals["problems"] <= MAX_TOTAL_PROBLEMS, (
            f"problem count {totals['problems']} outside expected range "
            f"[{MIN_TOTAL_PROBLEMS}, {MAX_TOTAL_PROBLEMS}]"
        )
    else:
        assert totals["steps"] == 1, f"--step requested but output has {totals['steps']} steps"
        assert totals["problems"] > 0, "selected step has 0 problems"
    blob = json.dumps(report)
    assert "takeuforward.orghttps" not in blob, "found double-prepended URL"


def _strip_timestamp(report: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `report` with `scraped_at` removed — used to compare
    content equality between runs."""
    return {k: v for k, v in report.items() if k != "scraped_at"}


def _merge_with_previous(new_report: dict[str, Any], output_path: Path) -> dict[str, Any]:
    """If the previous output exists and its content (excluding `scraped_at`)
    is byte-identical to the new content, keep the previous `scraped_at`. This
    makes repeated runs produce stable JSON when the site is unchanged."""
    if not output_path.exists():
        return new_report
    try:
        prev = json.loads(output_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return new_report
    if _strip_timestamp(prev) == _strip_timestamp(new_report):
        new_report["scraped_at"] = prev.get("scraped_at", new_report["scraped_at"])
        print("· content unchanged since last run — preserving previous scraped_at")
    return new_report


def _scrape_with_retries(*, headless: bool, retries: int) -> dict[str, Any]:
    """Run `scrape()` up to `retries + 1` times with exponential backoff."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with sync_playwright() as p:
                return scrape(p, headless=headless)
        except Exception as e:
            last_exc = e
            if attempt >= retries:
                break
            delay = 2 ** attempt
            print(
                f"! attempt {attempt + 1}/{retries + 1} failed: {e}; "
                f"retrying in {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _filter_to_step(report: dict[str, Any], step_no: int) -> dict[str, Any]:
    """Reduce the report to a single step. Recomputes totals."""
    keep = [s for s in report["steps"] if s["step_no"] == step_no]
    if not keep:
        raise SystemExit(f"step {step_no} not found in output (have 1..{len(report['steps'])})")
    filtered = dict(report)
    filtered["steps"] = keep
    filtered["totals"] = {
        "steps": len(keep),
        "lectures": sum(len(s["lectures"]) for s in keep),
        "problems": sum(len(l["problems"]) for s in keep for l in s["lectures"]),
    }
    return filtered


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scrape the Striver A2Z DSA sheet from takeuforward.org.",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_FILE,
        help=f"output JSON path (default: {OUTPUT_FILE.name})",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="run Chromium with a visible window (default: headless)",
    )
    p.add_argument(
        "--step",
        type=int,
        metavar="N",
        help="only emit step N (1..18) — the full page is still loaded",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=2,
        help="number of retries on failure (default: 2, so up to 3 attempts)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    report = _scrape_with_retries(headless=not args.headed, retries=args.retries)

    if args.step is not None:
        report = _filter_to_step(report, args.step)

    print(f"\n· totals: {report['totals']}")
    try:
        _verify(report, single_step=args.step)
        print("· verification passed")
    except AssertionError as e:
        print(f"! verification FAILED: {e}", file=sys.stderr)
        # Still write the output for inspection

    report = _merge_with_previous(report, args.output)

    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"· wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
