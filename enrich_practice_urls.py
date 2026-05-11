"""
Fill missing `practice_url` entries in problems.json by fuzzy-matching the
Striver problem titles against the full LeetCode problem catalogue.

Strategy
--------
1. Fetch every public LeetCode problem in one shot from
   `https://leetcode.com/api/problems/all/` and cache locally
   (`_leetcode_cache.json`). Paid-only problems are dropped.
2. Build a normalised index of LeetCode titles + a few key alias slugs
   (Striver tends to add "I"/"II" suffixes, the word "the", etc.).
3. For every Striver problem with `practice_url == null`:
   - Try exact normalised match first.
   - Fall back to `difflib.SequenceMatcher` with a strict threshold.
   - If accepted, set `practice_url` to the canonical LeetCode URL and
     `practice_url_source = "leetcode-fuzzy"`.
4. For problems whose `practice_url` already exists (from the scrape),
   set `practice_url_source = "takeuforward"`.

The script is idempotent — re-running it never regresses an already-good
link. Use `--refresh-cache` to re-pull the LeetCode catalogue.

Usage
-----
    python enrich_practice_urls.py            # enrich in place
    python enrich_practice_urls.py --dry-run  # show what would change
    python enrich_practice_urls.py --refresh-cache
    python enrich_practice_urls.py --threshold 0.85
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
PROBLEMS_FILE = HERE / "problems.json"
LC_CACHE = HERE / "_leetcode_cache.json"
LC_LIST_URL = "https://leetcode.com/api/problems/all/"
LC_PROBLEM_URL = "https://leetcode.com/problems/{slug}/"

DEFAULT_THRESHOLD = 0.93  # higher = stricter; false positives hurt more than misses

# Filler words that ONLY get stripped during the loose pass (after a strict
# pass has failed). Lets "Find missing number" match "Missing Number" without
# letting "Subsets I" silently collapse onto "Subsets" or "Subsets II".
_FILLER_RE = re.compile(
    r"\b(the|an|a|of|in|on|for|with|from|to|by|find|check|is|are)\b",
    re.IGNORECASE,
)

_ROMAN_TO_INT = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6}

_LC_DIFFICULTY = {1: "Easy", 2: "Medium", 3: "Hard"}


def _normalise(title: str) -> str:
    """Strict normalisation: lowercase, strip punctuation, collapse whitespace.
    Suffixes are PRESERVED — 'Subsets I' stays distinct from 'Subsets II'."""
    s = title.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalise_loose(title: str) -> str:
    """Loose normalisation: strict + filler-word removal. Still PRESERVES
    trailing numeric/roman suffixes."""
    s = _normalise(title)
    s = _FILLER_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _split_suffix(norm: str) -> tuple[str, int | None]:
    """Return (base, suffix_as_int_or_None). Recognises trailing digits and
    roman numerals up to VI."""
    m = re.search(r"^(.+?)\s+(\d+|i{1,3}|iv|v|vi)$", norm)
    if not m:
        return norm, None
    base = m.group(1).strip()
    suf = m.group(2)
    if suf.isdigit():
        return base, int(suf)
    return base, _ROMAN_TO_INT.get(suf)


def _canonical_key(title: str) -> tuple[str, int | None]:
    """Final matching key: (base_after_filler_strip, suffix_number)."""
    return _split_suffix(_normalise_loose(title))


def _suffixes_compatible(a: int | None, b: int | None) -> bool:
    """Striver may write 'X I' while LC writes plain 'X' (both mean variant 1).
    Otherwise suffixes must match exactly. Mismatched non-None suffixes (e.g.
    1 vs 2) are NEVER compatible — that was the 'Subsets I → Subsets II' bug."""
    if a == b:
        return True
    if {a, b} == {None, 1}:
        return True
    return False


def _difficulty_compatible(striver_diff: str | None, lc_level: int | None) -> bool:
    """Reject matches where Striver difficulty and LC difficulty disagree by
    more than one level. None on either side is permissive (don't reject)."""
    if not striver_diff or not lc_level:
        return True
    lc_str = _LC_DIFFICULTY.get(lc_level)
    if not lc_str:
        return True
    order = ["Easy", "Medium", "Hard"]
    try:
        s_idx = order.index(striver_diff)
        l_idx = order.index(lc_str)
    except ValueError:
        return True
    return abs(s_idx - l_idx) <= 1


def _fetch_leetcode(force_refresh: bool = False) -> list[dict[str, Any]]:
    if LC_CACHE.exists() and not force_refresh:
        cached = json.loads(LC_CACHE.read_text(encoding="utf-8"))
        print(f"· loaded {len(cached)} LeetCode problems from cache")
        return cached
    print(f"· fetching LeetCode catalogue from {LC_LIST_URL} (one-time)")
    req = urllib.request.Request(
        LC_LIST_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Referer": "https://leetcode.com/problemset/all/",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.load(resp)
    out: list[dict[str, Any]] = []
    for entry in raw.get("stat_status_pairs", []):
        if entry.get("paid_only"):
            continue
        stat = entry.get("stat", {})
        title = stat.get("question__title")
        slug = stat.get("question__title_slug")
        if not title or not slug:
            continue
        out.append(
            {
                "title": title,
                "slug": slug,
                "id": stat.get("frontend_question_id"),
                "difficulty_level": (entry.get("difficulty") or {}).get("level"),
            }
        )
    LC_CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"· cached {len(out)} LeetCode problems to {LC_CACHE.name}")
    return out


def _build_index(lc_problems: list[dict[str, Any]]) -> dict[tuple[str, int | None], list[dict[str, Any]]]:
    """Return {(base, suffix_num): [lc_problem, ...]} index. Multiple LC
    problems can share a key in rare cases; we store them all so we can pick
    the best by difficulty later."""
    idx: dict[tuple[str, int | None], list[dict[str, Any]]] = {}
    for p in lc_problems:
        key = _canonical_key(p["title"])
        if not key[0]:
            continue
        idx.setdefault(key, []).append(p)
    return idx


def _pick_best_lc(
    candidates: list[dict[str, Any]], striver_diff: str | None
) -> dict[str, Any] | None:
    """Among LC candidates that share a canonical key, prefer one whose
    difficulty matches Striver's, else the lowest LC id (most canonical)."""
    if not candidates:
        return None
    if striver_diff:
        same_diff = [
            c
            for c in candidates
            if _LC_DIFFICULTY.get(c.get("difficulty_level")) == striver_diff
        ]
        if same_diff:
            candidates = same_diff
    try:
        return min(candidates, key=lambda c: int(c.get("id") or 1_000_000))
    except (TypeError, ValueError):
        return candidates[0]


def _best_fuzzy(
    striver_base: str,
    striver_suf: int | None,
    striver_diff: str | None,
    idx_items: list[tuple[tuple[str, int | None], list[dict[str, Any]]]],
    threshold: float,
) -> tuple[dict[str, Any], float] | None:
    """Fuzzy-match on the base name only. The suffix MUST be compatible and
    the difficulty MUST be within one level."""
    sm = SequenceMatcher(None, striver_base, "")
    best: dict[str, Any] | None = None
    best_score = 0.0
    for (cand_base, cand_suf), cands in idx_items:
        if not _suffixes_compatible(striver_suf, cand_suf):
            continue
        sm.set_seq2(cand_base)
        if sm.quick_ratio() < threshold:
            continue
        score = sm.ratio()
        if score <= best_score:
            continue
        winner = _pick_best_lc(cands, striver_diff)
        if winner is None:
            continue
        if not _difficulty_compatible(striver_diff, winner.get("difficulty_level")):
            continue
        best = winner
        best_score = score
        if score == 1.0:
            break
    if best is not None and best_score >= threshold:
        return best, best_score
    return None


def _reset_stale_inferred(p: dict[str, Any]) -> None:
    """If a previous enrichment run wrote an inferred practice_url, clear it
    so this run can re-derive cleanly. Scraped (takeuforward) URLs are
    authoritative and never touched."""
    src = p.get("practice_url_source")
    if src in ("leetcode-exact", "leetcode-fuzzy"):
        p["practice_url"] = None
        p.pop("practice_url_source", None)
        p.pop("practice_url_confidence", None)


def enrich(
    report: dict[str, Any],
    lc_problems: list[dict[str, Any]],
    *,
    threshold: float,
) -> dict[str, Any]:
    idx = _build_index(lc_problems)
    idx_items = list(idx.items())

    stats = {
        "already_scraped": 0,
        "matched_exact": 0,
        "matched_fuzzy": 0,
        "unmatched": 0,
        "total": 0,
    }
    sample_matches: list[tuple[str, str, float, str]] = []
    sample_misses: list[str] = []

    for step in report["steps"]:
        for lec in step["lectures"]:
            for p in lec["problems"]:
                stats["total"] += 1
                _reset_stale_inferred(p)

                if p.get("practice_url"):
                    p["practice_url_source"] = "takeuforward"
                    stats["already_scraped"] += 1
                    continue

                title = p.get("title") or ""
                if not title.strip():
                    p["practice_url_source"] = None
                    stats["unmatched"] += 1
                    continue

                striver_diff = p.get("difficulty")
                s_base, s_suf = _canonical_key(title)
                if not s_base:
                    p["practice_url_source"] = None
                    stats["unmatched"] += 1
                    continue

                # 1) Exact canonical-key match (preserves suffix correctly)
                cands = idx.get((s_base, s_suf), [])
                # Striver may write 'X I' while LC writes 'X' (variant 1) — allow that
                if not cands and s_suf == 1:
                    cands = idx.get((s_base, None), [])
                if not cands and s_suf is None:
                    cands = idx.get((s_base, 1), [])
                winner = _pick_best_lc(cands, striver_diff)
                if winner is not None and _difficulty_compatible(
                    striver_diff, winner.get("difficulty_level")
                ):
                    p["practice_url"] = LC_PROBLEM_URL.format(slug=winner["slug"])
                    p["practice_url_source"] = "leetcode-exact"
                    p["practice_url_confidence"] = 1.0
                    stats["matched_exact"] += 1
                    if len(sample_matches) < 10:
                        sample_matches.append((title, winner["title"], 1.0, "exact"))
                    continue

                # 2) Fuzzy on base, suffix + difficulty validated
                fz = _best_fuzzy(s_base, s_suf, striver_diff, idx_items, threshold)
                if fz is not None:
                    winner, score = fz
                    p["practice_url"] = LC_PROBLEM_URL.format(slug=winner["slug"])
                    p["practice_url_source"] = "leetcode-fuzzy"
                    p["practice_url_confidence"] = round(score, 3)
                    stats["matched_fuzzy"] += 1
                    if len(sample_matches) < 10:
                        sample_matches.append((title, winner["title"], score, "fuzzy"))
                    continue

                # 3) Nothing
                p["practice_url_source"] = None
                stats["unmatched"] += 1
                if len(sample_misses) < 8:
                    sample_misses.append(title)

    report["enrichment"] = {"threshold": threshold, "stats": stats}

    print()
    print("· enrichment stats:")
    for k, v in stats.items():
        print(f"    {k:<17} {v:>4}")

    if sample_matches:
        print("\n· sample matches (sanity-check these):")
        for striver, lc, score, kind in sample_matches:
            print(f"    [{kind:5} {score:.2f}]  {striver!r}  ->  {lc!r}")
    if sample_misses:
        print("\n· sample unmatched:")
        for t in sample_misses:
            print(f"    - {t}")

    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--problems", type=Path, default=PROBLEMS_FILE)
    p.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"fuzzy match threshold 0..1 (default: {DEFAULT_THRESHOLD})",
    )
    p.add_argument(
        "--refresh-cache",
        action="store_true",
        help=f"re-fetch the LeetCode catalogue instead of using {LC_CACHE.name}",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="don't write changes back to problems.json",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.problems.exists():
        print(f"! {args.problems} not found — run scrape_takeuforward.py first", file=sys.stderr)
        return 1
    report = json.loads(args.problems.read_text(encoding="utf-8"))
    lc_problems = _fetch_leetcode(force_refresh=args.refresh_cache)
    enriched = enrich(report, lc_problems, threshold=args.threshold)
    if args.dry_run:
        print("\n· dry-run: not writing changes")
        return 0
    args.problems.write_text(
        json.dumps(enriched, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n· wrote {args.problems}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
