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
  /* ── link button ── */
  .btn-link {{
    background: none;
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--muted);
    cursor: pointer;
    font-size: 0.75rem;
    padding: 0.15rem 0.4rem;
    transition: color 0.15s, border-color 0.15s;
    white-space: nowrap;
  }}
  .btn-link:hover {{ color: var(--link); border-color: var(--link); }}
  .btn-link.linked {{ color: #3fb950; border-color: #3fb950; }}
  /* ── modal ── */
  #link-overlay {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.65);
    z-index: 1000;
    align-items: center;
    justify-content: center;
  }}
  #link-overlay.open {{ display: flex; }}
  #link-modal {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1.5rem;
    width: min(480px, 94vw);
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
  }}
  #link-modal h2 {{ margin: 0; font-size: 1rem; }}
  #link-modal .problem-name {{ color: var(--muted); font-size: 0.85rem; margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  #link-url {{
    padding: 0.5rem 0.75rem;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    font: inherit;
    width: 100%;
  }}
  #link-url:focus {{ outline: none; border-color: var(--link); }}
  .modal-actions {{ display: flex; gap: 0.5rem; justify-content: flex-end; }}
  .btn-save {{
    padding: 0.45rem 1rem;
    background: var(--link);
    color: #fff;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    font: inherit;
    font-weight: 600;
  }}
  .btn-save:hover {{ opacity: 0.85; }}
  .btn-save:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .btn-cancel {{
    padding: 0.45rem 1rem;
    background: none;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    cursor: pointer;
    font: inherit;
  }}
  .btn-cancel:hover {{ border-color: var(--muted); }}
  /* ── floating push panel ── */
  #push-panel {{
    position: fixed;
    bottom: 1.25rem;
    right: 1.25rem;
    z-index: 900;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 0.4rem;
  }}
  #push-status {{
    font-size: 0.75rem;
    color: var(--muted);
    text-align: right;
    max-width: 240px;
  }}
  #btn-push {{
    padding: 0.55rem 1.1rem;
    background: #238636;
    color: #fff;
    border: 1px solid #2ea043;
    border-radius: 8px;
    cursor: pointer;
    font: inherit;
    font-weight: 600;
    font-size: 0.9rem;
    display: flex;
    align-items: center;
    gap: 0.4rem;
    box-shadow: 0 4px 14px rgba(35,134,54,0.35);
    transition: opacity 0.15s;
  }}
  #btn-push:hover {{ opacity: 0.88; }}
  #btn-push:disabled {{ opacity: 0.45; cursor: not-allowed; }}
  /* ── offline banner ── */
  #offline-banner {{
    display: none;
    position: fixed;
    top: 0; left: 0; right: 0;
    background: #6e4b08;
    color: #f0c070;
    text-align: center;
    padding: 0.45rem;
    font-size: 0.82rem;
    z-index: 2000;
  }}
  /* ── status toast ── */
  #toast {{
    position: fixed;
    bottom: 5rem;
    right: 1.25rem;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.6rem 1rem;
    font-size: 0.85rem;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.25s;
    max-width: 300px;
    z-index: 950;
  }}
  #toast.show {{ opacity: 1; }}
  #toast.success {{ border-color: #2ea043; color: #3fb950; }}
  #toast.error {{ border-color: #f85149; color: #f85149; }}
</style>
</head>
<body>
<div id="offline-banner">⚠ API server offline — run <code>python api_server.py</code> to enable manual linking and GitHub push.</div>
<div id="toast"></div>
<!-- ── link modal ── -->
<div id="link-overlay" role="dialog" aria-modal="true" aria-labelledby="modal-title">
  <div id="link-modal">
    <h2 id="modal-title">Link Practice URL</h2>
    <p class="problem-name" id="modal-problem-name"></p>
    <input id="link-url" type="url" placeholder="https://leetcode.com/problems/…" autocomplete="off">
    <div class="modal-actions">
      <button class="btn-cancel" id="modal-cancel">Cancel</button>
      <button class="btn-save" id="modal-save">Save</button>
    </div>
  </div>
</div>
<!-- ── floating push panel ── -->
<div id="push-panel">
  <div id="push-status"></div>
  <button id="btn-push" title="Commit problems.json + problems.html and push to origin/main">
    🚀 Push to GitHub
  </button>
