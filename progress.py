"""
Local progress tracker for the A2Z sheet.

State is stored in `progress.json` (separate from `problems.json` so re-scrapes
never clobber your status). Each entry is keyed by the problem's stable `id`:

    {
      "425": {
        "status": "solved",         # not-started | attempted | solved
        "last_reviewed_at": "2026-05-11T...",
        "notes": "two-pointer trick"
      }
    }

Commands
--------
    python progress.py list [--status STATUS]
    python progress.py mark <id> <status> [--note "..."]
    python progress.py show <id>
    python progress.py stats
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
PROBLEMS_FILE = HERE / "problems.json"
PROGRESS_FILE = HERE / "progress.json"

VALID_STATUSES = {"not-started", "attempted", "solved"}


def _load_problems() -> dict[int, dict[str, Any]]:
    """Flatten problems.json into {id: problem_with_context}."""
    if not PROBLEMS_FILE.exists():
        raise SystemExit(f"{PROBLEMS_FILE} not found — run scrape_takeuforward.py first")
    data = json.loads(PROBLEMS_FILE.read_text(encoding="utf-8"))
    flat: dict[int, dict[str, Any]] = {}
    for step in data["steps"]:
        for lec in step["lectures"]:
            for p in lec["problems"]:
                if p["id"] is None:
                    continue
                flat[p["id"]] = {
                    **p,
                    "step_no": step["step_no"],
                    "step_title": step["step_title"],
                    "lecture_no": lec["lecture_no"],
                    "lecture_title": lec["lecture_title"],
                }
    return flat


def _load_progress() -> dict[str, dict[str, Any]]:
    if not PROGRESS_FILE.exists():
        return {}
    return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))


def _save_progress(state: dict[str, dict[str, Any]]) -> None:
    PROGRESS_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cmd_mark(args: argparse.Namespace) -> int:
    if args.status not in VALID_STATUSES:
        raise SystemExit(f"status must be one of {sorted(VALID_STATUSES)}")
    problems = _load_problems()
    if args.id not in problems:
        raise SystemExit(f"problem id {args.id} not in {PROBLEMS_FILE.name}")
    state = _load_progress()
    entry = state.get(str(args.id), {})
    entry["status"] = args.status
    entry["last_reviewed_at"] = _now()
    if args.note is not None:
        entry["notes"] = args.note
    state[str(args.id)] = entry
    _save_progress(state)
    print(f"· marked {args.id} ({problems[args.id]['title']}) as {args.status}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    problems = _load_problems()
    state = _load_progress()
    if args.id not in problems:
        raise SystemExit(f"problem id {args.id} not in {PROBLEMS_FILE.name}")
    p = problems[args.id]
    s = state.get(str(args.id), {})
    print(f"id: {p['id']}")
    print(f"title: {p['title']}")
    print(f"step: {p['step_no']} {p['step_title']}")
    print(f"lecture: {p['lecture_no']} {p['lecture_title']}")
    print(f"difficulty: {p['difficulty']}")
    print(f"status: {s.get('status', 'not-started')}")
    if s.get("last_reviewed_at"):
        print(f"last_reviewed_at: {s['last_reviewed_at']}")
    if s.get("notes"):
        print(f"notes: {s['notes']}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    problems = _load_problems()
    state = _load_progress()
    rows = []
    for pid, p in problems.items():
        status = state.get(str(pid), {}).get("status", "not-started")
        if args.status and status != args.status:
            continue
        rows.append((pid, status, p["difficulty"] or "?", p["step_no"], p["title"]))
    rows.sort(key=lambda r: (r[3], r[0]))
    for pid, status, diff, step, title in rows:
        print(f"  {pid:>5}  [{status:^11}]  {diff:<7}  step {step:>2}  {title}")
    print(f"\n· {len(rows)} problem(s)")
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    problems = _load_problems()
    state = _load_progress()
    counts = {"not-started": 0, "attempted": 0, "solved": 0}
    for pid in problems:
        s = state.get(str(pid), {}).get("status", "not-started")
        counts[s] = counts.get(s, 0) + 1
    total = sum(counts.values())
    print(f"  total:       {total}")
    for k in ("solved", "attempted", "not-started"):
        v = counts[k]
        pct = (100.0 * v / total) if total else 0.0
        print(f"  {k:<11}: {v:>4}  ({pct:5.1f}%)")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("mark", help="set a problem's status")
    pm.add_argument("id", type=int)
    pm.add_argument("status", help=f"one of {sorted(VALID_STATUSES)}")
    pm.add_argument("--note", help="optional note to attach")
    pm.set_defaults(func=cmd_mark)

    ps = sub.add_parser("show", help="show one problem's status + metadata")
    ps.add_argument("id", type=int)
    ps.set_defaults(func=cmd_show)

    pl = sub.add_parser("list", help="list problems (optionally filtered)")
    pl.add_argument("--status", help="filter by status")
    pl.set_defaults(func=cmd_list)

    pst = sub.add_parser("stats", help="show solved/attempted/remaining counts")
    pst.set_defaults(func=cmd_stats)

    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
