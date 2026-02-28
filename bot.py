import asyncio
import json
import os
import re
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
MODEL_ID = os.environ.get("LM_MODEL", "meta-llama/llama-3.3-70b-instruct")

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

# --- Scraper Factory ---
_TOOLS_DIR = Path(__file__).parent / "Tools"
# --- Direkta fetch-anrop: kör befintliga verktyg utan agent_team/scraper_factory ---
_BUILD_VERBS = ("bygg", "skapa", "implementera", "utveckla", "skriv kod", "generera")

DIRECT_KVD_TRIGGERS = ("kvd", "kvdbil", "kvd.se")
DIRECT_BLOCKET_TRIGGERS = (
    # generellt – fungerar som "kvd" för KVD
    "blocket",
    # explicita kommandon
    "blocket scraper", "blocket scrapern",
    "använd blocket", "kör blocket",
    "starta blocket", "hämta blocket",
    # beskrivande fraser
    "bilannonser från blocket", "bilar från blocket", "annonser från blocket",
    "blocket bilar", "blocket bil", "blocket.se/bilar",
    "sök på blocket", "hitta på blocket",
    "leta på blocket", "kolla blocket",
    "från blocket", "via blocket",
)

def _is_direct_kvd_fetch(text: str) -> bool:
    t = text.lower()
    return any(tr in t for tr in DIRECT_KVD_TRIGGERS) and not any(v in t for v in _BUILD_VERBS)

def _is_direct_blocket_fetch(text: str) -> bool:
    t = text.lower()
    return any(tr in t for tr in DIRECT_BLOCKET_TRIGGERS) and not any(v in t for v in _BUILD_VERBS)


# --- KVD: naturlig-språk-parsning av filter (deadline, märke, drivmedel, etc.) ---
# Ordningen spelar roll: mer specifikt (ikväll) måste komma före generellt (idag)
_KVD_DEADLINE_MAP: list[tuple[str, set]] = [
    ("imorgon",      {"Imorgon"}),
    ("morgondagens", {"Imorgon"}),
    ("morgon",       {"Imorgon"}),
    ("nästa dag",    {"Imorgon"}),
    ("ikväll",       {"Ikväll"}),
    ("kvällens",     {"Ikväll"}),
    ("kväll",        {"Ikväll"}),
    ("idag",         {"Idag", "Ikväll"}),
    ("dagens",       {"Idag", "Ikväll"}),
    ("denna dag",    {"Idag", "Ikväll"}),
    ("igår",         {"Igår"}),
    ("gårdagens",    {"Igår"}),
    ("gårdag",       {"Igår"}),
]

_KVD_FUEL_MAP: list[tuple[str, str]] = [
    ("diesel",     "Diesel"),
    ("bensin",     "Bensin"),
    ("elbil",      "El"),
    ("elektrisk",  "El"),
    (" el ",       "El"),
    ("hybrid",     "Hybrid"),
]

_KVD_BRAND_SET = {
    "audi", "bmw", "ford", "hyundai", "mazda", "mercedes",
    "nissan", "opel", "peugeot", "porsche", "renault", "seat",
    "skoda", "subaru", "tesla", "toyota", "volkswagen", "volvo",
}

_KVD_AUCTION_MAP: list[tuple[str, str]] = [
    ("fast pris",  "BUY_NOW"),
    ("köp nu",     "BUY_NOW"),
    ("buy now",    "BUY_NOW"),
    ("budgivning", "BIDDING"),
    ("bidding",    "BIDDING"),
    (" bud ",      "BIDDING"),
]

_KVD_GEAR_MAP: list[tuple[str, str]] = [
    ("manuell",    "Manuell"),
    ("automat",    "Automat"),
    ("automatisk", "Automat"),
]