</div>
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
  /* ── filter ── */
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

  /* ── API helpers ── */
  const API = 'http://127.0.0.1:5050';
  let apiOnline = false;

  async function checkApi() {{
    try {{
      const r = await fetch(API + '/api/health', {{signal: AbortSignal.timeout(1500)}});
      apiOnline = r.ok;
    }} catch {{
      apiOnline = false;
    }}
    document.getElementById('offline-banner').style.display = apiOnline ? 'none' : 'block';
  }}
  checkApi();
  setInterval(checkApi, 10000);

  /* ── toast ── */
  let toastTimer;
  function showToast(msg, type='success') {{
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'show ' + type;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.className = '', 3500);
  }}

  /* ── modal ── */
  const overlay  = document.getElementById('link-overlay');
  const modalName= document.getElementById('modal-problem-name');
  const urlInput = document.getElementById('link-url');
  const saveBtn  = document.getElementById('modal-save');
  const cancelBtn= document.getElementById('modal-cancel');
  let currentPid = null;
  let currentBtn = null;

  function openModal(btn) {{
    if (!apiOnline) {{ showToast('API server offline — run python api_server.py', 'error'); return; }}
    currentPid = parseInt(btn.dataset.pid, 10);
    currentBtn = btn;
    const tr = btn.closest('tr');
    modalName.textContent = tr ? tr.dataset.fullTitle : '';
    urlInput.value = btn.dataset.url || '';
    overlay.classList.add('open');
    urlInput.focus();
    urlInput.select();
  }}

  function closeModal() {{
    overlay.classList.remove('open');
    currentPid = null;
    currentBtn = null;
  }}

  overlay.addEventListener('click', e => {{ if (e.target === overlay) closeModal(); }});
  cancelBtn.addEventListener('click', closeModal);
  document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeModal(); }});

  saveBtn.addEventListener('click', async () => {{
    const url = urlInput.value.trim();
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    try {{
      const r = await fetch(API + '/api/link', {{
        method: 'PATCH',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{problem_id: currentPid, url}})
      }});
      const data = await r.json();
      if (r.ok) {{
        // update button appearance inline
        currentBtn.dataset.url = url;
        currentBtn.textContent = url ? '✏ linked' : '🔗';
        currentBtn.classList.toggle('linked', !!url);
        // update title cell link inline without full reload
        const td = currentBtn.closest('tr').querySelector('td:nth-child(2)');
        if (td) {{
          const nameEl = td.querySelector('a, span.no-link');
          const name = nameEl ? nameEl.textContent : '';
          if (url) {{
            td.innerHTML = `<a href="${{url}}" target="_blank" rel="noopener">${{name}}</a>`;
          }} else {{
            td.innerHTML = `<span class="no-link">${{name}}</span><span class="no-link-tag">no external link</span>`;
          }}
          // re-append the link button
          const newBtn = document.createElement('button');
          newBtn.className = 'btn-link' + (url ? ' linked' : '');
          newBtn.dataset.pid = currentPid;
          newBtn.dataset.url = url;
          newBtn.title = 'Set / change practice URL';
          newBtn.textContent = url ? '✏ linked' : '🔗';
          newBtn.addEventListener('click', () => openModal(newBtn));
          td.appendChild(newBtn);
        }}
        showToast(url ? '✓ URL saved — regenerated HTML' : '✓ URL cleared');
        closeModal();
        document.getElementById('push-status').textContent = 'Unsaved changes — push to deploy.';
      }} else {{
        showToast(data.error || 'Error saving', 'error');
      }}
    }} catch(e) {{
      showToast('Could not reach API server', 'error');
    }} finally {{
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
    }}
  }});

  urlInput.addEventListener('keydown', e => {{ if (e.key === 'Enter') saveBtn.click(); }});

  // wire up all link buttons
  document.querySelectorAll('.btn-link').forEach(btn => {{
    btn.addEventListener('click', () => openModal(btn));
  }});

  /* ── push to GitHub ── */
  const pushBtn    = document.getElementById('btn-push');
  const pushStatus = document.getElementById('push-status');

  pushBtn.addEventListener('click', async () => {{
    if (!apiOnline) {{ showToast('API server offline', 'error'); return; }}
    pushBtn.disabled = true;
    pushBtn.textContent = '⏳ Pushing…';
    pushStatus.textContent = '';
    try {{
      const r = await fetch(API + '/api/push', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: '{{}}'}} );
      const data = await r.json();
      if (r.ok) {{
        showToast('✓ Pushed to origin/main — Vercel deploying…');
        pushStatus.textContent = 'Last push: ' + new Date().toLocaleTimeString();
      }} else {{
        showToast(data.error || 'Push failed', 'error');
        pushStatus.textContent = 'Push failed — check console';
        console.error(data.error);
      }}
    }} catch(e) {{
      showToast('Could not reach API server', 'error');
    }} finally {{
      pushBtn.disabled = false;
      pushBtn.textContent = '🚀 Push to GitHub';
    }}
  }});
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

    pid = p.get("id") or 0
    link_btn_class = "btn-link linked" if primary else "btn-link"
    link_btn_label = "\u270f linked" if primary else "\U0001f517"
    link_btn = (
        f'<button class="{link_btn_class}" data-pid="{pid}"'
        f' data-url="{_h(primary or "")}"'
        f' title="Set / change practice URL">'
        f'{link_btn_label}</button>'
    )

    return (
        f'<tr class="problem" data-title="{_h(title.lower())}"'
        f' data-diff="{_h(diff)}" data-full-title="{_h(title)}">'
        f"<td>{idx}</td>"
        f"<td>{title_link}{host_html} {link_btn}</td>"
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
