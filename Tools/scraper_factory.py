"""
scraper_factory.py
------------------
Multi-agent ramverk som automatiskt bygger en webbscraper.

Flöde:
  1. web_inspector analyserar URL:en och förstår sidans struktur
  2. CoderAgent (Claude) skriver Python-scraper baserat på uppgiften
  3. ReviewerAgent (Claude) kör koden och granskar output + kvalitet
  4. CoderAgent förbättrar koden baserat på feedback
  5. Loop tills ReviewerAgent godkänner (APPROVED) eller max_iterations nåtts
  6. Fullständig logg returneras med exakt vad varje agent gjort

Input:
  url            (str)  – Sidan att scrapa
  task           (str)  – Vad som ska hämtas, t.ex. "alla bilannonser med pris och länk"
  max_iterations (int)  – Max antal byggrundor (default 3)
  write_file     (bool) – Spara slutkoden som .py-fil (default True)

Output:
  {
    "url": str,
    "task": str,
    "status": "approved" | "max_iterations_reached" | "error",
    "iterations": int,
    "final_code": str | null,
    "final_score": int,
    "log": [...],
    "out_file": str | null
  }
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# Lägg till Tools-katalogen i sökvägen för import av web_inspector
sys.path.insert(0, str(Path(__file__).parent))
import web_inspector

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #

OUTPUT_DIR = Path(__file__).parent / "scraper_factory" / "output"
CODE_RUN_TIMEOUT = 40   # sekunder per testkörning

# --------------------------------------------------------------------------- #
# Systemprompts
# --------------------------------------------------------------------------- #

CODER_SYSTEM = """\
Du är en expert Python-webbutvecklare specialiserad på webbskrapning och API-integration.

Din uppgift är att skriva en komplett, fristående Python-scraper för en specifik URL och uppgift.

STRATEGI – välj i prioritetsordning:
1. **API-FIRST**: Om API-endpoints hittats (XHR/fetch-anrop eller JS-mönster), FÖREDRA att anropa dessa API:er direkt med requests.get/post(). API-anrop ger renare data, är snabbare och robustare än HTML-parsing.
2. **HYBRID**: Kombinera API-anrop och HTML-parsing om data finns på båda ställen.
3. **HTML-SCRAPING**: Använd beautifulsoup4 om inga API:er hittats eller om API kräver auth du inte har.

REGLER:
- Använd ENBART dessa paket (redan installerade): requests, beautifulsoup4, playwright
- Skriv fullständig, körbar Python-kod med en main()-funktion
- Printa ALLTID det slutliga resultatet som giltig JSON till stdout (använd print(json.dumps(...)))
- Hantera nätverksfel och saknade HTML-element med try/except
- Sätt User-Agent-header och relevanta headers (Accept: application/json etc.) för API-anrop
- Ingen interaktiv input, inga hårdkodade sökvägar
- Skriv KUN Python-kod – INGEN markdown-wrapper, INGA förklaringar utanför kommentarer i koden
- Om du kallar ett API direkt: inkludera URL, method och ett exempel på svarsdatan i en kommentar
"""

REVIEWER_SYSTEM = """\
Du är en senior granskare och QA-ingenjör för Python-webbscrapers.

Din uppgift: granska given scraper-kod och dess faktiska körningsresultat.

Bedöm på:
1. Korrekthet  – löser koden uppgiften? Hämtas rätt data?
2. Körbarhet   – kraschar koden, eller kör den utan fel?
3. Output      – är JSON-outputen komplett och användbar?
4. Felhantering – hanteras nätverksfel och saknade element?
5. Kodkvalitet – är koden läsbar och underhållbar?

Returnera ALLTID exakt detta JSON-format (och INGET annat):
{
  "verdict": "APPROVED" eller "REVISE",
  "score": <heltal 1-10>,
  "what_works": "<vad som fungerar bra>",
  "issues": ["<specifikt problem 1>", "<specifikt problem 2>"],
  "feedback": "<konkreta förbättringsinstruktioner som kodaren ska följa i nästa iteration>"
}