def _parse_kvd_input(text: str) -> dict:
    """Parsar naturlig-språk-filter från ett KVD-meddelande.

    Returnerar en dict lämplig att skicka som tool_input till kvd_scraper,
    med optional-nycklarna: wanted_deadlines (list[str]) och url (str).
    """
    from urllib.parse import urlencode

    # Padda med mellanslag för enkel ordsökning
    t = " " + text.lower() + " "
    result: dict = {}

    # --- Deadline ---
    wanted: set[str] = set()
    for kw, deadlines in _KVD_DEADLINE_MAP:
        if kw in t:
            wanted |= deadlines
            break  # första träff vinner
    if wanted:
        result["wanted_deadlines"] = list(wanted)

    # --- URL-params ---
    params: list[tuple[str, str]] = [("orderBy", "countdown_start_at")]

    for kw, fuel in _KVD_FUEL_MAP:
        if kw in t:
            params.append(("fuel", fuel))
            break

    for brand in _KVD_BRAND_SET:
        if f" {brand} " in t:
            params.append(("brand", brand.capitalize()))

    for kw, atype in _KVD_AUCTION_MAP:
        if kw in t:
            params.append(("auctionType", atype))
            break

    for kw, gear in _KVD_GEAR_MAP:
        if kw in t:
            params.append(("gearbox", gear))
            break

    # Skicka bara url om vi faktiskt har extra filter (mer än bara orderBy)
    if len(params) > 1:
        result["url"] = "https://www.kvd.se/begagnade-bilar?" + urlencode(params)

    return result


SCRAPE_BUILD_TRIGGERS = (
    "bygg scraper",
    "skapa scraper",
    "bygg ett skript",
    "bygg skript",
    "skriv kod",
    "skriv ett skript",
    "kod som hämtar",
    "kod som scrapar",
    "skript som hämtar",
    "skript som scrapar",
    "scrapa",
    "scrapa sidan",
    "hämta data från",
    "hämta information från",
    "gå in på",
)

# --- Agent Team (Micke + Zack + Johan) ---
FEAT_TRIGGERS = (
    "bygg en app",
    "bygg ett verktyg",
    "skapa en app",
    "skapa ett verktyg",
    "implementera",
    "utveckla",
    "feat:",
    "feature:",
    "ny feature",
    "ny funktion",
)

_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+|"
    r"(?<!\w)(?:[a-zA-Z0-9-]+\.)+(?:se|com|org|net|io|dev|fi|no|dk|nu|app|ai)[^\s\"'<>]*"
)

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
    "ta fram",
    "hitta",
    "sök",
    "jämför",
    "visa mig",
    "lista",
    "räkna",
    "beräkna",
    "analysera",
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


def _has_run_intent(text: str) -> bool:
    t = (text or "").strip().lower()
    return bool(t) and any(w in t for w in ("kör", "run", "start", "skr"))


def _build_default_input(schema: dict) -> dict:
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
    defaults = {}
    for key, prop in properties.items():
        if isinstance(prop, dict) and "default" in prop:
            defaults[key] = prop["default"]
    return defaults


def _extract_direct_tool_call(text: str, tools: list[dict]) -> tuple[str, dict] | None:
    t = (text or "").strip().lower()
    if not t or not _has_run_intent(t):
        return None

    for tool in tools:
        name = (tool.get("name") or "").strip()
        if not name:
            continue
        if name.lower() in t:
            schema = tool.get("input_schema", {}) if isinstance(tool.get("input_schema"), dict) else {}
            direct_input = _build_default_input(schema)

            if "headless" in direct_input:
                if any(w in t for w in ("visa", "browser", "webbläsare", "headless=false")):
                    direct_input["headless"] = False
                if "headless=true" in t:
                    direct_input["headless"] = True

            if "write_file" in direct_input:
                if any(w in t for w in ("spara", "fil", "write_file=true")):
                    direct_input["write_file"] = True
                if "write_file=false" in t:
                    direct_input["write_file"] = False

            return name, direct_input
    return None


def _extract_url(text: str) -> str | None:
    """Extrahera första URL ur texten."""
    m = _URL_RE.search(text or "")
    if not m:
        return None
    url = m.group(0).rstrip(".,!?):")
    if not url.startswith("http"):
        url = "https://" + url
    return url


