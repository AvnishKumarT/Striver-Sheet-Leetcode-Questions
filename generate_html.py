"""
Generate a self-contained `problems.html` from `problems.json`.

The page lists every problem grouped by step → lecture. The title links to
the first available external URL in this priority order:

    practice_url (LeetCode / GFG / Coding Ninjas) → article_url → plus_problem_url

Client-side JS adds:
    - a search box that filters by title
    - difficulty chips (All / Easy / Medium / Hard)
    - collapsible step sections

No build step, no server. Double-click problems.html to open.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
DEFAULT_PROBLEMS = HERE / "problems.json"
DEFAULT_OUTPUT = HERE / "problems.html"


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Striver A2Z Problems</title>
<style>
  :root {{
    color-scheme: dark light;
    --bg: #0e1116;
    --panel: #161b22;
    --border: #2a313c;
    --text: #e6edf3;
    --muted: #8b949e;
    --link: #58a6ff;
    --link-hover: #79b8ff;
    --easy: #2ea043;
    --medium: #d29922;
    --hard: #f85149;
  }}
  @media (prefers-color-scheme: light) {{
    :root {{
      --bg: #ffffff;
      --panel: #f6f8fa;
      --border: #d0d7de;
      --text: #1f2328;
      --muted: #57606a;
      --link: #0969da;
      --link-hover: #0550ae;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 1.5rem;
    font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
  }}
  header {{
    max-width: 1100px;
    margin: 0 auto 1rem;
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    align-items: baseline;
  }}
  header h1 {{ margin: 0; font-size: 1.25rem; }}
  header .meta {{ color: var(--muted); font-size: 0.85rem; }}
  main {{ max-width: 1100px; margin: 0 auto; }}
  .controls {{
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 1rem;
    align-items: center;
  }}
  input[type="search"] {{
    flex: 1;
    min-width: 200px;
    padding: 0.5rem 0.75rem;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    font: inherit;
  }}
  .chip {{
    padding: 0.4rem 0.8rem;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 999px;
    cursor: pointer;
    color: var(--text);
    font: inherit;
  }}
  .chip.active {{ border-color: var(--link); color: var(--link); }}
  details.step {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 0.75rem;
  }}
  details.step > summary {{
    padding: 0.75rem 1rem;
    cursor: pointer;
    list-style: none;
    font-weight: 600;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  details.step > summary::-webkit-details-marker {{ display: none; }}
  details.step > summary::before {{
    content: '▸';
    margin-right: 0.5rem;
    transition: transform 0.15s;
    display: inline-block;
  }}
  details.step[open] > summary::before {{ transform: rotate(90deg); }}
  .step-count {{ color: var(--muted); font-weight: 400; font-size: 0.85rem; }}
  .lecture {{
    padding: 0.5rem 1rem 0.75rem;
    border-top: 1px solid var(--border);
  }}
  .lecture h3 {{
    margin: 0.5rem 0;
    font-size: 0.95rem;
    color: var(--muted);
    font-weight: 500;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9rem;
  }}
  td {{
    padding: 0.4rem 0.5rem;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }}
  td:first-child {{
    width: 3.5rem;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }}
  td.diff {{
    width: 5rem;
    text-align: right;
  }}
  td.extras {{
    width: 12rem;
    text-align: right;
    font-size: 0.8rem;
  }}
  a {{ color: var(--link); text-decoration: none; }}
  a:hover {{ color: var(--link-hover); text-decoration: underline; }}
  .diff-badge {{
    display: inline-block;
    padding: 0.1rem 0.55rem;
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 600;
    color: white;
  }}
  .diff-badge.easy {{ background: var(--easy); }}
  .diff-badge.medium {{ background: var(--medium); }}
  .diff-badge.hard {{ background: var(--hard); }}
  .extras a {{ margin-left: 0.5rem; color: var(--muted); }}
  .extras a:hover {{ color: var(--link); }}
  tr.hidden, .lecture.hidden, details.step.hidden {{ display: none; }}
  .host {{ color: var(--muted); font-size: 0.75rem; margin-left: 0.4rem; }}
  .no-link {{ color: var(--muted); font-style: italic; }}
  .no-link-tag {{
    margin-left: 0.5rem;
    font-size: 0.7rem;
    color: var(--muted);
    border: 1px solid var(--border);
    padding: 0 0.4rem;
    border-radius: 4px;
  }}
</style>
</head>
<body>
<header>
  <h1>Striver A2Z DSA Sheet</h1>
  <span class="meta">{total_problems} problems · {with_link} linked ({pct_link}%) · {total_lectures} lectures · 18 steps · scraped {scraped_at}</span>
</header>
<main>
  <div class="controls">
    <input id="q" type="search" placeholder="Filter by title…" autocomplete="off" autofocus>
    <button class="chip active" data-diff="all">All</button>
    <button class="chip" data-diff="Easy">Easy</button>
    <button class="chip" data-diff="Medium">Medium</button>
    <button class="chip" data-diff="Hard">Hard</button>
  </div>
  {body_html}
</main>
<script>
(() => {{
  const q = document.getElementById('q');
  const chips = document.querySelectorAll('.chip');
  let activeDiff = 'all';

  const applyFilter = () => {{
    const term = q.value.trim().toLowerCase();
    document.querySelectorAll('tr.problem').forEach(tr => {{
      const title = tr.dataset.title;
      const diff = tr.dataset.diff;
      const titleOk = !term || title.includes(term);
      const diffOk = activeDiff === 'all' || diff === activeDiff;
      tr.classList.toggle('hidden', !(titleOk && diffOk));
    }});
    // Hide lectures with zero visible rows; hide steps with zero visible lectures
    document.querySelectorAll('.lecture').forEach(lec => {{
      const any = lec.querySelectorAll('tr.problem:not(.hidden)').length > 0;
      lec.classList.toggle('hidden', !any);
    }});
    document.querySelectorAll('details.step').forEach(step => {{
      const any = step.querySelectorAll('.lecture:not(.hidden)').length > 0;
      step.classList.toggle('hidden', !any);
      if (any && term) step.open = true;
    }});
  }};

  q.addEventListener('input', applyFilter);
  chips.forEach(c => c.addEventListener('click', () => {{
    chips.forEach(x => x.classList.remove('active'));
    c.classList.add('active');
    activeDiff = c.dataset.diff;
    applyFilter();
  }}));
}})();
</script>
</body>
</html>
"""


