from pathlib import Path
import json

SESSIONS_DIR = Path("sessions")

def _session_path(chat_id: int) -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR / f"{chat_id}.json"

def load_session(chat_id: int):
    path = _session_path(chat_id)
    if not path.exists():
        return {"vars": {}, "last_result": None, "history": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"vars": {}, "last_result": None, "history": []}

def save_session(chat_id: int, session: dict):
    path = _session_path(chat_id)
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

def reset_session(chat_id: int):
    path = _session_path(chat_id)
    if path.exists():
        path.unlink()

def session_summary(session: dict) -> str:
    parts = []
    if session.get("vars"):
        parts.append("Vars: " + ", ".join(session["vars"].keys()))
    if session.get("last_result"):
        parts.append("Last result exists")
    return "\n".join(parts) or "Session is empty."