def _is_scraper_build_request(text: str) -> bool:
    """Returnerar True om meddelandet handlar om att bygga/skriva en scraper."""
    t = (text or "").strip().lower()
    return any(trigger in t for trigger in SCRAPE_BUILD_TRIGGERS)


def _format_build_result(result: dict) -> str:
    """Formaterar scraper_factory-resultatet för Telegram."""
    status_emoji = "✓" if result["status"] == "approved" else "~"
    lines = [
        f"[{status_emoji}] Scraper-bygge klart",
        f"URL: {result['url']}",
        f"Status: {result['status']}",
        f"Iterationer: {result['iterations']}",
        f"Slutpoäng: {result['final_score']}/10",
    ]

    if result.get("out_file"):
        lines.append(f"Sparad: {result['out_file']}")

    # Loggsummering
    events = [e["event"] for e in result.get("log", [])]
    lines.append(f"\nLogg ({len(events)} haendelser):")
    for entry in result.get("log", []):
        ev = entry["event"]
        t = entry["time"][11:19]
        if ev in ("coder_done", "reviewer_done", "sandbox_run_done",
                   "loop_approved", "loop_revise", "done", "web_inspector_done"):
            extra = {k: v for k, v in entry.items()
                     if k not in ("time", "event") and v is not None}
            extra_str = "  ".join(
                f"{k}={v}" for k, v in list(extra.items())[:4]
                if not isinstance(v, (list, dict))
            )
            lines.append(f"  [{t}] {ev}  {extra_str}")

    return "\n".join(lines)


async def _handle_scrape_build(update: Update, url: str, task: str):
    """Kör scraper_factory asynkront och svarar i Telegram."""
    await update.message.reply_text(
        f"Startar multi-agent scraper-bygge...\n"
        f"URL: {url}\n"
        f"Uppgift: {task[:200]}\n\n"
        "CoderAgent + ReviewerAgent arbetar. Det tar 1-3 minuter."
    )

    # Importera scraper_factory lazily för att undvika startup-delay
    sys.path.insert(0, str(_TOOLS_DIR))
    import scraper_factory

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: scraper_factory.run(url=url, task=task, max_iterations=3, write_file=True),
        )
    except Exception as e:
        await update.message.reply_text(f"Fel vid scraper-bygge: {e}")
        return

    summary = _format_build_result(result)
    for part in split_telegram(summary):
        await update.message.reply_text(part)

    # Skicka slutkoden separat om den finns
    if result.get("final_code"):
        code_msg = f"```python\n{result['final_code'][:3800]}\n```"
        try:
            await update.message.reply_text(code_msg, parse_mode="Markdown")
        except Exception:
            for part in split_telegram(result["final_code"]):
                await update.message.reply_text(part)


async def _handle_direct_fetch(update: Update, tool_name: str, tool_input: dict):
    """Kör ett befintligt datahämtningsverktyg direkt – ingen agent_team behövs."""
    if tool_name == "kvd_scraper":
        dl = tool_input.get("wanted_deadlines")
        label = "/".join(dl) if dl else "alla"
        await update.message.reply_text(f"Hämtar KVD-auktioner ({label})...")
    else:
        await update.message.reply_text(f"Hämtar data med {tool_name}...")
    run_result = execute_tool(tool_name, tool_input)
    if not isinstance(run_result, dict) or not run_result.get("ok"):
        err = (run_result or {}).get("error", {})
        await update.message.reply_text(f"Fel: {err.get('message', str(run_result))[:500]}")
        return

    result = run_result.get("result", {})
    items  = result.get("items", [])
    source = result.get("source", tool_name)
    lines  = [f"{len(items)} objekt från {source} ({result.get('run_at', '')[:16]}):"]
    for item in items[:15]:
        title = item.get("title") or item.get("name") or "?"
        price = item.get("price_str") or item.get("leading_bid") or item.get("price") or "–"
        url   = item.get("url") or item.get("link") or ""
        lines.append(f"\n• {title}  |  {price}")
        if url:
            lines.append(f"  {url}")
    for part in split_telegram("\n".join(lines)):
        await update.message.reply_text(part)