def _h(s: str | None) -> str:
    return html.escape(s) if s else ""


def _host(url: str) -> str:
    """Return a short host label like 'leetcode' or 'gfg' for the link."""
    if not url:
        return ""
    u = url.lower()
    if "leetcode.com" in u:
        return "LeetCode"
    if "geeksforgeeks.org" in u:
        return "GFG"
    if "naukri.com" in u or "codingninjas" in u:
        return "CN"
    if "hackerrank.com" in u:
        return "HackerRank"
    if "interviewbit.com" in u:
        return "InterviewBit"
    if "spoj.com" in u:
        return "SPOJ"
    return ""


def _primary_url(p: dict[str, Any]) -> str | None:
    """Practice URL only — never fall back to takeuforward article."""
    return p.get("practice_url")


def _row_html(idx: int, p: dict[str, Any]) -> str:
    title = p.get("title") or ""
    diff = p.get("difficulty") or ""
    primary = _primary_url(p)
    host = _host(primary or "")
    if primary:
        title_link = f'<a href="{_h(primary)}" target="_blank" rel="noopener">{_h(title)}</a>'
    else:
        title_link = (
            f'<span class="no-link">{_h(title)}</span>'
            f'<span class="no-link-tag">no external link</span>'
        )

    extras: list[str] = []
    yt = p.get("youtube_url")
    if yt:
        extras.append(f'<a href="{_h(yt)}" target="_blank" rel="noopener">YT</a>')
    art = p.get("article_url")
    if art and art != primary:
        extras.append(f'<a href="{_h(art)}" target="_blank" rel="noopener">Article</a>')
    plus = p.get("plus_problem_url")
    if plus and plus != primary:
        extras.append(f'<a href="{_h(plus)}" target="_blank" rel="noopener">Plus</a>')

    diff_html = (
        f'<span class="diff-badge {diff.lower()}">{_h(diff)}</span>' if diff else ""
    )
    host_html = f'<span class="host">{host}</span>' if host else ""

    return (
        f'<tr class="problem" data-title="{_h(title.lower())}" data-diff="{_h(diff)}">'
        f"<td>{idx}</td>"
        f"<td>{title_link}{host_html}</td>"
        f'<td class="extras">{" ".join(extras)}</td>'
        f'<td class="diff">{diff_html}</td>'
        f"</tr>"
    )


def _lecture_html(lec: dict[str, Any]) -> str:
    rows = "".join(
        _row_html(i, p) for i, p in enumerate(lec.get("problems") or [], start=1)
    )
    return (
        f'<div class="lecture">'
        f"<h3>Lecture {lec['lecture_no']} · {_h(lec.get('lecture_title') or '')}</h3>"
        f"<table>{rows}</table>"
        f"</div>"
    )


def _step_html(step: dict[str, Any]) -> str:
    lectures_html = "".join(_lecture_html(l) for l in step.get("lectures") or [])
    n_problems = sum(len(l.get("problems") or []) for l in step.get("lectures") or [])
    return (
        f'<details class="step" open>'
        f"<summary>"
        f"Step {step['step_no']} · {_h(step.get('step_title') or '')}"
        f'<span class="step-count">{n_problems} problems</span>'
        f"</summary>"
        f"{lectures_html}"
        f"</details>"
    )


def build(problems_path: Path) -> str:
    data = json.loads(problems_path.read_text(encoding="utf-8"))
    body = "".join(_step_html(s) for s in data["steps"])
    total = data["totals"]["problems"]
    with_link = sum(
        1
        for s in data["steps"]
        for l in s["lectures"]
        for p in l["problems"]
        if p.get("practice_url")
    )
    pct = round(100.0 * with_link / total, 1) if total else 0.0
    return PAGE_TEMPLATE.format(
        total_problems=total,
        total_lectures=data["totals"]["lectures"],
        with_link=with_link,
        pct_link=pct,
        scraped_at=_h(data.get("scraped_at", "")),
        body_html=body,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--problems",
        type=Path,
        default=DEFAULT_PROBLEMS,
        help=f"problems JSON path (default: {DEFAULT_PROBLEMS.name})",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"output HTML path (default: {DEFAULT_OUTPUT.name})",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.problems.exists():
        print(
            f"! {args.problems} not found — run scrape_takeuforward.py first",
            file=sys.stderr,
        )
        return 1
    page = build(args.problems)
    args.output.write_text(page, encoding="utf-8")
    print(f"· wrote {args.output} ({len(page):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