APPROVED om: koden kör utan krasch, producerar relevant JSON-output och löser uppgiften.
REVISE om: koden kraschar, producerar felaktig/tom output, eller missar väsentlig data.
"""


# --------------------------------------------------------------------------- #
# Hjälpfunktioner
# --------------------------------------------------------------------------- #

def _log(event: str, **kwargs) -> dict:
    entry = {"time": datetime.now().isoformat(), "event": event}
    entry.update(kwargs)
    return entry


def _call_claude(system: str, user_msg: str, max_tokens: int = 4096) -> str:
    """Anropar lokal LLM via OpenAI-kompatibelt API."""
    lm_base  = os.environ.get("OPENAI_API_BASE", "http://127.0.0.1:1234/v1")
    lm_model = os.environ.get("LM_MODEL", "meta-llama/llama-3.3-70b-instruct")
    resp = requests.post(
        f"{lm_base}/chat/completions",
        json={
            "model": lm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user_msg},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        },
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _extract_code(text: str) -> str:
    """Ta bort eventuella ```python...``` wrappers ur Claude-svar."""
    match = re.search(r"```(?:python)?\n?(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def _extract_json_obj(text: str) -> dict:
    """Extrahera första JSON-objekt ur text."""
    s = text.strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Ingen JSON hittades i svaret")
    return json.loads(s[start:end + 1])


# --------------------------------------------------------------------------- #
# Agent 1: CoderAgent
# --------------------------------------------------------------------------- #

def run_coder(
    url: str,
    task: str,
    site_info: dict,
    prev_feedback: str | None,
    iteration: int,
    api_calls: list | None = None,
    api_patterns: list | None = None,
) -> str:
    """Anropar CoderAgent → returnerar ren Python-kod."""

    # Kondensera webbsideinfo för prompten
    structure_preview = json.dumps({
        "title": site_info.get("title"),
        "description": site_info.get("meta", {}).get("description", "")[:200],
        "h1_headings": site_info.get("headings", {}).get("h1", [])[:5],
        "sample_links": [
            {"text": l.get("text", ""), "href": l.get("href", "")}
            for l in site_info.get("internal_links", [])[:8]
        ],
        "forms": site_info.get("forms", []),
        "total_links": site_info.get("total_links"),
        "main_text_sample": site_info.get("main_text_sample", [])[:2],
    }, ensure_ascii=False, indent=2)

    ai_analysis = site_info.get("ai_summary") or "Ingen AI-analys tillgänglig."

    # API-sektion: prioritera live-fångade anrop, komplettera med JS-mönster
    api_block = ""
    all_api_calls = list(api_calls or [])
    all_api_patterns = list(api_patterns or [])

    if all_api_calls:
        api_json = json.dumps(all_api_calls[:12], ensure_ascii=False, indent=2)
        api_block += (
            f"\n\n=== DETEKTERADE API-ANROP (live-fångade XHR/fetch) ===\n"
            f"{api_json}\n"
            "INSTRUKTION: Anropa dessa endpoints direkt med requests istället för att scrapa HTML!"
        )
    if all_api_patterns:
        pat_json = json.dumps(all_api_patterns[:12], ensure_ascii=False, indent=2)
        api_block += (
            f"\n\n=== API-MÖNSTER FRÅN JS-KOD ===\n"
            f"{pat_json}\n"
            "INSTRUKTION: Undersök dessa URL-mönster – de kan vara REST-endpoints du kan anropa direkt."
        )
    if not api_block:
        api_block = "\n\n(Inga API-endpoints detekterade – använd HTML-scraping.)"

    feedback_block = ""
    if prev_feedback:
        feedback_block = (
            f"\n\n=== FEEDBACK FRÅN GRANSKAREN (du måste åtgärda dessa i iteration {iteration}) ===\n"
            f"{prev_feedback}\n"
            "=== ÅTGÄRDA ALLA PUNKTER OVAN I DEN NYA VERSIONEN ==="
        )

    prompt = f"""URL: {url}