def should_activate_agent_mode(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(trigger in t for trigger in AGENT_TRIGGER_WORDS)


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
        "OK. Default-läge: vanlig LLM-chat.\n\n"
        "AGENT-TEAM (Micke + Zack + Johan):\n"
        "• /feat [url] <uppgift> – full SPEC→IMPL→TEST→REVIEW-loop\n"
        "  Exempel: /feat blocket.se Bygg scraper för bilannonser\n"
        "  Eller skriv: 'bygg en app som...' / 'implementera...'\n\n"
        "SCRAPER-BYGGE (snabb, utan spec):\n"
        "• /build <url> [uppgift] – CoderAgent + ReviewerAgent\n"
        "  Exempel: /build blocket.se/bilar hämta annonser med pris\n"
        "  Eller skriv: 'bygg scraper för blocket.se som hämtar...'\n\n"
        "AGENTLÄGE:\n"
        "• /agent => växla till agentläge\n"
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
        await update.message.reply_text('Usage: /run {"tool":"tool_name","input":{...}}')
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


def _is_feat_request(text: str) -> bool:
    """Returnerar True om meddelandet handlar om att bygga en ny feature/app (utan URL-krav)."""
    t = (text or "").strip().lower()
    return any(trigger in t for trigger in FEAT_TRIGGERS)


def _format_feat_result(result: dict) -> str:
    """Formaterar agent_team-resultatet för Telegram."""
    verdict_sym = "✓" if result["status"] == "approved" else "~"
    lines = [
        f"[{verdict_sym}] Agent-team klart — {result['feat_id']}",
        f"Status   : {result['status']}",
        f"Cyklar   : {result['cycles']}",
        f"Mapp     : {result['project_path']}",
    ]

    # Uppgift vs resultat per cykel
    cycle_summaries = result.get("cycle_summaries", [])
    if cycle_summaries:
        lines.append("\nResultat per cykel:")
        for cs in cycle_summaries:
            passed = cs.get("passed", 0)
            total = cs.get("total", 0)
            bugs = cs.get("bugs", [])
            blockers = [b for b in bugs if b.get("severity") == "blocker"]
            v = cs.get("verdict", "?")
            v_sym = "✓" if v == "approve" else "~"
            lines.append(f"  Cykel {cs['cycle']}: {passed}/{total} PASS  {len(bugs)} buggar ({len(blockers)} blocker)  [{v_sym}] {v}")
            for rc in cs.get("required_changes", [])[:3]:
                lines.append(f"    → {rc}")

        # AC-jämförelse från sista cykeln
        final = cycle_summaries[-1]
        approved_ac = final.get("approved_ac", [])
        failed_ac = final.get("failed_ac", [])
        if approved_ac or failed_ac:
            lines.append("\nAcceptance Criteria:")
            for ac in approved_ac:
                lines.append(f"  [✓] {ac}")
            for ac in failed_ac:
                lines.append(f"  [✗] {ac}")

    if result.get("required_changes") and result["status"] != "approved":
        lines.append("\nÅterstår att fixa:")
        for c in result["required_changes"][:5]:
            lines.append(f"  - {c}")

    return "\n".join(lines)


async def _handle_feat(update: Update, task: str, url: str | None = None):
    """Kör agent_team asynkront (Micke + Zack + Johan) och svarar i Telegram."""
    msg = await update.message.reply_text(
        f"Startar agent-team...\n"
        f"Uppgift: {task[:200]}\n\n"
        f"Micke skriver spec → Zack bygger → Johan testar → Micke reviewar.\n"
        f"Det tar 3-8 minuter beroende på uppgiften."
    )

    sys.path.insert(0, str(_TOOLS_DIR))
    import agent_team

    # Progress-callback som skickar uppdateringar till Telegram
    sent_steps: list[str] = []

    def progress_cb(step_msg: str):
        sent_steps.append(step_msg)

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: agent_team.run(task=task, url=url, max_cycles=2, progress_cb=progress_cb),
        )
    except Exception as e:
        await update.message.reply_text(f"Fel vid agent-körning: {e}")
        return

    summary = _format_feat_result(result)
    for part in split_telegram(summary):
        await update.message.reply_text(part)

    # Skicka main.py om den finns
    src_main = result.get("src_files", {}).get("main.py", "")
    if src_main:
        code_msg = f"```python\n{src_main[:3800]}\n```"
        try:
            await update.message.reply_text(code_msg, parse_mode="Markdown")
        except Exception:
            for part in split_telegram(src_main):
                await update.message.reply_text(part)


