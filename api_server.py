"""
Local API server for the Striver A2Z problems viewer.

Provides two endpoints used by the in-browser "Link problem" and
"Push to GitHub" UI:

  PATCH /api/link
    Body: { "problem_id": <int>, "url": "<str>" }
    • Updates practice_url + practice_url_source = "manual" in problems.json.
    • Regenerates problems.html via generate_html.build().
    • Returns 200 on success or 4xx/5xx with {"error": "..."}.

  POST /api/push
    Body: {} (empty)
    • Runs: git add problems.json problems.html
    • Runs: git commit -m "manual: link <N> problem(s)"
    • Runs: git push origin main
    • Streams stdout+stderr back as plain text.

Run with:
    python api_server.py           (default port 5050)
    python api_server.py --port 8080

CORS is wide-open so the file:// origin of problems.html can reach it.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).parent
PROBLEMS_JSON = HERE / "problems.json"
PROBLEMS_HTML = HERE / "problems.html"

# ── helpers ────────────────────────────────────────────────────────────────


def _load_problems() -> dict:
    return json.loads(PROBLEMS_JSON.read_text(encoding="utf-8"))


def _save_problems(data: dict) -> None:
    PROBLEMS_JSON.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _regen_html() -> None:
    """Re-generate problems.html using generate_html.build()."""
    sys.path.insert(0, str(HERE))
    import generate_html  # noqa: PLC0415  (local import by design)
    page = generate_html.build(PROBLEMS_JSON)
    PROBLEMS_HTML.write_text(page, encoding="utf-8")


def _apply_link(problem_id: int, url: str) -> str | None:
    """
    Set practice_url for the given problem_id.
    Returns an error string on failure, or None on success.
    """
    data = _load_problems()
    found = False
    for step in data.get("steps", []):
        for lec in step.get("lectures", []):
            for prob in lec.get("problems", []):
                if prob.get("id") == problem_id:
                    prob["practice_url"] = url or None
                    prob["practice_url_source"] = "manual" if url else None
                    found = True
    if not found:
        return f"problem_id {problem_id} not found"
    _save_problems(data)
    _regen_html()
    return None


def _git_push() -> tuple[int, str]:
    """
    Stage problems.json + problems.html, commit, push.
    Returns (returncode, combined_output).
    """
    lines: list[str] = []

    def run(cmd: list[str]) -> int:
        result = subprocess.run(
            cmd,
            cwd=HERE,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        lines.append(f"$ {' '.join(cmd)}")
        if result.stdout:
            lines.append(result.stdout.rstrip())
        if result.stderr:
            lines.append(result.stderr.rstrip())
        return result.returncode

    rc = run(["git", "add", "problems.json", "problems.html"])
    if rc != 0:
        return rc, "\n".join(lines)

    # Check if there is actually something to commit
    status = subprocess.run(
        ["git", "status", "--porcelain", "problems.json", "problems.html"],
        cwd=HERE,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if not status.stdout.strip():
        lines.append("Nothing to commit — already up to date.")
        return 0, "\n".join(lines)

    rc = run(["git", "commit", "-m", "manual: update practice links via viewer"])
    if rc != 0:
        return rc, "\n".join(lines)

    rc = run(["git", "push", "origin", "main"])
    return rc, "\n".join(lines)


# ── HTTP handler ───────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    """Minimal JSON/plain-text API handler."""

    def log_message(self, fmt, *args):  # silence default access log
        print(f"  {self.address_string()} — {fmt % args}")

    # ── CORS pre-flight ──────────────────────────────────────────────────

    def do_OPTIONS(self):
        self._send_cors(200, b"")

    # ── routes ───────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path == "/api/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_PATCH(self):
        if self.path != "/api/link":
            self._json(404, {"error": "not found"})
            return
        body = self._read_json()
        if body is None:
            return
        pid = body.get("problem_id")
        url = body.get("url", "").strip()
        if not isinstance(pid, int):
            self._json(400, {"error": "'problem_id' must be an integer"})
            return
        err = _apply_link(pid, url)
        if err:
            self._json(400, {"error": err})
        else:
            self._json(200, {"ok": True})

    def do_POST(self):
        if self.path != "/api/push":
            self._json(404, {"error": "not found"})
            return
        rc, output = _git_push()
        if rc == 0:
            self._json(200, {"ok": True, "output": output})
        else:
            self._json(500, {"error": output})

    # ── helpers ──────────────────────────────────────────────────────────

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            return json.loads(raw)
        except Exception as exc:
            self._json(400, {"error": f"invalid JSON: {exc}"})
            return None

    def _json(self, code: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self._send_cors(code, payload, "application/json")

    def _send_cors(self, code: int, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PATCH, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        if body:
            self.wfile.write(body)


# ── entry point ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=5050, help="Port to listen on (default 5050)")
    args = ap.parse_args(argv)

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"✓ API server running on http://127.0.0.1:{args.port}")
    print(f"  PATCH /api/link   — update a practice URL")
    print(f"  POST  /api/push   — git commit + push origin main")
    print(f"  GET   /api/health — health check")
    print("  Ctrl-C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n· stopped.")


if __name__ == "__main__":
    main()
