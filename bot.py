import os
import json
import subprocess
import sys
from pathlib import Path

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters


# --- LM Studio ---
LM_STUDIO_BASE = os.environ.get("OPENAI_API_BASE", "http://127.0.0.1:1234/v1")
MODEL_ID = os.environ.get("LM_MODEL", "qwen/qwen3-vl-8b")

# --- Local runner/tooling ---
RUNNER_PATH = Path(os.environ.get("RUNNER_PATH", r"C:\local-agent\Tools\runner.py"))
TOOLS_JSON_PATH = Path(os.environ.get("TOOLS_JSON_PATH", r"C:\local-agent\tools.json"))


def call_lm_studio(user_text: str) -> str:
    url = f"{LM_STUDIO_BASE}/chat/completions"
    payload = {
        "model": MODEL_ID,
        "messages": [
            {"role": "system", "content": "Du är en hjälpsam lokal assistent. Svara kort och tydligt."},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.3,
    }
    r = requests.post(url, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def call_runner(payload: dict) -> dict:
    if not RUNNER_PATH.exists():
        raise FileNotFoundError(f"runner.py hittades inte: {RUNNER_PATH}")

    proc = subprocess.run(
        [sys.executable, str(RUNNER_PATH)],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=240,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Runner failed.\nSTDERR:\n{proc.stderr}\nSTDOUT:\n{proc.stdout}")

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Runner returnerade inte JSON.\nSTDOUT:\n{proc.stdout}") from e


def split_telegram(text: str, chunk_size: int = 3500):
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


def list_tools_from_json() -> list[dict]:
    if not TOOLS_JSON_PATH.exists():
        return []
    return json.loads(TOOLS_JSON_PATH.read_text(encoding="utf-8"))


def _extract_first_json_object(text: str) -> dict:
    s = (text or "").strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    return json.loads(s[start : end + 1])


def plan_tool_call(user_text: str, tools: list[dict]) -> dict:
    """
    Returnerar antingen:
      {"action":"run","tool":"<name>","input":{...}}
    eller
      {"action":"chat"}
    """
    url = f"{LM_STUDIO_BASE}/chat/completions"
    tool_list = "\n".join([f"- {t.get('name','?')}: {t.get('description','')}" for t in tools])

    system = (
        "Du är en router. Välj om användaren vill köra ett tool eller bara chatta.\n"
        "Svara ENDAST med giltig JSON (ingen annan text).\n"
        'Schema:\n'
        '1) Tool: {"action":"run","tool":"<name>","input":{...}}\n'
        '2) Chat: {"action":"chat"}\n'
        "Tillgängliga tools:\n"
        f"{tool_list}\n"
        "Regler:\n"
        "- Om användaren nämner 'kvd' eller vill hämta listningar/annonser -> välj kvd_scraper.\n"
        "- Sätt input.write_file=true om användaren säger spara/fil.\n"
        "- headless ska vara true om inte användaren uttryckligen vill se webbläsaren.\n"
        "- Om du är osäker: välj chat.\n"
    )

    payload = {
        "model": MODEL_ID,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.0,
    }
    r = requests.post(url, json=payload, timeout=120)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]

    try:
        plan = _extract_first_json_object(content)
    except Exception:
        return {"action": "chat"}

    if not isinstance(plan, dict):
        return {"action": "chat"}

    if plan.get("action") == "run" and isinstance(plan.get("tool"), str):
        if not isinstance(plan.get("input", {}), dict):
            plan["input"] = {}
        return plan

    return {"action": "chat"}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "OK.\n"
        "• Skriv vanligt meddelande (auto: boten kan välja tool).\n"
        "• /kvd kör KVD-scraper (kvd_scraper).\n"
        "• /run {JSON} kör valfritt tool via runner.\n"
        "• /tools listar tools från tools.json\n"
    )


async def tools_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tools = list_tools_from_json()
        if not tools:
            await update.message.reply_text(f"Inga tools hittades i {TOOLS_JSON_PATH}")
            return

        lines = ["Tools:"]
        for t in tools:
            name = t.get("name", "?")
            desc = t.get("description", "")
            lines.append(f"- {name}: {desc}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Fel: {e}")


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /run {"tool":"kvd_scraper","input":{"headless":true,"write_file":false}}
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
    try:
        result = call_runner(payload)
        msg = "OK."
        if isinstance(result, dict) and "items" in result and isinstance(result["items"], list):
            msg = f"OK. items={len(result['items'])}"
        for part in split_telegram(msg):
            await update.message.reply_text(part)
    except Exception as e:
        await update.message.reply_text(f"Fel: {e}")


async def kvd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    headless = True
    write_file = False

    for arg in context.args:
        if "=" in arg:
            k, v = arg.split("=", 1)
            k = k.strip().lower()
            v = v.strip().lower()
            if k == "headless":
                headless = v in ("1", "true", "yes", "y", "on")
            elif k == "write_file":
                write_file = v in ("1", "true", "yes", "y", "on")

    await update.message.reply_text("Kör KVD-scraper...")

    try:
        result = call_runner({"tool": "kvd_scraper", "input": {"headless": headless, "write_file": write_file}})
        items = result.get("items", [])
        out_file = result.get("out_file")

        msg = f"Hittade {len(items)} annonser."
        if out_file:
            msg += f"\nSparade fil: {out_file}"

        if items:
            preview = []
            for it in items[:5]:
                title = (it.get("title") or "").strip()
                deadline = (it.get("deadline_text") or "").strip()
                url = (it.get("url") or "").strip()
                preview.append(f"- {title} ({deadline})\n  {url}")
            msg += "\n\n" + "\n".join(preview)

    except Exception as e:
        msg = f"Fel när jag körde KVD-scraper: {e}"

    for part in split_telegram(msg):
        await update.message.reply_text(part)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # Auto-tool routing via LM Studio (före triggers)
    tools = list_tools_from_json()
    try:
        plan = plan_tool_call(text, tools)
    except Exception:
        plan = {"action": "chat"}

    if plan.get("action") == "run":
        tool = plan["tool"]
        tool_input = plan.get("input", {})

        await update.message.reply_text("Kör tool...")
        try:
            result = call_runner({"tool": tool, "input": tool_input})
            msg = "OK."
            if isinstance(result, dict) and "items" in result and isinstance(result["items"], list):
                msg = f"OK. items={len(result['items'])}"
            for part in split_telegram(msg):
                await update.message.reply_text(part)
        except Exception as e:
            await update.message.reply_text(f"Fel: {e}")
        return

    # Snabbtrigger utan kommando (fallback)
    if text.lower().startswith("kvd"):
        parts = text.split()
        headless = True
        write_file = False
        for arg in parts[1:]:
            if "=" in arg:
                k, v = arg.split("=", 1)
                k = k.strip().lower()
                v = v.strip().lower()
                if k == "headless":
                    headless = v in ("1", "true", "yes", "y", "on")
                elif k == "write_file":
                    write_file = v in ("1", "true", "yes", "y", "on")

        await update.message.reply_text("Kör KVD-scraper...")
        try:
            result = call_runner({"tool": "kvd_scraper", "input": {"headless": headless, "write_file": write_file}})
            items = result.get("items", [])
            out_file = result.get("out_file")

            msg = f"Hittade {len(items)} annonser."
            if out_file:
                msg += f"\nSparade fil: {out_file}"

            for part in split_telegram(msg):
                await update.message.reply_text(part)
        except Exception as e:
            await update.message.reply_text(f"Fel när jag körde KVD-scraper: {e}")
        return

    # Default: LM Studio chat
    try:
        reply = call_lm_studio(text)
    except Exception as e:
        reply = f"Fel mot LM Studio: {e}"

    for part in split_telegram(reply):
        await update.message.reply_text(part)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Sätt TELEGRAM_BOT_TOKEN som miljövariabel.")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tools", tools_cmd))
    app.add_handler(CommandHandler("run", run_cmd))
    app.add_handler(CommandHandler("kvd", kvd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.run_polling()


if __name__ == "__main__":
    main()