from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict


TOOLS_DIR = Path(r"C:\local-agent\Tools")
REGISTRY_PATH = TOOLS_DIR.parent / "tools.json"


def load_registry() -> list[dict]:
    if not REGISTRY_PATH.exists():
        return []
    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_tool_file(tool_ref: str) -> str:
    if tool_ref.lower().endswith(".py"):
        return tool_ref

    for tool in load_registry():
        if tool.get("name") == tool_ref:
            file_name = tool.get("file")
            if file_name:
                return file_name

    raise ValueError(f"Tool not found in registry (and not a .py file): {tool_ref}")


def _extract_last_json(stdout_text: str) -> Dict[str, Any]:
    """
    Extract last JSON object from mixed stdout (logs + JSON).
    Strategy: scan backwards for '{' and try json.loads from there.
    """
    s = stdout_text.strip()
    if not s:
        raise ValueError("Empty stdout")

    # First try whole output
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Scan backwards for a JSON object start
    last_err: Exception | None = None
    for i in range(len(s) - 1, -1, -1):
        if s[i] == "{":
            candidate = s[i:]
            try:
                return json.loads(candidate)
            except Exception as e:
                last_err = e
                continue

    raise ValueError(f"Could not extract JSON from stdout. Last error: {last_err}")


def run_tool(tool_filename: str, input_data: Dict[str, Any]) -> Dict[str, Any]:
    tool_path = TOOLS_DIR / tool_filename
    if not tool_path.exists():
        raise FileNotFoundError(f"Tool not found: {tool_path}")

    proc = subprocess.run(
        [sys.executable, str(tool_path)],
        input=json.dumps(input_data, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=240,
    )

    if proc.returncode != 0:
        raise RuntimeError(f"Tool failed.\nSTDERR:\n{proc.stderr}\nSTDOUT:\n{proc.stdout}")

    try:
        return _extract_last_json(proc.stdout)
    except Exception as e:
        raise RuntimeError(f"Tool returned non-JSON output.\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}") from e


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print("No JSON input provided.", file=sys.stderr)
        return 2

    data = json.loads(raw)
    tool_ref = data["tool"]
    input_data = data.get("input", {})

    tool_file = resolve_tool_file(tool_ref)
    out = run_tool(tool_file, input_data)

    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())