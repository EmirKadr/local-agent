from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from jsonschema import ValidationError, validate
except Exception:  # optional fallback
    ValidationError = Exception
    validate = None

TOOLS_DIR = Path(__file__).resolve().parent
REGISTRY_PATH = Path(os.environ.get("TOOLS_JSON_PATH", str(TOOLS_DIR.parent / "tools.json")))
TOOL_TIMEOUT_SECONDS = int(os.environ.get("TOOL_TIMEOUT_SECONDS", "60"))
MAX_STDOUT_CHARS = int(os.environ.get("TOOL_MAX_STDOUT_CHARS", "200000"))


def ok(tool: str, result: Any) -> dict:
    return {"ok": True, "tool": tool, "result": result}


def err(tool: str | None, err_type: str, message: str) -> dict:
    return {
        "ok": False,
        "tool": tool,
        "error": {
            "type": err_type,
            "message": (message or "")[:2000],
        },
    }


def load_registry() -> list[dict]:
    if not REGISTRY_PATH.exists():
        return []
    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def get_tool_spec(tool_name: str) -> dict | None:
    for tool in load_registry():
        if tool.get("name") == tool_name:
            return tool
    return None


def _match_type(value: Any, type_name: str) -> bool:
    return {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
        "null": value is None,
    }.get(type_name, True)


def validate_input(input_data: Any, schema: dict) -> None:
    if not isinstance(input_data, dict):
        raise ValueError("input must be a JSON object")
    if not isinstance(schema, dict):
        raise ValueError("tool has invalid input_schema")

    if validate is not None:
        validate(instance=input_data, schema=schema)
        return

    # Minimal fallback validation (when jsonschema package is unavailable)
    required = schema.get("required", []) if isinstance(schema.get("required"), list) else []
    properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
    additional = schema.get("additionalProperties", True)

    for key in required:
        if key not in input_data:
            raise ValueError(f"missing required field: {key}")

    if additional is False:
        unknown = [k for k in input_data.keys() if k not in properties]
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(unknown)}")

    for key, value in input_data.items():
        spec = properties.get(key)
        if not isinstance(spec, dict):
            continue
        expected_type = spec.get("type")
        if isinstance(expected_type, str) and not _match_type(value, expected_type):
            raise ValueError(f"field '{key}' must be {expected_type}")


def _extract_last_json(stdout_text: str) -> dict[str, Any]:
    s = (stdout_text or "").strip()
    if not s:
        raise ValueError("Empty stdout")

    if len(s) > MAX_STDOUT_CHARS:
        s = s[-MAX_STDOUT_CHARS:]

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    last_err: Exception | None = None
    for i in range(len(s) - 1, -1, -1):
        if s[i] == "{":
            candidate = s[i:]
            try:
                return json.loads(candidate)
            except Exception as e:
                last_err = e
    sample = s[:300].replace("\n", "\\n")
    raise ValueError(f"Could not extract JSON from stdout. Last error: {last_err}. Stdout sample: {sample}")


def run_tool(entrypoint: str, input_data: dict[str, Any]) -> dict[str, Any]:
    tool_path = Path(entrypoint)
    if not tool_path.is_absolute():
        tool_path = TOOLS_DIR.parent / tool_path
    tool_path = tool_path.resolve()

    if not tool_path.exists() or tool_path.suffix.lower() != ".py":
        raise FileNotFoundError(f"Tool entrypoint not found: {tool_path}")

    env = os.environ.copy()
    # Hint tools that support dual CLI/tool mode to force JSON tool behavior.
    env["LOCAL_AGENT_TOOL_MODE"] = "1"

    proc = subprocess.run(
        [sys.executable, str(tool_path)],
        input=json.dumps(input_data, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=TOOL_TIMEOUT_SECONDS,
        env=env,
    )

    if proc.returncode != 0:
        raise RuntimeError(f"Tool failed with code {proc.returncode}. STDERR: {proc.stderr.strip()}")

    return _extract_last_json(proc.stdout)


def main() -> int:
    raw = (sys.stdin.read() or "").strip()

    if not raw:
        out = err(None, "invalid_request", "No JSON input provided")
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        return 0

    tool_name = None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("request must be object")

        tool_name = data.get("tool")
        input_data = data.get("input", {})

        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("tool is required and must be string")

        spec = get_tool_spec(tool_name)
        if not spec:
            out = err(tool_name, "tool_not_found", f"Tool not found in registry: {tool_name}")
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
            return 0

        schema = spec.get("input_schema")
        if not isinstance(schema, dict):
            out = err(tool_name, "invalid_tool_spec", "Tool is missing input_schema")
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
            return 0

        try:
            validate_input(input_data, schema)
        except ValidationError as e:
            out = err(tool_name, "input_validation_error", str(e))
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
            return 0
        except Exception as e:
            out = err(tool_name, "input_validation_error", str(e))
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
            return 0

        entrypoint = spec.get("entrypoint")
        if not isinstance(entrypoint, str) or not entrypoint.strip():
            out = err(tool_name, "invalid_tool_spec", "Tool is missing entrypoint")
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
            return 0

        try:
            result = run_tool(entrypoint, input_data)
        except subprocess.TimeoutExpired:
            out = err(tool_name, "timeout", f"Tool exceeded timeout of {TOOL_TIMEOUT_SECONDS}s")
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
            return 0
        except Exception as e:
            out = err(tool_name, "tool_execution_error", str(e))
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
            return 0

        sys.stdout.write(json.dumps(ok(tool_name, result), ensure_ascii=False))
        return 0

    except Exception as e:
        out = err(tool_name, "invalid_request", str(e))
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
