from pathlib import Path
import json

SESSIONS_DIR = Path("sessions")


def _default_session() -> dict:
    return {
        "history": [],
        "vars": {},
        "last_tool": None,
        "step": 0,
        "pending": None,
        "mode": "llm",
    }


def _session_path(chat_id: int) -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR / f"{chat_id}.json"


def load_session(chat_id: int):
    path = _session_path(chat_id)
    if not path.exists():
        return _default_session()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _default_session()

    base = _default_session()
    if isinstance(data, dict):
        base.update(data)
    return base


def save_session(chat_id: int, session: dict):
    path = _session_path(chat_id)
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def reset_session(chat_id: int):
    path = _session_path(chat_id)
    if path.exists():
        path.unlink()


def session_summary(session: dict) -> str:
    vars_keys = list((session or {}).get("vars", {}).keys())
    last_tool = ((session or {}).get("last_tool") or {}).get("tool") if isinstance((session or {}).get("last_tool"), dict) else None
    history_len = len((session or {}).get("history", []))
    step = (session or {}).get("step", 0)

    parts = [f"step={step}", f"history={history_len}"]
    parts.append(f"mode={(session or {}).get('mode', 'llm')}")
    parts.append("vars=" + (", ".join(vars_keys) if vars_keys else "-"))
    parts.append(f"last_tool={last_tool or '-'}")
    if (session or {}).get("pending"):
        parts.append("pending=yes")
    return "\n".join(parts)
