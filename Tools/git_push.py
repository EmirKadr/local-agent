"""
git_push.py
-----------
Tool som committar och pushar lokala ändringar till main.
Anropas av agenter när de är klara med att bygga något.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: list[str]) -> tuple[int, str, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT))
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def main() -> int:
    raw = (sys.stdin.read() or "").strip()
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}

    message = (data.get("message") or "").strip() or "Agent: ny kod tillagd"
    files = data.get("files") or []

    # Staging
    if files:
        for f in files:
            code, _, err = _run(["git", "add", str(f)])
            if code != 0:
                print(json.dumps({"ok": False, "error": f"git add {f} misslyckades: {err}"}))
                return 0
    else:
        _run(["git", "add", "-A"])

    # Kolla om det finns något att committa
    code, stdout, _ = _run(["git", "status", "--porcelain"])
    staged = _run(["git", "diff", "--cached", "--name-only"])[1]
    if not staged:
        print(json.dumps({"ok": True, "result": {"status": "nothing_to_commit"}}))
        return 0

    # Commit
    code, _, err = _run(["git", "commit", "-m", message])
    if code != 0:
        print(json.dumps({"ok": False, "error": f"git commit misslyckades: {err}"}))
        return 0

    # Push
    code, out, err = _run(["git", "push", "origin", "main"])
    if code != 0:
        print(json.dumps({"ok": False, "error": f"git push misslyckades: {err}"}))
        return 0

    code2, commit_hash, _ = _run(["git", "rev-parse", "--short", "HEAD"])
    print(json.dumps({
        "ok": True,
        "result": {
            "status": "pushed",
            "commit": commit_hash,
            "message": message,
            "files": staged.splitlines(),
        }
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
