"""
Stage 4 — semantic problem-statement matching.

For every Striver problem in problems.json that still has no `practice_url`,
this script:

  1. Fetches the Striver article body (Playwright, cached to disk).
  2. Narrows the LeetCode catalogue to the top-N title-similar candidates.
  3. Fetches each candidate's problem statement via the LeetCode GraphQL API
     (cached to disk).
  4. Asks Groq's Llama-3.3-70B to judge, in one batched call per Striver
     problem, whether any of the candidates ask the exact same question with
     the same input shape, output and required logic — and to emit a JSON
     verdict {"slug": "...", "confidence": 0..1, "reason": "..."}.
  5. If confidence >= threshold AND the slug is in the candidate set, writes
     practice_url = https://leetcode.com/problems/<slug>/ with
     practice_url_source = "leetcode-semantic".

Calibration
-----------
With --calibrate, runs the LLM judge against:
  * 30 already-correct Striver→LC pairs (positive control)
  * 30 random Striver→LC mismatches (negative control)

The lowest confidence the LLM emits on any NEGATIVE pair becomes the auto-
picked production threshold (guaranteeing 0% known-bad leakage), but never
lower than --min-threshold (defaults to 0.97 per user spec).

Idempotency
-----------
On every production run, any prior `leetcode-semantic` URL is cleared
*before* matching, so re-runs re-derive cleanly. Other sources
(takeuforward / leetcode-exact / leetcode-fuzzy / codolio-*) are NEVER
touched.

Requirements
------------
* GROQ_API_KEY in environment or in `.env` (gitignored).
* Playwright + Chromium already installed (same as scraper).
* problems.json + _leetcode_cache.json present.

Usage
-----
    python enrich_practice_urls_semantic.py --calibrate   # phase 0
    python enrich_practice_urls_semantic.py               # production
    python enrich_practice_urls_semantic.py --limit 5     # smoke test
    python enrich_practice_urls_semantic.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

# Force UTF-8 on stdout for Windows cp1252 consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
PROBLEMS_FILE = HERE / "problems.json"
LC_CATALOGUE_CACHE = HERE / "_leetcode_cache.json"
STRIVER_DESC_DIR = HERE / "_striver_descriptions"
LC_DESC_DIR = HERE / "_lc_descriptions"
LLM_VERDICT_CACHE = HERE / "_llm_verdicts.json"
ENV_FILE = HERE / ".env"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_RATE_LIMIT_SLEEP = 2.1  # 30 RPM / 60 = 2 sec, plus margin

GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
)
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"
GEMINI_RATE_LIMIT_SLEEP = 4.1  # free tier 15 RPM for 2.5-flash-lite

NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
DEFAULT_NVIDIA_MODEL = "meta/llama-3.3-70b-instruct"
NVIDIA_RATE_LIMIT_SLEEP = 5.0  # free tier appears to burst-limit ~12 RPM


def _provider_for_model(model: str) -> str:
    """Return 'groq', 'gemini', or 'nvidia' for the given model name.

    Heuristic: a slash in the name (e.g. 'meta/llama-3.3-70b-instruct') is
    NVIDIA's catalogue convention. A 'gemini-' prefix is Google. Everything
    else (the plain Llama / Mixtral names) is Groq.
    """
    m = model.lower()
    if "/" in m:
        return "nvidia"
    if m.startswith("gemini"):
        return "gemini"
    return "groq"


def _rate_limit_sleep_for(model: str) -> float:
    prov = _provider_for_model(model)
    if prov == "gemini":
        return GEMINI_RATE_LIMIT_SLEEP
    if prov == "nvidia":
        return NVIDIA_RATE_LIMIT_SLEEP
    return GROQ_RATE_LIMIT_SLEEP

LC_GRAPHQL_URL = "https://leetcode.com/graphql/"
LC_RATE_LIMIT_SLEEP = 0.25  # gentle on LC

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Titles we always skip — pure theory or algorithm-name rows with no problem
# statement that any platform would host.
CONCEPT_BLACKLIST_EXACT = {
    "Theory with examples",
    "Easy and Medium",
    "Hard",
    "STL",
    "Java Collections",
    "Pre Requisites for Bit Manipulation",
    "Bit PreRequisites for TRIE Problems",
    "Introduction to DP",
    "Introduction to Graph",
    "Graph Representation | C++",
    "Graph Representation | Java",
    "MST theory",
    "Traversal Techniques",
    "BFS",
    "DFS",
    "Connected Components",
    "Topo Sort",
    "Topological sort or Kahn's algorithm",
    "Djisktra's Algorithm",
    "Why priority Queue is used in Djisktra's Algorithm",
    "Bellman Ford Algorithm",
    "Floyd warshall algorithm",
    "Prim's Algorithm",
    "Disjoint Set",
    "Hashing In Strings | Theory",
    "Learn All Patterns of Subsequences (Theory)",
}
CONCEPT_BLACKLIST_PREFIX = ("Pattern ",)

# How many LC candidates to consider per Striver problem
DEFAULT_TOP_K_CANDIDATES = 5

# Default min threshold — the user specified ≥0.97
DEFAULT_MIN_THRESHOLD = 0.97

# Calibration sample sizes
CALIBRATION_POSITIVE = 30
CALIBRATION_NEGATIVE = 30


# ---------------------------------------------------------------------------
# .env loader (no external deps)
# ---------------------------------------------------------------------------

def _load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


# ---------------------------------------------------------------------------
# Striver article fetcher (Playwright)
# ---------------------------------------------------------------------------

class _StriverFetcher:
    """Reuses a single Playwright browser across many fetches."""

    def __init__(self, headless: bool = True):
        self._headless = headless
        self._pw = None
        self._browser = None
        self._ctx = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self._headless)
        self._ctx = self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
        )
        return self

    def __exit__(self, *args):
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()

    def fetch(self, url: str) -> str | None:
        """Return the visible article text, or None on failure."""
        page = self._ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # let the article body hydrate
            page.wait_for_timeout(1500)
            # extract a sensible "main content" — strip nav/footer/code/sidebar
            text = page.evaluate(
                """
                () => {
                  // Remove obvious chrome before reading text
                  const drop = (sel) => document.querySelectorAll(sel).forEach(n => n.remove());
                  drop('script'); drop('style'); drop('noscript');
                  drop('header'); drop('footer'); drop('nav');
                  drop('aside');
                  drop('[class*="sidebar" i]');
                  drop('[class*="navbar" i]');
                  drop('[class*="topbar" i]');
                  drop('[class*="cookie" i]');
                  drop('[class*="ad-" i]');
                  drop('[role="banner"]'); drop('[role="navigation"]');
                  // try a few likely article containers, fall back to body
                  const tries = [
                    'main article', 'article', 'main',
                    '[class*="article" i]', '[class*="content" i]',
                  ];
                  for (const sel of tries) {
                    const el = document.querySelector(sel);
                    if (el && (el.innerText || '').length > 400) {
                      return el.innerText;
                    }
                  }
                  return document.body.innerText || '';
                }
                """
            )
            return _clean_text(text)
        except PlaywrightTimeoutError:
            return None
        except Exception as e:
            print(f"  ! striver fetch error for {url}: {e}", file=sys.stderr)
            return None
        finally:
            page.close()


def _clean_text(text: str) -> str:
    """Normalise whitespace and trim. Removes boilerplate lines we never want."""
    if not text:
        return ""
    text = text.replace(" ", " ")
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        # drop nav / ad-ish lines
        if s.lower() in {
            "home", "blog", "discussion", "solve", "editorial", "plus",
            "track", "search", "menu", "login", "sign in", "sign up",
            "command palette", "search for a command to run...",
        }:
            continue
        lines.append(s)
    out = "\n".join(lines)
    # collapse 3+ blank-ish lines into one
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _striver_cache_path(problem_id: int | None, url: str) -> Path:
    STRIVER_DESC_DIR.mkdir(exist_ok=True)
    if problem_id is not None:
        return STRIVER_DESC_DIR / f"{problem_id}.txt"
    safe = re.sub(r"[^a-z0-9]+", "_", url.lower())[:80]
    return STRIVER_DESC_DIR / f"url__{safe}.txt"


def fetch_striver_desc(
    fetcher: _StriverFetcher | None,
    *,
    problem_id: int | None,
    url: str | None,
    max_len: int = 4000,
) -> str | None:
    """Cached Striver article text. Returns None if URL is missing or fetch
    fails. Requires a live _StriverFetcher for cache misses."""
    if not url:
        return None
    path = _striver_cache_path(problem_id, url)
    if path.exists():
        return path.read_text(encoding="utf-8")[:max_len]
    if fetcher is None:
        # Can't fetch and no cache — caller wanted cache-only
        return None
    text = fetcher.fetch(url)
    if not text:
        return None
    path.write_text(text, encoding="utf-8")
    return text[:max_len]


# ---------------------------------------------------------------------------
# LeetCode GraphQL fetcher
# ---------------------------------------------------------------------------

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "br", "li", "pre", "div"):
            self.parts.append("\n")

    def text(self) -> str:
        return "".join(self.parts)


def _strip_html(html: str) -> str:
    s = _HTMLStripper()
    s.feed(html or "")
    return _clean_text(s.text())


def _lc_cache_path(slug: str) -> Path:
    LC_DESC_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^a-z0-9-]+", "_", slug.lower())[:80]
    return LC_DESC_DIR / f"{safe}.txt"


def fetch_leetcode_desc(slug: str, *, max_len: int = 4000) -> str | None:
    """Cached LC problem statement (HTML stripped). Returns None on failure."""
    if not slug:
        return None
    path = _lc_cache_path(slug)
    if path.exists():
        return path.read_text(encoding="utf-8")[:max_len]

    payload = json.dumps({
        "query": (
            "query getQuestion($titleSlug: String!) { "
            "  question(titleSlug: $titleSlug) { "
            "    title content difficulty exampleTestcases "
            "  } "
            "}"
        ),
        "variables": {"titleSlug": slug},
    }).encode("utf-8")
    req = urllib.request.Request(
        LC_GRAPHQL_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Referer": f"https://leetcode.com/problems/{slug}/",
        },
        method="POST",
    )
    delay = 1.0
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 3:
                time.sleep(delay)
                delay *= 2
                continue
            print(f"  ! LC fetch error for {slug}: HTTP {e.code}", file=sys.stderr)
            return None
        except Exception as e:
            if attempt < 3:
                time.sleep(delay)
                delay *= 2
                continue
            print(f"  ! LC fetch error for {slug}: {e}", file=sys.stderr)
            return None
    time.sleep(LC_RATE_LIMIT_SLEEP)
    q = ((body or {}).get("data") or {}).get("question") or {}
    if not q:
        return None
    html = q.get("content") or ""
    text = _strip_html(html)
    if not text:
        return None
    path.write_text(text, encoding="utf-8")
    return text[:max_len]


# ---------------------------------------------------------------------------
# Candidate narrowing (title fuzzy match against the LC catalogue)
# ---------------------------------------------------------------------------

@dataclass
class LCCandidate:
    slug: str
    title: str
    id: str
    difficulty_level: int | None


def _norm_title(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def load_lc_catalogue() -> list[LCCandidate]:
    if not LC_CATALOGUE_CACHE.exists():
        sys.exit(
            f"! {LC_CATALOGUE_CACHE.name} missing — "
            f"run enrich_practice_urls.py first (it caches the LC catalogue)."
        )
    raw = json.loads(LC_CATALOGUE_CACHE.read_text(encoding="utf-8"))
    return [
        LCCandidate(
            slug=p["slug"],
            title=p["title"],
            id=str(p.get("id") or ""),
            difficulty_level=p.get("difficulty_level"),
        )
        for p in raw
    ]


def top_k_candidates(
    striver_title: str, catalogue: list[LCCandidate], k: int
) -> list[LCCandidate]:
    needle = _norm_title(striver_title)
    if not needle:
        return []
    sm = SequenceMatcher(None, needle, "")
    scored: list[tuple[float, LCCandidate]] = []
    for c in catalogue:
        h = _norm_title(c.title)
        sm.set_seq2(h)
        if sm.quick_ratio() < 0.30:  # cheap filter
            continue
        score = sm.ratio()
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:k]]


# ---------------------------------------------------------------------------
# Groq LLM judge
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = (
    "You are a careful judge that decides whether two competitive-programming "
    "problems are the SAME problem. Two problems are 'the same' only when "
    "they take the same input shape, return the same output, and require the "
    "same algorithmic logic. Related-but-different problems (e.g. 'find max' "
    "vs 'find min', 'subset sum' vs 'partition equal subset sum', "
    "'two-sum' vs 'three-sum') are NOT the same. You return one JSON object "
    "and nothing else."
)

_JUDGE_USER_TEMPLATE = """STRIVER PROBLEM
Title: {striver_title}
Difficulty: {striver_diff}

