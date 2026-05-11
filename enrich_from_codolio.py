"""
Fill remaining `practice_url == null` problems by cross-referencing Codolio's
community-maintained Striver A2Z mapping
(https://codolio.com/question-tracker/sheet/strivers-a2z-dsa-sheet).

Codolio's public API exposes one canonical URL per problem. Distribution
across platforms (as of the cache):
    leetcode      ~274
    tuf (skipped) ~172
    hackerrank      5
    interviewbit    3
    spoj            1

This script only fills URLs whose Codolio platform is in `EXTERNAL_PLATFORMS`
(LeetCode, GFG, Coding Ninjas, HackerRank, InterviewBit, SPOJ). Codolio
takeuforward URLs are skipped — the user explicitly does NOT want
takeuforward.org as the practice link.

Idempotent — running again after a re-scrape will re-apply matches cleanly.

Usage
-----
    python enrich_from_codolio.py
    python enrich_from_codolio.py --refresh-cache
    python enrich_from_codolio.py --dry-run
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
CACHE = HERE / "_codolio_sheet.json"
API_URL = (
    "https://node.codolio.com/api/question-tracker/v2/sheet/"
    "get-sheet-data-by-slug/strivers-a2z-dsa-sheet"
)

EXTERNAL_PLATFORMS = {
    "leetcode",
    "gfg",
    "geeksforgeeks",
    "codingninjas",
    "hackerrank",
    "interviewbit",
    "spoj",
}

FUZZY_THRESHOLD = 1.01  # default: exact only. Set <1 via --threshold to enable fuzzy.

# Private/contest URLs we never want to surface as practice links
_BAD_URL_RE = re.compile(r"/contests?/", re.IGNORECASE)

# Strip noisy suffix Striver adds like "(DP - 14)", "| (DP - 49)", " - III"
_PAREN_SUFFIX_RE = re.compile(r"[(\[][^)\]]*[)\]]\s*$")
_PIPE_SUFFIX_RE = re.compile(r"\s*\|.*$")
_TRAIL_DASH_RE = re.compile(r"\s*-\s*[ivx0-9]+\s*$", re.IGNORECASE)


def _normalise(title: str) -> str:
    s = title.lower().strip()
    # Drop trailing "(DP - 14)", "(N - 24)", "(DP-26)", etc.
    for _ in range(3):
        prev = s
        s = _PAREN_SUFFIX_RE.sub("", s).strip()
        s = _PIPE_SUFFIX_RE.sub("", s).strip()
        s = _TRAIL_DASH_RE.sub("", s).strip()
        if s == prev:
            break
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fetch_codolio(force_refresh: bool = False) -> dict[str, Any]:
    if CACHE.exists() and not force_refresh:
        print(f"· loaded Codolio sheet from cache ({CACHE.name})")
        return json.loads(CACHE.read_text(encoding="utf-8"))
    print(f"· fetching Codolio sheet from {API_URL}")
    req = urllib.request.Request(
        API_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _build_index(
    mappings: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    """Return (exact-by-normalised-title, list-for-fuzzy)."""
    exact: dict[str, dict[str, Any]] = {}
    fuzzy: list[tuple[str, dict[str, Any]]] = []
    for m in mappings:
        n = _normalise(m.get("title") or "")
        if not n:
            continue
        if n not in exact:
            exact[n] = m
        fuzzy.append((n, m))
    return exact, fuzzy


def _best_fuzzy(
    needle: str,
    candidates: list[tuple[str, dict[str, Any]]],
    threshold: float,
) -> tuple[dict[str, Any], float] | None:
    sm = SequenceMatcher(None, needle, "")
    best: dict[str, Any] | None = None
    best_score = 0.0
    for cand_norm, cand in candidates:
        sm.set_seq2(cand_norm)
        if sm.quick_ratio() < threshold:
            continue
        score = sm.ratio()
        if score > best_score:
            best_score = score
            best = cand
            if score == 1.0:
                break
    if best is not None and best_score >= threshold:
        return best, best_score
    return None


def _reset_stale_codolio(p: dict[str, Any]) -> None:
    """Clear a previously-Codolio-inferred URL so this run can re-apply
    cleanly. Other sources (takeuforward / leetcode-fuzzy) are preserved."""
    if p.get("practice_url_source") in ("codolio-exact", "codolio-fuzzy"):
        p["practice_url"] = None
        p.pop("practice_url_source", None)
        p.pop("practice_url_confidence", None)
        p.pop("practice_url_platform", None)


def enrich(
    report: dict[str, Any],
    codolio_data: dict[str, Any],
    *,
    threshold: float,
) -> dict[str, Any]:
    mappings = codolio_data["data"]["mappings"]
    exact_idx, fuzzy_idx = _build_index(mappings)

    stats = {
        "already_set": 0,
        "matched_exact_codolio": 0,
        "matched_fuzzy_codolio": 0,
        "codolio_tuf_skipped": 0,
        "no_codolio_entry": 0,
        "total": 0,
    }
    samples: list[str] = []

    for step in report["steps"]:
        for lec in step["lectures"]:
            for p in lec["problems"]:
                stats["total"] += 1
                _reset_stale_codolio(p)

                if p.get("practice_url"):
                    stats["already_set"] += 1
                    continue

                title = p.get("title") or ""
                key = _normalise(title)
                if not key:
                    stats["no_codolio_entry"] += 1
                    continue

                match: dict[str, Any] | None = exact_idx.get(key)
                kind = "codolio-exact"
                score: float | None = 1.0
                if match is None:
                    fz = _best_fuzzy(key, fuzzy_idx, threshold)
                    if fz is not None:
                        match, score = fz
                        kind = "codolio-fuzzy"
                if match is None:
                    stats["no_codolio_entry"] += 1
                    continue

                qid = match.get("questionId") or {}
                plat = (qid.get("platform") or "").lower()
                url = qid.get("problemUrl")
                if not url or plat not in EXTERNAL_PLATFORMS:
                    stats["codolio_tuf_skipped"] += 1
                    continue
                if _BAD_URL_RE.search(url):
                    # private contest URLs etc. — reject silently
                    stats["codolio_tuf_skipped"] += 1
                    continue

                p["practice_url"] = url
                p["practice_url_source"] = kind
                p["practice_url_platform"] = plat
                p["practice_url_confidence"] = round(score or 1.0, 3)
                if kind == "codolio-exact":
                    stats["matched_exact_codolio"] += 1
                else:
                    stats["matched_fuzzy_codolio"] += 1
                if len(samples) < 12:
                    samples.append(
                        f"[{kind:14} {score:.2f} {plat}]  {title!r}  ->  {url}"
                    )

    report.setdefault("enrichment", {})
    report["enrichment"]["codolio"] = {
        "threshold": threshold,
        "stats": stats,
    }

    print()
    print("· Codolio enrichment stats:")
    for k, v in stats.items():
        print(f"    {k:<24} {v:>4}")
    if samples:
        print("\n· sample new matches:")
        for s in samples:
            print(f"    {s}")
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--problems", type=Path, default=PROBLEMS_FILE)
    p.add_argument("--refresh-cache", action="store_true")
    p.add_argument("--threshold", type=float, default=FUZZY_THRESHOLD)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.problems.exists():
        print(f"! {args.problems} not found", file=sys.stderr)
        return 1
    report = json.loads(args.problems.read_text(encoding="utf-8"))
    codolio = _fetch_codolio(force_refresh=args.refresh_cache)
    out = enrich(report, codolio, threshold=args.threshold)
    if args.dry_run:
        print("\n· dry-run: not writing changes")
        return 0
    args.problems.write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n· wrote {args.problems}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