UPPGIFT: {task}
ITERATION: {iteration}

=== WEBBSIDANS STRUKTUR ===
{structure_preview}

=== AI-ANALYS AV SIDAN ===
{ai_analysis}{api_block}{feedback_block}

Skriv nu en komplett Python-scraper som löser uppgiften.
Om API-endpoints finns ovan – ANVÄND DEM DIREKT med requests.
KUN Python-kod, ingen markdown."""

    raw = _call_claude(CODER_SYSTEM, prompt, max_tokens=4096)
    return _extract_code(raw)


# --------------------------------------------------------------------------- #
# Sandlåda: kör kod i subprocess
# --------------------------------------------------------------------------- #

def run_in_sandbox(code: str) -> tuple[str, str, bool]:
    """
    Kör Python-koden i en temporär fil.
    Returnerar (stdout, stderr, success).
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(code)
        tmp = f.name

    try:
        proc = subprocess.run(
            [sys.executable, tmp],
            capture_output=True,
            text=True,
            timeout=CODE_RUN_TIMEOUT,
        )
        return proc.stdout[:6000], proc.stderr[:3000], proc.returncode == 0
    except subprocess.TimeoutExpired:
        return "", f"TIMEOUT: koden tog mer än {CODE_RUN_TIMEOUT}s", False
    except Exception as e:
        return "", str(e), False
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Agent 2: ReviewerAgent
# --------------------------------------------------------------------------- #

def run_reviewer(
    code: str,
    stdout: str,
    stderr: str,
    success: bool,
    task: str,
    iteration: int,
) -> dict:
    """Anropar ReviewerAgent → returnerar granskningsobjekt."""

    run_status = "KÖRDES OK (returncode 0)" if success else "KRASCHADE (returncode != 0)"

    prompt = f"""UPPGIFT SOM SCRAPERS SKULLE LÖSA:
{task}

ITERATION: {iteration}
KÖRNINGSSTATUS: {run_status}

=== KOD ({len(code.splitlines())} rader) ===
{code[:6000]}

=== STDOUT (faktisk output) ===
{stdout[:3000] or "(tom)"}

=== STDERR (felmeddelanden) ===
{stderr[:1500] or "(inga fel)"}

Granska koden och körningsresultatet. Returnera ENBART JSON."""

    raw = _call_claude(REVIEWER_SYSTEM, prompt, max_tokens=1024)
    try:
        return _extract_json_obj(raw)
    except Exception:
        # Fallback om ReviewerAgent returnerar ogiltig JSON
        verdict = "APPROVED" if success and stdout.strip() else "REVISE"
        return {
            "verdict": verdict,
            "score": 5 if verdict == "APPROVED" else 2,
            "what_works": "Oklar granskning (JSON-parse-fel)",
            "issues": ["Granskaren returnerade ogiltig JSON"],
            "feedback": raw[:400],
        }


# --------------------------------------------------------------------------- #
# Spara slutkod
# --------------------------------------------------------------------------- #

