import json
import os
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from session_store import load_session, reset_session, save_session, session_summary

# --- LM Studio ---
LM_STUDIO_BASE = os.environ.get("OPENAI_API_BASE", "http://127.0.0.1:1234/v1")
MODEL_ID = os.environ.get("LM_MODEL", "qwen/qwen3-vl-8b")

# --- Local runner/tooling ---
RUNNER_PATH = Path(os.environ.get("RUNNER_PATH", r"C:\local-agent\Tools\runner.py"))
TOOLS_JSON_PATH = Path(os.environ.get("TOOLS_JSON_PATH", r"C:\local-agent\tools.json"))

MAX_STEPS = int(os.environ.get("MAX_AGENT_STEPS", "8"))
PLAN_RETRIES = int(os.environ.get("PLAN_JSON_RETRIES", "2"))
CHAT_HISTORY_LIMIT = int(os.environ.get("CHAT_HISTORY_LIMIT", "12"))

AGENT_MODE = "agent"
LLM_MODE = "llm"
DEFAULT_AGENT_ENGINE = os.environ.get("DEFAULT_AGENT_ENGINE", "local").strip().lower() or "local"
AGENT_ENGINES = {"local", "autogen"}

AFFIRMATIVE_WORDS = {"ja", "japp", "yes", "ok", "okej", "kör", "go", "retry", "igen"}
NEGATIVE_WORDS = {"nej", "no", "stop", "avbryt", "cancel"}

AGENT_TRIGGER_WORDS = (
    "kör",
    "run",
    "start",
    "script",
    "skript",
    "läs fil",
    "read file",
    "hämta",
    "fetch",
    "databas",
    "database",
)


def _normalize_engine(engine: str) -> str:
    value = (engine or "").strip().lower()
    return value if value in AGENT_ENGINES else ""


def _effective_engine(session: dict) -> str:
    return _normalize_engine(session.get("agent_engine", DEFAULT_AGENT_ENGINE)) or "local"


def _is_affirmative(text: str) -> bool:
    return (text or "").strip().lower() in AFFIRMATIVE_WORDS


def _is_negative(text: str) -> bool:
    return (text or "").strip().lower() in NEGATIVE_WORDS


def _looks_like_kvd_command(text: str) -> bool:
    t = (text or "").strip().lower()
    return bool(t) and "kvd" in t and any(w in t for w in ("kör", "run", "start", "skr"))