async def build_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /build <url> [task description]
    Exempel: /build https://blocket.se/bilar hämta alla bilannonser med pris
    """
    raw = (update.message.text or "").strip()
    parts = raw.split(" ", 2)   # ["/build", "<url>", "<task...>"]
    if len(parts) < 2:
        await update.message.reply_text(
            "Användning: /build <url> [beskrivning av vad som ska hämtas]\n"
            "Exempel: /build https://blocket.se/bilar hämta alla bilannonser med pris och länk"
        )
        return

    url = parts[1].strip()
    task = parts[2].strip() if len(parts) > 2 else "Hämta all väsentlig information från sidan"
    await _handle_scrape_build(update, url, task)


async def feat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /feat [url] <uppgiftsbeskrivning>
    Exempel: /feat https://blocket.se Bygg en scraper för bilannonser
             /feat Bygg ett verktyg som hämtar väderprognoser från SMHI
    """
    raw = (update.message.text or "").strip()
    parts = raw.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "Användning: /feat [url] <uppgiftsbeskrivning>\n\n"
            "Exempel:\n"
            "  /feat https://blocket.se/bilar Bygg scraper för bilannonser\n"
            "  /feat Bygg ett verktyg som kollar valutakurser\n\n"
            "Agent-teamet (Micke + Zack + Johan) tar hand om resten:\n"
            "  Micke → SPEC + TESTPLAN\n"
            "  Zack  → kod + tester\n"
            "  Johan → testkörning + buggar\n"
            "  Micke → slutlig review (Approve/Changes Required)"
        )
        return

    rest = parts[1].strip()
    # Kolla om första ordet är en URL
    url = _extract_url(rest)
    if url:
        task = rest[len(url):].strip() or rest
    else:
        task = rest

    await _handle_feat(update, task=task, url=url)


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
    direct_call = _extract_direct_tool_call(text, tools)
    if direct_call:
        tool_name, direct_input = direct_call
        await update.message.reply_text(f"Steg 1/{MAX_STEPS}: kör {tool_name}")
        run_result = execute_tool(tool_name, direct_input)
        obs = summarize_observation(run_result)
        session["last_tool"] = {
            "tool": tool_name,
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

    # --- Direkta fetch: kör befintliga verktyg direkt (ingen agent_team behövs) ---
    if _is_direct_kvd_fetch(text):
        await _handle_direct_fetch(update, "kvd_scraper", _parse_kvd_input(text))
        return

    if _is_direct_blocket_fetch(text):
        blocket_url = _extract_url(text) or "https://www.blocket.se/bilar?sort=price_ascending"
        await _handle_direct_fetch(update, "blocket_scraper", {"url": blocket_url})
        return

    # --- Agent Team: detektera "bygg en app/verktyg..." ---
    if _is_feat_request(text):
        detected_url = _extract_url(text)
        await _handle_feat(update, task=text, url=detected_url)
        return

    # --- Scraper Factory: detektera "bygg scraper för <url>" ---
    detected_url = _extract_url(text)
    if detected_url and _is_scraper_build_request(text):
        await _handle_scrape_build(update, detected_url, text)
        return

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
    app.add_handler(CommandHandler("build", build_cmd))
    app.add_handler(CommandHandler("feat", feat_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("vars", vars_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.run_polling()


if __name__ == "__main__":
    main()