def _save_code(code: str, url: str) -> str:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    domain = urlparse(url).netloc.replace(".", "_").replace("-", "_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / f"{domain}_{ts}.py"
    path.write_text(code, encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- #
# Huvudfunktion
# --------------------------------------------------------------------------- #

def run(
    url: str,
    task: str,
    max_iterations: int = 3,
    write_file: bool = True,
) -> dict:
    """
    Kör hela builder-reviewer-loopen.

    Returnerar ett dict med status, slutkod, poäng och fullständig logg.
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    log: list[dict] = []
    log.append(_log("start", url=url, task=task, max_iterations=max_iterations))

    # ---------------------------------------------------------------------- #
    # Steg 1: Analysera webbsidan med web_inspector
    # ---------------------------------------------------------------------- #
    api_calls: list = []
    api_patterns: list = []
    print(f"[scraper_factory] Analyserar {url} ...", file=sys.stderr)
    log.append(_log("web_inspector_start", url=url))
    try:
        inspection = web_inspector.run(url, headless=True, use_ai=True, write_file=False)
        site_info = inspection["structure"]
        site_info["ai_summary"] = inspection.get("ai_summary")
        api_calls   = inspection.get("api_calls", [])
        api_patterns = inspection.get("api_patterns", [])
        log.append(_log(
            "web_inspector_done",
            title=site_info.get("title"),
            fetch_method=inspection["fetch_method"],
            total_links=site_info.get("total_links"),
            api_calls_found=len(api_calls),
            api_patterns_found=len(api_patterns),
        ))
        if api_calls:
            print(
                f"[scraper_factory] {len(api_calls)} live API-anrop detekterade – "
                f"CoderAgent instrueras att föredra API-scraping.",
                file=sys.stderr,
            )
        if api_patterns:
            print(
                f"[scraper_factory] {len(api_patterns)} API-URL-mönster hittade via JS-scanning.",
                file=sys.stderr,
            )
    except Exception as e:
        log.append(_log("web_inspector_failed", error=str(e)))
        return {
            "url": url, "task": task,
            "status": "error", "error": str(e),
            "iterations": 0, "final_code": None,
            "final_score": 0, "log": log, "out_file": None,
        }

    # ---------------------------------------------------------------------- #
    # Builder-Reviewer-loop
    # ---------------------------------------------------------------------- #
    final_code: str | None = None
    final_score: int = 0
    status = "max_iterations_reached"
    prev_feedback: str | None = None
    iteration = 0

    for iteration in range(1, max_iterations + 1):
        print(
            f"[scraper_factory] --- Iteration {iteration}/{max_iterations} ---",
            file=sys.stderr,
        )

        # ---- CoderAgent ---- #
        log.append(_log("coder_start", iteration=iteration))
        print(f"[scraper_factory]   CoderAgent bygger kod...", file=sys.stderr)
        try:
            code = run_coder(
                url, task, site_info, prev_feedback, iteration,
                api_calls=api_calls, api_patterns=api_patterns,
            )
            log.append(_log(
                "coder_done",
                iteration=iteration,
                code_lines=len(code.splitlines()),
                code_preview=code[:300],
            ))
        except Exception as e:
            log.append(_log("coder_error", iteration=iteration, error=str(e)))
            print(f"[scraper_factory]   CoderAgent fel: {e}", file=sys.stderr)
            break

        # ---- Testkörning ---- #
        log.append(_log("sandbox_run_start", iteration=iteration))
        print(f"[scraper_factory]   Kör koden i sandlåda...", file=sys.stderr)
        stdout, stderr, success = run_in_sandbox(code)
        log.append(_log(
            "sandbox_run_done",
            iteration=iteration,
            success=success,
            stdout_chars=len(stdout),
            stderr_preview=stderr[:400],
            stdout_preview=stdout[:400],
        ))
        print(
            f"[scraper_factory]   Körning: {'OK' if success else 'KRASCHADE'} "
            f"| stdout={len(stdout)}c stderr={len(stderr)}c",
            file=sys.stderr,
        )

        # ---- ReviewerAgent ---- #
        log.append(_log("reviewer_start", iteration=iteration))
        print(f"[scraper_factory]   ReviewerAgent granskar...", file=sys.stderr)
        try:
            review = run_reviewer(code, stdout, stderr, success, task, iteration)
            verdict = review.get("verdict", "REVISE").upper()
            score = int(review.get("score", 0))
            log.append(_log(
                "reviewer_done",
                iteration=iteration,
                verdict=verdict,
                score=score,
                what_works=review.get("what_works", ""),
                issues=review.get("issues", []),
                feedback=review.get("feedback", ""),
            ))
            print(
                f"[scraper_factory]   Granskning: {verdict} (poäng {score}/10)",
                file=sys.stderr,
            )
        except Exception as e:
            log.append(_log("reviewer_error", iteration=iteration, error=str(e)))
            verdict = "REVISE"
            score = 0
            review = {"feedback": str(e), "issues": [str(e)], "what_works": ""}
            print(f"[scraper_factory]   ReviewerAgent fel: {e}", file=sys.stderr)

        final_code = code
        final_score = score
        prev_feedback = review.get("feedback", "")

        if verdict == "APPROVED":
            status = "approved"
            log.append(_log("loop_approved", iteration=iteration, score=score))
            print(
                f"[scraper_factory] GODKÄND efter {iteration} iteration(er)!",
                file=sys.stderr,
            )
            break

        log.append(_log(
            "loop_revise",
            iteration=iteration,
            feedback_preview=prev_feedback[:300],
        ))
        print(
            f"[scraper_factory]   Begär revision. Feedback: {prev_feedback[:120]}...",
            file=sys.stderr,
        )

    # ---------------------------------------------------------------------- #
    # Spara slutkod
    # ---------------------------------------------------------------------- #
    out_file: str | None = None
    if write_file and final_code:
        try:
            out_file = _save_code(final_code, url)
            log.append(_log("code_saved", path=out_file))
            print(f"[scraper_factory] Kod sparad: {out_file}", file=sys.stderr)
        except Exception as e:
            log.append(_log("code_save_error", error=str(e)))

    log.append(_log(
        "done",
        status=status,
        total_iterations=iteration,
        final_score=final_score,
    ))

    return {
        "url": url,
        "task": task,
        "status": status,
        "iterations": iteration,
        "final_code": final_code,
        "final_score": final_score,
        "log": log,
        "out_file": out_file,
    }


# --------------------------------------------------------------------------- #
# Entrypoint: runner-kompatibelt (stdin JSON) + CLI
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    if os.environ.get("LOCAL_AGENT_TOOL_MODE") == "1":
        # Runner-läge: läs JSON från stdin, skriv JSON till stdout
        data = json.loads(sys.stdin.read())
        result = run(
            url=data["url"],
            task=data.get("task", "Hämta all väsentlig information från sidan"),
            max_iterations=int(data.get("max_iterations", 3)),
            write_file=bool(data.get("write_file", True)),
        )
        print(json.dumps(result, ensure_ascii=False))
    else:
        # CLI-läge
        import argparse
        parser = argparse.ArgumentParser(
            description="Multi-agent scraper-byggare"
        )
        parser.add_argument("url", help="URL att scrapa")
        parser.add_argument("task", nargs="?",
                            default="Hämta all väsentlig information från sidan",
                            help="Vad ska scrapers hämta?")
        parser.add_argument("--iterations", type=int, default=3,
                            help="Max antal byggrundor (default: 3)")
        parser.add_argument("--no-save", action="store_true",
                            help="Spara inte koden till fil")
        parser.add_argument("--json", action="store_true",
                            help="Skriv ut hela JSON-resultatet")
        args = parser.parse_args()

        result = run(
            url=args.url,
            task=args.task,
            max_iterations=args.iterations,
            write_file=not args.no_save,
        )

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"\n{'='*60}")
            print(f"  Status    : {result['status']}")
            print(f"  Iterationer: {result['iterations']}")
            print(f"  Poäng     : {result['final_score']}/10")
            if result["out_file"]:
                print(f"  Sparad till: {result['out_file']}")
            print(f"{'='*60}")
            if result["final_code"]:
                print("\n--- SLUTKOD ---")
                print(result["final_code"])
            print(f"\n--- LOGG ({len(result['log'])} händelser) ---")
            for entry in result["log"]:
                t = entry["time"][11:19]
                ev = entry["event"]
                extra = {k: v for k, v in entry.items() if k not in ("time", "event")}
                extra_str = ", ".join(f"{k}={v}" for k, v in list(extra.items())[:3])
                print(f"  [{t}] {ev}  {extra_str}")