def should_activate_agent_mode(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(trigger in t for trigger in AGENT_TRIGGER_WORDS)


def _kvd_input_from_text(text: str) -> dict:
    t = (text or "").lower()
    write_file = any(w in t for w in ("spara", "fil", "write_file=true"))
    headless = not any(w in t for w in ("visa", "browser", "webbläsare", "headless=false"))
    return {"headless": headless, "write_file": write_file}


def split_telegram(text: str, chunk_size: int = 3500):
    for i in range(0, len(text), chunk_size):
        yield text[i : i + chunk_size]


def _compact_json(value, max_len: int = 6000) -> str:
    try:
        s = json.dumps(value, ensure_ascii=False)
    except Exception:
        s = str(value)
    return s if len(s) <= max_len else s[:max_len] + " ...[truncated]"


def summarize_observation(obj: dict, max_items: int = 10) -> dict:
    if not isinstance(obj, dict):
        return {"raw": str(obj)[:1200]}

    out = {"ok": obj.get("ok"), "tool": obj.get("tool")}
    if not obj.get("ok"):
        out["error"] = obj.get("error")
        return out

    result = obj.get("result")
    if isinstance(result, dict):
        summary = {}
        if "items" in result and isinstance(result["items"], list):
            summary["items_count"] = len(result["items"])
            summary["items_top"] = result["items"][:max_items]
        for k in ("out_file", "run_at", "source", "query_url"):
            if k in result:
                summary[k] = result[k]
        if not summary:
            keys = list(result.keys())[:20]
            summary["keys"] = keys
            summary["preview"] = {k: result[k] for k in keys[:8]}
        out["result_summary"] = summary
    elif isinstance(result, list):
        out["result_summary"] = {"count": len(result), "top": result[:max_items]}
    else:
        out["result_summary"] = result
    return out


def list_tools_from_json() -> list[dict]:
    if not TOOLS_JSON_PATH.exists():
        return []
    return json.loads(TOOLS_JSON_PATH.read_text(encoding="utf-8"))


def tool_index_for_prompt(tools: list[dict]) -> list[dict]:
    short = []
    for t in tools:
        schema = t.get("input_schema", {}) if isinstance(t.get("input_schema"), dict) else {}
        props = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
        short.append(
            {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "required": schema.get("required", []),
                "fields": {k: (v.get("type") if isinstance(v, dict) else "any") for k, v in list(props.items())[:12]},
                "example": (t.get("examples") or [{}])[0],
            }
        )
    return short


def _extract_first_json_object(text: str) -> dict:
    s = (text or "").strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found")
    return json.loads(s[start : end + 1])


def lm_chat(messages: list[dict], temperature: float = 0.2) -> str:
    r = requests.post(
        f"{LM_STUDIO_BASE}/chat/completions",
        json={"model": MODEL_ID, "messages": messages, "temperature": temperature},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def plan_next_action(*, user_text: str, tools: list[dict], session: dict) -> dict:
    tools_index = tool_index_for_prompt(tools)
    vars_summary = {k: type(v).__name__ for k, v in session.get("vars", {}).items()}
    latest_obs = next((h for h in reversed(session.get("history", [])) if h.get("role") == "observation"), None)

    system = (
        "Du är planner för en lokal AI-agent. Return ONLY valid JSON.\n"
        "Kontrakt:\n"
        '1) {"action":"run","tool":"TOOL_NAME","input":{},"save_as":"optional","note":"..."}\n'
        '2) {"action":"final","answer":"...","citations":["..."]}\n'
        '3) {"action":"ask","question":"...","choices":["..."]}\n'
        "Regler:\n"
        "- action måste vara run|final|ask.\n"
        "- Använd endast tool-namn från listan.\n"
        "- När observation finns måste nästa steg baseras på den.\n"
        "- Om uppgiften är klar: action=final.\n"
        "- Svara ENDAST JSON, ingen markdown/text runtom.\n"
        "- Om user uttryckligen säger kör/run/start för ett känt verktyg, returnera action=run direkt.\n"
    )

    user_payload = {
        "goal": user_text,
        "step": session.get("step", 0),
        "vars_summary": vars_summary,
        "latest_observation": latest_obs.get("content") if isinstance(latest_obs, dict) else None,
        "tools_index": tools_index,
    }

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _compact_json(user_payload, max_len=7000)},
    ]

    last_error = None
    for retry in range(PLAN_RETRIES + 1):
        content = lm_chat(messages, temperature=0.0)
        try:
            plan = _extract_first_json_object(content)
            action = plan.get("action")
            if action not in ("run", "final", "ask"):
                raise ValueError("Invalid action")
            if action == "run":
                if not isinstance(plan.get("tool"), str):
                    raise ValueError("run requires tool")
                if not isinstance(plan.get("input", {}), dict):
                    plan["input"] = {}
            if action == "ask" and not isinstance(plan.get("question"), str):
                raise ValueError("ask requires question")
            if action == "final" and not isinstance(plan.get("answer"), str):
                raise ValueError("final requires answer")
            return plan
        except Exception as e:
            last_error = str(e)
            messages.extend(
                [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": "Your previous output was invalid. Return ONLY valid JSON with contract."},
                ]
            )
            if retry >= PLAN_RETRIES:
                break

    return {"action": "ask", "question": f"Planner JSON-fel: {last_error}. Kan du omformulera uppgiften?", "choices": []}


def call_runner(payload: dict) -> dict:
    if not RUNNER_PATH.exists():
        return {
            "ok": False,
            "tool": payload.get("tool"),
            "error": {"type": "runner_missing", "message": f"runner.py hittades inte: {RUNNER_PATH}"},
        }

    proc = subprocess.run(
        [sys.executable, str(RUNNER_PATH)],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=240,
    )

    if proc.stdout.strip():
        try:
            return json.loads(proc.stdout)
        except Exception:
            pass

    msg = proc.stderr.strip() or proc.stdout.strip() or "Runner returned no JSON"
    return {"ok": False, "tool": payload.get("tool"), "error": {"type": "runner_error", "message": msg[:1200]}}


def execute_tool(tool: str, tool_input: dict) -> dict:
    return call_runner({"tool": tool, "input": tool_input})


