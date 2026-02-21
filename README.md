# local-agent

En Telegram-bot som kan växla mellan:

- **LLM-läge** för vanliga frågor/svar.
- **Agent-läge** för att planera och köra lokala verktyg via en tool-runner.

Projektet är byggt för lokal körning med LM Studio-kompatibel OpenAI-endpoint.

## Funktioner

- Router mellan LLM-läge och agent-läge baserat på användarens text.
- Sessioner per chat-id (`sessions/*.json`).
- Verktygsregistry via `tools.json`.
- Tool-runner som validerar input mot JSON-schema och kör Python-verktyg.
- Stöd för flera agentmotorer (`local` och `autogen`).

## Krav

- Python 3.10+
- Telegram bot-token
- En körande OpenAI-kompatibel endpoint (t.ex. LM Studio)

Installera beroenden:

```bash
pip install -r requirements.txt
```

## Konfiguration (miljövariabler)

Vanliga variabler:

- `TELEGRAM_BOT_TOKEN` – token för din Telegram-bot.
- `OPENAI_API_BASE` – bas-URL till chat API (default: `http://127.0.0.1:1234/v1`).
- `LM_MODEL` – modell-ID (default: `qwen/qwen3-vl-8b`).
- `DEFAULT_AGENT_ENGINE` – `local` eller `autogen` (default: `local`).
- `RUNNER_PATH` – sökväg till runner-script (default pekar på `Tools/runner.py` i Windows-miljö).
- `TOOLS_JSON_PATH` – sökväg till tool-registry (default pekar på `tools.json` i Windows-miljö).
- `MAX_AGENT_STEPS` – max steg i agent-loop (default: `8`).

Exempel (Linux/macOS):

```bash
export TELEGRAM_BOT_TOKEN="<din_token>"
export OPENAI_API_BASE="http://127.0.0.1:1234/v1"
export LM_MODEL="qwen/qwen3-vl-8b"
export RUNNER_PATH="$(pwd)/Tools/runner.py"
export TOOLS_JSON_PATH="$(pwd)/tools.json"
```

## Verktyg (tools) i registry

Boten läser tillgängliga verktyg dynamiskt från `tools.json`.
Det betyder att när du lägger till, tar bort eller byter namn på tools i registryt, behöver du normalt inte ändra botlogiken.

Snabbflöde:

1. Lägg till/uppdatera tool i `tools.json` (`name`, `entrypoint`, `input_schema`).
2. Säkerställ att entrypoint-scriptet läser JSON från stdin och skriver JSON till stdout.
3. Kör `/tools` i Telegram för att verifiera att boten ser verktyget.
4. Kör verktyget med `/run {"tool":"<tool_name>","input":{...}}` eller via agentläget.

## Starta boten

```bash
python bot.py
```

## Tester

Kör enhetstester:

```bash
python -m unittest -v
```

## Projektstruktur

```text
.
├── bot.py                  # Telegram-bot + router + agentloop
├── session_store.py        # Persistens av chat-sessioner
├── tools.json              # Registry för verktyg
├── Tools/
│   ├── runner.py           # Tool-runner med validering och exekvering
│   └── ...                 # Verktygsskript
├── tests/
│   └── test_router_mode.py # Tester för router/agentbeteende
└── sessions/               # Session-filer (skapas vid körning)
```

## Notering

`Tools/runner.py` ansvarar för att exekvera verktyg från registryt och förväntar sig JSON-in/JSON-ut. Säkerställ att varje verktyg följer samma kontrakt för stabil agentkörning.
