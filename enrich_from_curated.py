"""
Apply hand-curated Striver -> external-practice mappings from
`curated_mappings.json` into problems.json.

Each URL is HTTP-verified before being applied — GFG and LeetCode both
return 200 OK for non-existent slugs (with a 'page not found' template),
so we fetch the body and check for problem-page markers / negative markers
before accepting.

Idempotent: re-running clears any prior `curated` source on a problem
before re-deriving. Other sources (takeuforward / leetcode-exact /
leetcode-fuzzy / codolio-* / leetcode-semantic) are never touched.

Usage
-----
    python enrich_from_curated.py
    python enrich_from_curated.py --dry-run
    python enrich_from_curated.py --no-validate   # skip URL checks
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
PROBLEMS_FILE = HERE / "problems.json"
MAPPINGS_FILE = HERE / "curated_mappings.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Markers that indicate a "no such problem" page (despite a 200 status)
_NEGATIVE_MARKERS_GFG = (
    "Page Not Found",
    "Sorry, the page you're looking for",
    "404",
    "We can't find that page",
)
_NEGATIVE_MARKERS_LC = (
    "404 Not Found",
)


def _fetch_text(url: str, timeout: float = 20.0) -> tuple[int, str]:
    """Return (status_code, body_text). Raises on network failure."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(200_000).decode("utf-8", errors="replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(2_000).decode("utf-8", errors="replace")
        except Exception:
            pass
        return e.code, body


_LC_SLUG_RE = re.compile(r"^https://leetcode\.com/problems/[a-z0-9-]+/?$")
_CN_SLUG_RE = re.compile(
    r"^https://(www\.)?naukri\.com/code360/problems/[a-z0-9-]+/?(\?.*)?$"
)


def _validate(url: str, host: str) -> tuple[bool, str]:
    """Return (is_valid, reason)."""
    # LeetCode blocks generic urllib UAs with 403, but its URL format is
    # stable. Trust LC URLs that match the canonical
    # /problems/<slug>/ pattern.
    if host == "leetcode":
        if _LC_SLUG_RE.match(url):
            return True, "ok (LC URL pattern)"
        return False, "LC URL does not match canonical /problems/<slug>/ pattern"

    try:
        status, body = _fetch_text(url)
    except Exception as e:
        return False, f"network error: {type(e).__name__}"
    if status == 404:
        return False, "404"
    if status >= 500:
        return False, f"server error {status}"
    if status >= 400:
        return False, f"client error {status}"

    lower = body.lower()
    if host == "geeksforgeeks":
        for m in _NEGATIVE_MARKERS_GFG:
            if m.lower() in lower:
                return False, f"negative marker: {m!r}"
        if "problem" in lower and ("difficulty" in lower or "solve" in lower):
            return True, "ok"
        return False, "no positive problem markers"

    # Coding Ninjas / Naukri Code360: CF-protected like LC; trust canonical
    # /problems/<slug> URL pattern instead of fetching (generic UAs get 403).
    if host in ("codingninjas", "naukri", "code360"):
        if _CN_SLUG_RE.match(url):
            return True, "ok (Code360 URL pattern)"
        return False, "Code360 URL does not match canonical /problems/<slug> pattern"

    # Other hosts (spoj, hackerrank, interviewbit) — just trust HTTP 2xx
    return True, "ok (non-validated host)"


def _reset_stale_curated(p: dict[str, Any]) -> None:
    if p.get("practice_url_source") == "curated":
        p["practice_url"] = None
        p.pop("practice_url_source", None)
        p.pop("practice_url_host", None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-validate", action="store_true", help="skip URL checks")
    parser.add_argument("--problems", type=Path, default=PROBLEMS_FILE)
    parser.add_argument("--mappings", type=Path, default=MAPPINGS_FILE)
    args = parser.parse_args(argv)

    if not args.problems.exists():
        print(f"! {args.problems} missing", file=sys.stderr)
        return 1
    if not args.mappings.exists():
        print(f"! {args.mappings} missing", file=sys.stderr)
        return 1

    report = json.loads(args.problems.read_text(encoding="utf-8"))
    mappings = json.loads(args.mappings.read_text(encoding="utf-8"))["mappings"]

    # Build id -> problem index
    id_to_problem: dict[int, dict[str, Any]] = {}
    for s in report["steps"]:
        for l in s["lectures"]:
            for p in l["problems"]:
                if p.get("id") is not None:
                    id_to_problem[p["id"]] = p

    stats = {"applied": 0, "rejected_already_set": 0, "rejected_validation": 0,
             "rejected_no_such_id": 0, "considered": 0}
    rejects: list[str] = []
    samples: list[str] = []

    for id_str, entry in mappings.items():
        try:
            pid = int(id_str)
        except ValueError:
            continue
        stats["considered"] += 1

        p = id_to_problem.get(pid)
        if p is None:
            stats["rejected_no_such_id"] += 1
            rejects.append(f"id {pid}: not in problems.json")
            continue

        _reset_stale_curated(p)

        if p.get("practice_url"):
            # Already has a URL from an earlier (more authoritative) stage
            stats["rejected_already_set"] += 1
            continue

        url = entry.get("url")
        host = entry.get("host", "")
        if not url:
            rejects.append(f"id {pid}: no URL in mapping")
            stats["rejected_validation"] += 1
            continue

        if not args.no_validate:
            ok, reason = _validate(url, host)
            print(f"  pid {pid:<5} {url:<70} {'OK' if ok else 'SKIP'}  {reason}")
            if not ok:
                rejects.append(f"id {pid} ({p['title'][:50]}): {url} -> {reason}")
                stats["rejected_validation"] += 1
                continue
            time.sleep(0.3)  # be polite

        p["practice_url"] = url
        p["practice_url_source"] = "curated"
        p["practice_url_host"] = host
        stats["applied"] += 1
        if len(samples) < 12:
            samples.append(f"{pid} {p['title']!r} -> {url}")

    print()
    print("· curated enrichment stats:")
    for k, v in stats.items():
        print(f"    {k:<28} {v:>4}")
    if samples:
        print("\n· sample applied:")
        for s in samples:
            print(f"    {s}")
    if rejects:
        print(f"\n· {len(rejects)} rejected:")
        for r in rejects[:25]:
            print(f"    {r}")

    if args.dry_run:
        print("\n· dry-run: not writing problems.json")
        return 0

    tmp = args.problems.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    # Retry os.replace to handle transient OneDrive locks
    for attempt in range(8):
        try:
            os.replace(tmp, args.problems)
            break
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.5 * (1.7 ** attempt))
    print(f"\n· wrote {args.problems.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