def _history_to_chat_messages(session: dict) -> list[dict]:
    out = [
        {
            "role": "system",
            "content": (
                "Du har två lägen: LLM-LÄGE (default) och AGENTLÄGE (vid behov). "
                "I LLM-LÄGE svarar du direkt, kort och konkret på svenska utan verktyg eller meta-frågor. "
                "Ställ bara följdfråga om svaret annars blir meningslöst."
            ),
        }
    ]
    for h in session.get("history", [])[-CHAT_HISTORY_LIMIT:]:
        role = h.get("role")
        if role in ("user", "assistant") and isinstance(h.get("content"), str):
            out.append({"role": role, "content": h["content"]})
    return out


def _autogen_available() -> bool:
    return find_spec("autogen_agentchat") is not None and find_spec("autogen_ext") is not None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "OK. Default-läge: vanlig LLM-chat.\n"
        "• /agent => växla till agentläge (plan/run/observation/final)\n"
        "• /llm => växla tillbaka till vanlig LLM\n"
        "• /mode visar nuvarande läge\n"
        "• /engine [local|autogen] visar/sätter agent-engine\n"
        "• /run {JSON} kör valfritt registry-tool via runner\n"
        "• /tools listar tools från tools.json\n"
        "• /vars visar session-vars\n"
        "• /reset rensar session\n"
    )


async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = load_session(update.effective_chat.id)
    await update.message.reply_text(
        f"Läge: {session.get('mode', LLM_MODE)}\nAgent-engine: {_effective_engine(session)}"
    )


async def engine_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = load_session(chat_id)

    if not context.args:
        await update.message.reply_text(
            f"Agent-engine: {_effective_engine(session)}\nTillgängliga: {', '.join(sorted(AGENT_ENGINES))}"
        )
        return

    requested = _normalize_engine(context.args[0])
    if not requested:
        await update.message.reply_text(
            f"Ogiltig engine. Använd: {', '.join(sorted(AGENT_ENGINES))}"
        )
        return

    if requested == "autogen" and not _autogen_available():
        await update.message.reply_text(
            "autogen-engine vald men paket saknas i miljön.\n"
            "Installera autogen-agentchat + autogen-ext eller kör /engine local."
        )

    session["agent_engine"] = requested
    save_session(chat_id, session)
    await update.message.reply_text(f"Agent-engine satt till: {requested}")


async def agent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = load_session(chat_id)
    session["mode"] = AGENT_MODE
    session.setdefault("agent_engine", DEFAULT_AGENT_ENGINE)
    save_session(chat_id, session)
    await update.message.reply_text(f"Agentläge aktiverat. Engine: {_effective_engine(session)}")


async def llm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = load_session(chat_id)
    session["mode"] = LLM_MODE
    session.pop("pending", None)
    save_session(chat_id, session)
    await update.message.reply_text("LLM-läge aktiverat.")


async def tools_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tools = list_tools_from_json()
        if not tools:
            await update.message.reply_text(f"Inga tools hittades i {TOOLS_JSON_PATH}")
            return

        lines = ["Tools:"]
        for t in tools:
            req = (t.get("input_schema", {}) or {}).get("required", [])
            lines.append(f"- {t.get('name', '?')}: {t.get('description', '')} (required: {', '.join(req) or '-'})")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Fel: {e}")


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    parts = raw.split(" ", 1)
    if len(parts) < 2:
        await update.message.reply_text('Usage: /run {"tool":"kvd_scraper","input":{...}}')
        return

    try:
        payload = json.loads(parts[1])
    except Exception as e:
        await update.message.reply_text(f"Ogiltig JSON: {e}")
        return

    await update.message.reply_text("Kör tool...")
    result = call_runner(payload)
    for part in split_telegram(_compact_json(summarize_observation(result), max_len=3200)):
        await update.message.reply_text(part)


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_session(update.effective_chat.id)
    await update.message.reply_text("Session reset.")


async def vars_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(session_summary(load_session(update.effective_chat.id)))


async def _handle_llm_mode(update: Update, text: str, session: dict):
    session["history"].append({"role": "user", "content": text})
    messages = _history_to_chat_messages(session)
    answer = lm_chat(messages, temperature=0.4)
    session["history"].append({"role": "assistant", "content": answer})
    session["history"] = session["history"][-40:]
    save_session(update.effective_chat.id, session)
    for part in split_telegram(answer):
        await update.message.reply_text(part)


async def _handle_autogen_mode(update: Update, text: str, session: dict) -> bool:
    if not _autogen_available():
        await update.message.reply_text(
            "Autogen-engine är vald men paketen saknas. Faller tillbaka till local-engine för detta meddelande."
        )
        return False

    await update.message.reply_text(
        "Autogen-engine är förberedd men ännu inte implementerad i detalj. Faller tillbaka till local-engine."
    )
    return False