Description (may include explanation/code — focus on the problem ask):
\"\"\"
{striver_desc}
\"\"\"

LEETCODE CANDIDATES (zero-indexed):
{candidates_block}

TASK
For each candidate, judge whether it asks the SAME problem as the Striver
one (same input, same output, same required algorithm). Then pick the BEST
candidate if any. If none qualify, return index = -1.

Output JSON only, with EXACTLY this shape:
{{
  "index": <int from -1 to {max_index}>,
  "slug": "<the chosen candidate's slug or null>",
  "confidence": <float 0.0 to 1.0>,
  "reason": "<one short sentence>"
}}
"""


def _gemini_call(
    messages: list[dict[str, str]],
    *,
    model: str = DEFAULT_GEMINI_MODEL,
    timeout: float = 45.0,
) -> str:
    """Call Gemini's generateContent endpoint. Returns the raw model text."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("! GEMINI_API_KEY not set (in .env or environment)")
    # Gemini takes a single 'systemInstruction' + a list of user/model 'contents'.
    # We translate the OpenAI-style messages.
    sys_inst = None
    contents: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        text = m.get("content") or ""
        if role == "system":
            sys_inst = {"parts": [{"text": text}]}
        else:
            contents.append({
                "role": "user" if role == "user" else "model",
                "parts": [{"text": text}],
            })
    body = {
        "contents": contents,
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0,
            # gemini-2.5-* default to "thinking" mode which silently consumes
            # output tokens. Disable it; for a 1-line JSON verdict we don't
            # need any internal reasoning budget.
            "thinkingConfig": {"thinkingBudget": 0},
            "maxOutputTokens": 400,
        },
    }
    if sys_inst:
        body["systemInstruction"] = sys_inst

    url = GEMINI_URL_TEMPLATE.format(model=model, key=key)
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    delay = 2.0
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.load(r)
            break
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            # 429 = rate limit, 503 = overloaded — both are transient
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(delay)
                delay *= 2
                continue
            if e.code == 429:
                raise SystemExit(
                    f"! Gemini rate-limit exceeded after retries.\n  Response: {err_body}"
                )
            raise SystemExit(f"! Gemini HTTP {e.code}: {err_body}")
        except Exception as e:
            if attempt < 4:
                time.sleep(delay)
                delay *= 2
                continue
            raise SystemExit(f"! Gemini call failed: {e}")
    else:
        raise SystemExit("! Gemini retries exhausted")

    # Pull the JSON-text out of the candidates structure
    cand = (resp.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = next((p.get("text") for p in parts if p.get("text")), "")
    return text or ""


def _nvidia_call(
    messages: list[dict[str, str]],
    *,
    model: str = DEFAULT_NVIDIA_MODEL,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Call NVIDIA NIM (OpenAI-compatible). Returns the parsed JSON body."""
    key = os.environ.get("NVIDIA_API_KEY")
    if not key:
        raise SystemExit("! NVIDIA_API_KEY not set (in .env or environment)")
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0,
        # NVIDIA NIM accepts response_format on most chat models that support
        # JSON mode; falls back gracefully if the model ignores it.
        "response_format": {"type": "json_object"},
        "max_tokens": 400,
    }).encode("utf-8")
    req = urllib.request.Request(
        NVIDIA_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    delay = 2.0
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            if e.code == 429 and attempt < 4:
                # NVIDIA's free tier rate-limit is per-minute. A short backoff
                # isn't enough — wait at least 60s for the window to slide.
                retry_after = e.headers.get("Retry-After") if hasattr(e, "headers") else None
                wait = max(60, int(retry_after) if retry_after and retry_after.isdigit() else 60)
                print(f"  ! NVIDIA 429 (rate limit); sleeping {wait}s before retry", flush=True)
                time.sleep(wait)
                continue
            if e.code in (500, 502, 503, 504) and attempt < 4:
                time.sleep(delay)
                delay *= 2
                continue
            raise SystemExit(f"! NVIDIA HTTP {e.code}: {err_body}")
        except Exception as e:
            if attempt < 4:
                time.sleep(delay)
                delay *= 2
                continue
            raise SystemExit(f"! NVIDIA call failed: {e}")
    raise SystemExit("! NVIDIA retries exhausted")


def _groq_call(
    messages: list[dict[str, str]],
    *,
    model: str = DEFAULT_GROQ_MODEL,
    timeout: float = 45.0,
) -> dict[str, Any]:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise SystemExit("! GROQ_API_KEY not set (in .env or environment)")
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 300,
    }).encode("utf-8")
    req = urllib.request.Request(
        GROQ_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="POST",
    )
    delay = 2.0
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            # If we hit a daily-token-limit, NO point retrying — bail with a
            # clear message so the user can resume tomorrow or switch model.
            if e.code == 429 and "tokens per day" in body.lower():
                raise SystemExit(
                    f"! Groq daily token limit reached for this model.\n"
                    f"  Response: {body}\n"
                    f"  Try --model llama-3.1-8b-instant (500K TPD vs 100K), "
                    f"or wait until the limit resets."
                )
            if e.code == 429 and attempt < 4:
                # Per-minute rate limit — back off and retry
                time.sleep(delay)
                delay *= 2
                continue
            raise SystemExit(f"! Groq HTTP {e.code}: {body}")
        except Exception as e:
            if attempt < 4:
                time.sleep(delay)
                delay *= 2
                continue
            raise SystemExit(f"! Groq call failed: {e}")
    raise SystemExit("! Groq retries exhausted")


def _load_verdict_cache() -> dict[str, Any]:
    if LLM_VERDICT_CACHE.exists():
        try:
            return json.loads(LLM_VERDICT_CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _atomic_replace(src: Path, dst: Path, *, retries: int = 8) -> None:
    """`os.replace` with retry for transient Windows / OneDrive file locks.
    OneDrive frequently grabs a brief shared lock on a file it's syncing,
    causing PermissionError [WinError 5] / [WinError 32]. Backoff and retry."""
    delay = 0.3
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 1.7, 4.0)


def _save_verdict_cache(cache: dict[str, Any]) -> None:
    tmp = LLM_VERDICT_CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    _atomic_replace(tmp, LLM_VERDICT_CACHE)


def _verdict_cache_key(
    problem_id: int | None, candidates: list["LCCandidate"], model: str
) -> str:
    """Stable cache key for a judge call. If the candidate set changes
    (e.g. top-K changed), the key changes and we re-judge."""
    slugs = ",".join(sorted(c.slug for c in candidates))
    return f"{problem_id}|{model}|{slugs}"


def _judge_pair_set(
    striver_title: str,
    striver_diff: str | None,
    striver_desc: str,
    candidates: list[LCCandidate],
    candidate_descs: list[str],
    *,
    model: str = DEFAULT_GROQ_MODEL,
    verdict_cache: dict[str, Any] | None = None,
    problem_id: int | None = None,
) -> dict[str, Any] | None:
    """Send one batched LLM call: 1 Striver + up to K LC candidates.
    Returns parsed judge JSON or None on failure. Cached by (problem_id,
    sorted candidate slugs, model)."""
    if not candidates:
        return None
    if verdict_cache is not None and problem_id is not None:
        ck = _verdict_cache_key(problem_id, candidates, model)
        if ck in verdict_cache:
            return verdict_cache[ck]
    # Trim aggressively to stay under Groq free-tier TPM budget. The first
    # ~1500 chars of an LC problem statement always contain the ask, input,
    # output and one example — that's enough for "same problem?" judgement.
    blocks: list[str] = []
    for i, (c, desc) in enumerate(zip(candidates, candidate_descs)):
        blocks.append(
            f"[{i}] LC #{c.id}  title: {c.title}  slug: {c.slug}\n"
            f"description:\n\"\"\"\n{(desc or '(empty)')[:1500]}\n\"\"\"\n"
        )
    candidates_block = "\n".join(blocks)
    user_msg = _JUDGE_USER_TEMPLATE.format(
        striver_title=striver_title,
        striver_diff=striver_diff or "?",
        striver_desc=striver_desc[:2200],
        candidates_block=candidates_block,
        max_index=len(candidates) - 1,
    )
    provider = _provider_for_model(model)
    msgs = [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    if provider == "gemini":
        content = _gemini_call(msgs, model=model) or "{}"
    elif provider == "nvidia":
        body = _nvidia_call(msgs, model=model)
        content = (
            (body.get("choices") or [{}])[0].get("message", {}).get("content") or "{}"
        )
    else:
        body = _groq_call(msgs, model=model)
        content = (
            (body.get("choices") or [{}])[0].get("message", {}).get("content") or "{}"
        )
    try:
        verdict = json.loads(content)
    except json.JSONDecodeError:
        # Salvage: pull the first {...} block
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return None
        try:
            verdict = json.loads(m.group(0))
        except Exception:
            return None
    if not isinstance(verdict, dict):
        return None
    # Persist verdict to cache so re-runs skip the LLM call entirely
    if verdict_cache is not None and problem_id is not None:
        ck = _verdict_cache_key(problem_id, candidates, model)
        verdict_cache[ck] = verdict
    return verdict


# ---------------------------------------------------------------------------
# Calibration (Phase 0)
# ---------------------------------------------------------------------------

def _flatten_problems(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for s in report["steps"]:
        for l in s["lectures"]:
            for p in l["problems"]:
                out.append(p)
    return out


def _lc_slug_from_url(url: str) -> str | None:
    if not url:
        return None
    m = re.search(r"leetcode\.com/problems/([a-z0-9-]+)", url)
    return m.group(1) if m else None


def run_calibration(
    *, headless: bool, min_threshold: float, model: str = DEFAULT_GROQ_MODEL
) -> float:
    """Run the LLM judge against known good + bad pairs, return chosen threshold."""
    report = json.loads(PROBLEMS_FILE.read_text(encoding="utf-8"))
    flat = _flatten_problems(report)
    catalogue = load_lc_catalogue()

    # Pool of problems that already have a confirmed LC practice_url
    confirmed: list[tuple[dict[str, Any], str]] = []
    for p in flat:
        slug = _lc_slug_from_url(p.get("practice_url") or "")
        if slug and p.get("practice_url_source") in (
            "takeuforward", "leetcode-exact", "leetcode-fuzzy", "codolio-exact"
        ):
            confirmed.append((p, slug))

    rng = random.Random(42)
    pos_pool = list(confirmed)
    rng.shuffle(pos_pool)
    pos_sample = pos_pool[:CALIBRATION_POSITIVE]

    # Negative pairs: take the same Striver problems but swap to a RANDOM LC slug
    # from the catalogue that does NOT match the real one.
    catalogue_slugs = [c for c in catalogue if c.slug]
    rng.shuffle(catalogue_slugs)

    neg_sample: list[tuple[dict[str, Any], LCCandidate]] = []
    seen_neg_keys: set[tuple[int, str]] = set()
    for p, real_slug in pos_pool[:CALIBRATION_NEGATIVE]:
        # pick a random candidate that's clearly NOT the real slug
        for c in catalogue_slugs:
            if c.slug != real_slug and (p["id"], c.slug) not in seen_neg_keys:
                neg_sample.append((p, c))
                seen_neg_keys.add((p["id"], c.slug))
                break
        catalogue_slugs.append(catalogue_slugs.pop(0))  # cycle to avoid same slug each time

    print(f"\n· calibration: {len(pos_sample)} positive + {len(neg_sample)} negative pairs")

    pos_scores: list[float] = []
    neg_scores: list[float] = []

    verdict_cache = _load_verdict_cache()
    with _StriverFetcher(headless=headless) as sf:
        # Positives
        for i, (p, slug) in enumerate(pos_sample, 1):
            verdict = _calibration_one_pair(p, [slug], sf, catalogue, model, verdict_cache)
            # Save verdict cache every 5 calls so a rate-limit crash doesn't
            # lose all the LLM work we just paid for.
            if i % 5 == 0:
                _save_verdict_cache(verdict_cache)
            score = float(verdict.get("confidence") or 0.0) if verdict else 0.0
            picked = (verdict or {}).get("slug")
            matched = (picked == slug)
            print(
                f"  [pos {i:>2}/{len(pos_sample)}] {p['title'][:45]:<45} "
                f"-> conf {score:.3f}  picked={picked!r}  "
                f"{'OK' if matched else '(model disagreed)'}"
            )
            if matched:
                pos_scores.append(score)
            time.sleep(_rate_limit_sleep_for(model))
        # Negatives
        for i, (p, c) in enumerate(neg_sample, 1):
            verdict = _calibration_one_pair(p, [c.slug], sf, catalogue, model, verdict_cache)
            if i % 5 == 0:
                _save_verdict_cache(verdict_cache)
            score = float(verdict.get("confidence") or 0.0) if verdict else 0.0
            picked = (verdict or {}).get("slug")
            print(
                f"  [neg {i:>2}/{len(neg_sample)}] {p['title'][:35]:<35}  "
                f"vs LC {c.title[:30]:<30}  conf {score:.3f}  picked={picked!r}"
            )
            # If the model picked the wrong slug at all, count its confidence as a leak
            if picked == c.slug:
                neg_scores.append(score)
            time.sleep(_rate_limit_sleep_for(model))

    print()
    if pos_scores:
        ps = sorted(pos_scores)
        print(
            f"· positive scores (n={len(ps)}): "
            f"min={ps[0]:.3f}  p25={ps[len(ps)//4]:.3f}  "
            f"median={ps[len(ps)//2]:.3f}  max={ps[-1]:.3f}"
        )
    else:
        print("· no positive matches survived — model never agreed on confirmed pairs!?")
    if neg_scores:
        ns = sorted(neg_scores, reverse=True)
        print(
            f"· negative-leak scores (n={len(ns)}): "
            f"max={ns[0]:.3f}  p75={ns[len(ns)//4]:.3f}  "
            f"median={ns[len(ns)//2]:.3f}  min={ns[-1]:.3f}"
        )
        # Auto-pick = first score strictly above the largest negative leak.
        auto = max(ns[0] + 0.001, min_threshold)
    else:
        print("· no negative leaks at all — using min_threshold")
        auto = min_threshold

    chosen = max(auto, min_threshold)
    print(f"\n· chosen threshold = {chosen:.3f}  (min={min_threshold:.3f}, "
          f"auto-pick={auto:.3f})")
    _save_verdict_cache(verdict_cache)
    return chosen


def _calibration_one_pair(
    p: dict[str, Any],
    candidate_slugs: list[str],
    sf: _StriverFetcher,
    catalogue: list[LCCandidate],
    model: str,
    verdict_cache: dict[str, Any],
) -> dict[str, Any] | None:
    striver_desc = fetch_striver_desc(
        sf,
        problem_id=p.get("id"),
        url=p.get("article_url") or p.get("plus_problem_url"),
    )
    if not striver_desc:
        return None
    cat_by_slug = {c.slug: c for c in catalogue}
    cands: list[LCCandidate] = []
    descs: list[str] = []
    for slug in candidate_slugs:
        c = cat_by_slug.get(slug)
        if c is None:
            continue
        d = fetch_leetcode_desc(slug)
        if d:
            cands.append(c)
            descs.append(d)
    if not cands:
        return None
    return _judge_pair_set(
        striver_title=p["title"],
        striver_diff=p.get("difficulty"),
        striver_desc=striver_desc,
        candidates=cands,
        candidate_descs=descs,
        model=model,
        verdict_cache=verdict_cache,
        problem_id=p.get("id"),
    )


# ---------------------------------------------------------------------------
# Production matching
# ---------------------------------------------------------------------------

def _reset_stale_semantic(p: dict[str, Any]) -> None:
    if p.get("practice_url_source") == "leetcode-semantic":
        p["practice_url"] = None
        p.pop("practice_url_source", None)
        p.pop("practice_url_confidence", None)
        p.pop("practice_url_reason", None)


def _is_blacklisted(title: str) -> bool:
    if title in CONCEPT_BLACKLIST_EXACT:
        return True
    return any(title.startswith(pref) for pref in CONCEPT_BLACKLIST_PREFIX)


def _difficulty_compatible(striver: str | None, lc_level: int | None) -> bool:
    """Reject pairs that differ by more than one level on the Easy/Med/Hard scale."""
    if not striver or not lc_level:
        return True
    order = {"Easy": 0, "Medium": 1, "Hard": 2}
    lc_str = {1: "Easy", 2: "Medium", 3: "Hard"}.get(lc_level)
    if not lc_str or striver not in order or lc_str not in order:
        return True
    return abs(order[striver] - order[lc_str]) <= 1


def _process_one_problem(
    *,
    p: dict[str, Any],
    title: str,
    fetcher_factory,
    catalogue: list[LCCandidate],
    top_k: int,
    model: str,
    threshold: float,
    verdict_cache: dict[str, Any],
    stats: dict[str, int],
    samples: list[str],
) -> None:
    """Pull a Striver desc + top-K LC descs + ask the judge. Records the
    outcome in `stats`, prints a one-line summary, may mutate `p` to add the
    new `practice_url` fields when a match is accepted.

    Network sleeps run inside this function so that the caller's `finally`
    block always runs the same way after each problem."""
    sleep_sec = _rate_limit_sleep_for(model)

    sf = fetcher_factory()
    striver_desc = fetch_striver_desc(
        sf,
        problem_id=p.get("id"),
        url=p.get("article_url") or p.get("plus_problem_url"),
    )
    if not striver_desc or len(striver_desc) < 200:
        stats["rejected_no_desc"] += 1
        print("  no usable striver description")
        return

    cands = top_k_candidates(title, catalogue, top_k)
    descs: list[str] = []
    kept: list[LCCandidate] = []
    for c in cands:
        d = fetch_leetcode_desc(c.slug)
        if d:
            kept.append(c)
            descs.append(d)
    if not kept:
        stats["rejected_no_desc"] += 1
        print("  no LC candidate descriptions")
        return

    cache_key = _verdict_cache_key(p.get("id"), kept, model)
    from_cache = cache_key in verdict_cache
    verdict = _judge_pair_set(
        striver_title=title,
        striver_diff=p.get("difficulty"),
        striver_desc=striver_desc,
        candidates=kept,
        candidate_descs=descs,
        model=model,
        verdict_cache=verdict_cache,
        problem_id=p.get("id"),
    )
    if from_cache:
        stats["verdict_cache_hits"] += 1
    if not verdict:
        stats["errors"] += 1
        print("  judge call failed")
        if not from_cache:
            time.sleep(sleep_sec)
        return

    picked_slug = verdict.get("slug")
    conf = float(verdict.get("confidence") or 0.0)
    picked_obj = next((c for c in kept if c.slug == picked_slug), None)

    if not picked_slug or picked_slug == "null":
        stats["rejected_below_threshold"] += 1
        print(f"  no match (judge said none)")
        if not from_cache:
            time.sleep(sleep_sec)
        return

    if picked_obj is None:
        stats["rejected_bad_slug"] += 1
        print(f"  bad slug {picked_slug!r} (not in candidates)")
        if not from_cache:
            time.sleep(sleep_sec)
        return

    if conf < threshold:
        stats["rejected_below_threshold"] += 1
        print(f"  conf {conf:.3f} < {threshold:.3f}")
        if not from_cache:
            time.sleep(sleep_sec)
        return

    if not _difficulty_compatible(p.get("difficulty"), picked_obj.difficulty_level):
        stats["rejected_difficulty"] += 1
        print(
            f"  difficulty mismatch (striver={p.get('difficulty')} "
            f"lc={picked_obj.difficulty_level})"
        )
        if not from_cache:
            time.sleep(sleep_sec)
        return

    p["practice_url"] = f"https://leetcode.com/problems/{picked_slug}/"
    p["practice_url_source"] = "leetcode-semantic"
    p["practice_url_confidence"] = round(conf, 3)
    p["practice_url_reason"] = (verdict.get("reason") or "")[:160]
    stats["matched"] += 1
    print(f"  matched LC {picked_obj.title!r} conf={conf:.3f}")
    if len(samples) < 12:
        samples.append(f"[{conf:.3f}] {title!r} -> {picked_obj.title!r}")
    if not from_cache:
        time.sleep(sleep_sec)


def run_production(
    *,
    threshold: float,
    limit: int | None,
    dry_run: bool,
    headless: bool,
    top_k: int,
    model: str,
) -> None:
    report = json.loads(PROBLEMS_FILE.read_text(encoding="utf-8"))
    flat = _flatten_problems(report)
    catalogue = load_lc_catalogue()

    candidates_to_process: list[dict[str, Any]] = []
    for p in flat:
        _reset_stale_semantic(p)
        if p.get("practice_url"):
            continue
        title = p.get("title") or ""
        if _is_blacklisted(title):
            continue
        if not (p.get("article_url") or p.get("plus_problem_url")):
            continue
        candidates_to_process.append(p)
    if limit:
        candidates_to_process = candidates_to_process[:limit]

    stats = {
        "considered": len(candidates_to_process),
        "matched": 0,
        "rejected_no_desc": 0,
        "rejected_below_threshold": 0,
        "rejected_difficulty": 0,
        "rejected_bad_slug": 0,
        "errors": 0,
        "verdict_cache_hits": 0,
    }
    samples: list[str] = []
    verdict_cache = _load_verdict_cache()

    print(
        f"\n· production: {stats['considered']} problem(s), "
        f"threshold={threshold:.3f}, top_k={top_k}, model={model}"
    )
    print(f"· verdict cache: {len(verdict_cache)} prior entries loaded")

    def _save_partial():
        """Write problems.json + verdict cache atomically so a crash mid-run
        doesn't lose anything."""
        if dry_run:
            return
        tmp = PROBLEMS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        _atomic_replace(tmp, PROBLEMS_FILE)
        _save_verdict_cache(verdict_cache)

    sf_holder: list[_StriverFetcher] = []

    def _ensure_fetcher() -> _StriverFetcher:
        """Lazily (re-)create the Playwright browser. Used after a crash."""
        if not sf_holder:
            sf = _StriverFetcher(headless=headless)
            sf.__enter__()
            sf_holder.append(sf)
        return sf_holder[0]

    def _drop_fetcher() -> None:
        """Force-close the browser; next _ensure_fetcher() builds a fresh one."""
        if sf_holder:
            try:
                sf_holder[0].__exit__(None, None, None)
            except Exception:
                pass
            sf_holder.clear()

    try:
        for i, p in enumerate(candidates_to_process, 1):
            title = p.get("title") or ""
            print(f"  [{i:>3}/{stats['considered']}] {title[:55]:<55}", end="", flush=True)
            try:
                _process_one_problem(
                    p=p,
                    title=title,
                    fetcher_factory=_ensure_fetcher,
                    catalogue=catalogue,
                    top_k=top_k,
                    model=model,
                    threshold=threshold,
                    verdict_cache=verdict_cache,
                    stats=stats,
                    samples=samples,
                )
            except Exception as e:
                # Per-problem error (most likely a Playwright crash). Drop the
                # browser so the next iteration rebuilds it; do NOT kill the run.
                stats["errors"] += 1
                print(f"  ! exception: {type(e).__name__}: {str(e)[:120]}")
                _drop_fetcher()
            finally:
                # Save EVERY 10 problems regardless of branch (matched, skipped,
                # rejected, errored). This was previously after `continue`s
                # which silently skipped the save.
                if i % 10 == 0:
                    _save_partial()
    finally:
        _drop_fetcher()

    # Final save of cache (problems.json save happens below)
    if not dry_run:
        _save_verdict_cache(verdict_cache)

    report.setdefault("enrichment", {})
    report["enrichment"]["semantic"] = {
        "threshold": threshold,
        "stats": stats,
    }

    print()
    print("· semantic enrichment stats:")
    for k, v in stats.items():
        print(f"    {k:<28} {v:>4}")
    if samples:
        print("\n· sample matches (sanity-check these):")
        for s in samples:
            print(f"    {s}")

    if dry_run:
        print("\n· dry-run: not writing problems.json")
        return

    # Atomic write (with retry for OneDrive locks)
    tmp = PROBLEMS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    _atomic_replace(tmp, PROBLEMS_FILE)
    print(f"\n· wrote {PROBLEMS_FILE.name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--calibrate",
        action="store_true",
        help="run Phase 0 calibration (30 pos + 30 neg pairs) and print the "
             "auto-picked threshold; does NOT modify problems.json",
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="confidence threshold; if not set, --calibrate is run first",
    )
    p.add_argument(
        "--min-threshold",
        type=float,
        default=DEFAULT_MIN_THRESHOLD,
        help=f"floor used when auto-picking threshold (default {DEFAULT_MIN_THRESHOLD})",
    )
    p.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K_CANDIDATES,
        help=f"how many LC candidates to consider per problem (default {DEFAULT_TOP_K_CANDIDATES})",
    )
    p.add_argument(
        "--model",
        default=None,
        help=(
            f"LLM model. If omitted, defaults to {DEFAULT_GEMINI_MODEL} when "
            f"GEMINI_API_KEY is set, else {DEFAULT_GROQ_MODEL}. Override with e.g. "
            f"'llama-3.1-8b-instant' or 'gemini-1.5-pro-latest'."
        ),
    )
    p.add_argument("--limit", type=int, default=None, help="cap problems to process (testing)")
    p.add_argument("--dry-run", action="store_true", help="don't modify problems.json")
    p.add_argument("--headed", action="store_true", help="show Chromium window (debug)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = _parse_args(argv)
    if not PROBLEMS_FILE.exists():
        print(f"! {PROBLEMS_FILE} missing", file=sys.stderr)
        return 1

    # If --model not specified, pick a default based on which key is present.
    # Preference order: NVIDIA (largest free quota for 70B-class models) →
    # Groq (fast but tight TPD) → Gemini (very tight free-tier RPD per project).
    if args.model is None:
        if os.environ.get("NVIDIA_API_KEY"):
            args.model = DEFAULT_NVIDIA_MODEL
        elif os.environ.get("GROQ_API_KEY"):
            args.model = DEFAULT_GROQ_MODEL
        elif os.environ.get("GEMINI_API_KEY"):
            args.model = DEFAULT_GEMINI_MODEL
        else:
            print(
                "! No LLM API key set. Put one of NVIDIA_API_KEY, "
                "GROQ_API_KEY or GEMINI_API_KEY in .env or environment.",
                file=sys.stderr,
            )
            return 1
    print(f"· using model: {args.model} (provider: {_provider_for_model(args.model)})")

    if args.calibrate:
        chosen = run_calibration(
            headless=not args.headed,
            min_threshold=args.min_threshold,
            model=args.model,
        )
        print(f"\n→ use --threshold {chosen:.3f} for the production run")
        return 0

    if args.threshold is None:
        print("· no --threshold given; running calibration first")
        threshold = run_calibration(
            headless=not args.headed,
            min_threshold=args.min_threshold,
            model=args.model,
        )
    else:
        threshold = args.threshold

    run_production(
        threshold=threshold,
        limit=args.limit,
        dry_run=args.dry_run,
        headless=not args.headed,
        top_k=args.top_k,
        model=args.model,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