async def _handle_local_agent_mode(update: Update, text: str, session: dict):
    chat_id = update.effective_chat.id
    session["history"].append({"role": "user", "content": text})
    tools = list_tools_from_json()

    if _looks_like_kvd_command(text):
        direct_input = _kvd_input_from_text(text)
        await update.message.reply_text(f"Steg 1/{MAX_STEPS}: kör kvd_scraper")
        run_result = execute_tool("kvd_scraper", direct_input)
        obs = summarize_observation(run_result)
        session["last_tool"] = {
            "tool": "kvd_scraper",
            "input": direct_input,
            "result": run_result.get("result") if isinstance(run_result, dict) else None,
            "ok": bool(run_result.get("ok")) if isinstance(run_result, dict) else False,
        }
        session["history"].append({"role": "observation", "content": obs})
        session["history"] = session["history"][-40:]
        save_session(chat_id, session)
        await update.message.reply_text(f"Observation:\n{_compact_json(obs, max_len=2000)}")
        return

    for step in range(1, MAX_STEPS + 1):
        session["step"] = step
        save_session(chat_id, session)

        plan = plan_next_action(user_text=text, tools=tools, session=session)
        session["history"].append({"role": "assistant", "content": _compact_json(plan, 2000)})
        action = plan.get("action")

        if action == "ask":
            await update.message.reply_text(plan.get("question", "Jag behöver mer info."))
            save_session(chat_id, session)
            return

        if action == "final":
            answer = plan.get("answer", "Klart.")
            for part in split_telegram(answer):
                await update.message.reply_text(part)
            save_session(chat_id, session)
            return

        if action != "run":
            await update.message.reply_text("Planner returnerade okänd action. Försök igen.")
            save_session(chat_id, session)
            return

        tool = plan.get("tool")
        tool_input = plan.get("input", {})
        await update.message.reply_text(f"Steg {step}/{MAX_STEPS}: kör {tool}")

        run_result = execute_tool(tool, tool_input)
        obs = summarize_observation(run_result)
        session["last_tool"] = {
            "tool": tool,
            "input": tool_input,
            "result": run_result.get("result") if isinstance(run_result, dict) else None,
            "ok": bool(run_result.get("ok")) if isinstance(run_result, dict) else False,
        }

        if isinstance(run_result, dict) and run_result.get("ok") and isinstance(plan.get("save_as"), str) and plan.get("save_as"):
            session["vars"][plan["save_as"]] = run_result.get("result")

        session["history"].append({"role": "observation", "content": obs})
        session["history"] = session["history"][-40:]
        save_session(chat_id, session)
        await update.message.reply_text(f"Observation:\n{_compact_json(obs, max_len=2000)}")

    await update.message.reply_text("Jag nådde max steg utan final.")


async def _handle_agent_mode(update: Update, text: str, session: dict):
    if _is_negative(text):
        session["history"].append({"role": "user", "content": text})
        session["history"].append({"role": "assistant", "content": "Okej, då stannar vi här."})
        session["history"] = session["history"][-40:]
        save_session(update.effective_chat.id, session)
        await update.message.reply_text("Okej, då stannar vi här.")
        return

    engine = _effective_engine(session)
    if engine == "autogen":
        handled = await _handle_autogen_mode(update, text, session)
        if handled:
            return
    await _handle_local_agent_mode(update, text, session)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    session = load_session(chat_id)

    session.setdefault("history", [])
    session.setdefault("vars", {})
    session.setdefault("last_tool", None)
    session.setdefault("step", 0)
    session.setdefault("mode", LLM_MODE)
    session.setdefault("agent_engine", DEFAULT_AGENT_ENGINE)

    active_mode = session.get("mode")
    if active_mode == LLM_MODE and should_activate_agent_mode(text):
        await update.message.reply_text("Jag hämtar/beräknar detta.")
        await _handle_agent_mode(update, text, session)
        return

    if active_mode == AGENT_MODE:
        await _handle_agent_mode(update, text, session)
    else:
        await _handle_llm_mode(update, text, session)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Sätt TELEGRAM_BOT_TOKEN som miljövariabel.")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("engine", engine_cmd))
    app.add_handler(CommandHandler("agent", agent_cmd))
    app.add_handler(CommandHandler("llm", llm_cmd))
    app.add_handler(CommandHandler("tools", tools_cmd))
    app.add_handler(CommandHandler("run", run_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("vars", vars_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.run_polling()


if __name__ == "__main__":
    main()
