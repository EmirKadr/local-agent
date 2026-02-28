"""
agent_team.py
-------------
Tre permanenta sub-agenter för all app-byggande.

  Micke – Spec & QA Lead      Skriver SPEC, TESTPLAN, DoD. Slutgiltig review.
  Zack  – Dev Lead            Implementerar kod. Kan ställa Q-### till Micke.
  Johan – QA/Test Operator    Kör testplan som svart låda, rapporterar buggar.

Flöde per FEAT-###:
  Steg 1  Micke  → SPEC.md + TESTPLAN.md + testdata/ + expected/   [Gate 1]
  Steg 2  Zack   → src/ + tests/ + RUNBOOK.md + CHANGELOG.md        [Gate 2]
           └─ Zack öppnar Q-### vid oklarhet → Micke svarar direkt
  Steg 3  Johan  → TESTREPORT.md + bugs/BUG-###.md                  [Gate 3]
  Steg 4  Micke  → Approve eller Changes Required
           └─ Changes Required → Zack fixar → Johan re-testar (loop)

Tillgängliga tools (skickas som info till varje agent):
  web_inspector    – analyserar webbsidors HTML-struktur
  scraper_factory  – multi-agent scraper-bygge (CoderAgent + ReviewerAgent)
  kvd_scraper      – hämtar KVD-auktionslistningar
  runner           – kör vilken registrerad tool som helst via JSON

Indata: url (optional), task (required), feat_id (optional), max_cycles (default 2)
Utdata: status, artefakter, fullständig logg, projektmapp
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

import requests

PROJECTS_DIR = Path(__file__).parent / "agent_team" / "projects"
_TOOLS_DIR   = Path(__file__).resolve().parent
MAX_ZACK_QUESTIONS = 3       # Max Q-### per steg 2-iteration
CODE_RUN_TIMEOUT  = 30       # sekunder per testkörning


# --------------------------------------------------------------------------- #
# Tillgängliga tools (info till agenterna)
# --------------------------------------------------------------------------- #

TOOLS_INFO = f"""
Tillgängliga tools – ANVÄND dessa istället för att bygga från scratch:

━━━ web_inspector ━━━
Hämtar HTML från en URL och returnerar AI-analys + sidstruktur (titlar, länkar, formulär).

  # Direkt import (rekommenderat):
  import sys; sys.path.insert(0, r"{_TOOLS_DIR}")
  import web_inspector
  result = web_inspector.run(url="https://example.com")
  # result["ai_summary"]  – AI-beskrivning av sidan
  # result["structure"]   – dict med headings, links, forms, scripts m.m.

  # Via runner.py (subprocess):
  import json, subprocess, sys
  req = {{"tool": "web_inspector", "input": {{"url": "https://example.com"}}}}
  proc = subprocess.run([sys.executable, r"{_TOOLS_DIR}/runner.py"],
                        input=json.dumps(req), capture_output=True, text=True, timeout=60)
  result = json.loads(proc.stdout)["result"]

━━━ scraper_factory ━━━
Bygger automatiskt en komplett Python-scraper (CoderAgent + ReviewerAgent-loop).
Returnerar färdig, granskad scraper-kod + resultatet från testkörning.

  # Direkt import (rekommenderat):
  import sys; sys.path.insert(0, r"{_TOOLS_DIR}")
  import scraper_factory
  result = scraper_factory.run(
      url="https://www.blocket.se/bilar",
      task="Hämta alla bilannonser med titel, pris och länk",
  )
  # result["status"]      – "approved" | "max_iterations_reached" | "error"
  # result["final_code"]  – färdig Python-scraper-kod som sträng
  # result["out_file"]    – sökväg till sparad .py-fil (om write_file=True)

  # Via runner.py (subprocess):
  req = {{"tool": "scraper_factory", "input": {{"url": "https://...", "task": "Hämta..."}}}}
  proc = subprocess.run([sys.executable, r"{_TOOLS_DIR}/runner.py"],
                        input=json.dumps(req), capture_output=True, text=True, timeout=120)
  result = json.loads(proc.stdout)["result"]

━━━ kvd_scraper ━━━
Hämtar KVD-auktionslistningar (idag/ikväll/imorgon).

  import sys; sys.path.insert(0, r"{_TOOLS_DIR}")
  import kvd_scraper14
  result = kvd_scraper14.run()

━━━ runner.py ━━━
Generell dispatcher – kör vilken registrerad tool som helst via JSON på stdin.

  req = {{"tool": "<tool_name>", "input": {{...}}}}
  proc = subprocess.run([sys.executable, r"{_TOOLS_DIR}/runner.py"],
                        input=json.dumps(req), capture_output=True, text=True)
  out = json.loads(proc.stdout)  # {{"ok": true, "result": {{...}}}} eller {{"ok": false, "error": {{...}}}}
"""


# --------------------------------------------------------------------------- #
# System-prompts
# --------------------------------------------------------------------------- #

MICKE_SPEC_SYSTEM = f"""\
Du är Micke, Spec & QA Lead. Du producerar SPEC och TESTPLAN INNAN någon kod skrivs.

{TOOLS_INFO}

Du skriver INTE kod. Du kör INTE tester.
Du är noggrann, strukturerad och kravdriven.

VIKTIGT – om uppgiften involverar att hämta data från webben:
- Specificera i SPEC att Zack SKA använda web_inspector och/eller scraper_factory
- Zack ska INTE bygga en ny scraper från scratch när dessa verktyg redan finns
- Ange i SPEC: vilken URL som ska användas och vad som ska hämtas – verktygen sköter resten

Returnera ALLTID ett giltigt JSON-objekt (inget annat) med exakt dessa nycklar:
{{
  "spec_md": "# SPEC\\n...",
  "testplan_md": "# TESTPLAN\\n...",
  "dod_md": "# Definition of Done\\n...",
  "testcases": [
    {{
      "id": "TC-001",
      "description": "...",
      "category": "happy_path | edge_case | error_case | regression",
      "input": {{}},
      "expected_output": {{}},
      "pass_criterion": "Exakt vad som krävs för PASS"
    }}
  ]
}}

Krav på testfall: minst 5 st (1 happy_path, 2 edge_case, 1 error_case, 1 regression).
SPEC.md måste innehålla: Syfte, Scope (In/Out), Inputs/Outputs, Regler (numrerade),
Felhantering, Observability, Acceptance Criteria (AC), Definition of Done.
"""

MICKE_REVIEW_SYSTEM = """\
Du är Micke, Spec & QA Lead. Du gör den slutliga kodreviewen.

Du läser TESTREPORT.md, buggar och koden.
Du fattar beslut: Approve eller Changes Required.

Returnera ALLTID ett giltigt JSON-objekt:
{
  "verdict": "approve" | "changes_required",
  "summary": "Kort sammanfattning av reviewen",
  "required_changes": [
    "Specifik förändring 1 som Zack MÅSTE göra",
    "Specifik förändring 2"
  ],
  "approved_ac": ["AC-1", "AC-3"],
  "failed_ac": ["AC-2"]
}

Approve om: alla Acceptance Criteria uppfyllda, inga öppna Blockers/Majors.
Changes Required om: någon AC inte uppfylld, eller Blocker/Major-bug finns.
"""

MICKE_ANSWER_SYSTEM = """\
Du är Micke, Spec & QA Lead. Zack har en fråga om specen.

Svara tydligt och uppdatera spec-texten om det behövs.

Returnera JSON:
{
  "answer": "Tydligt svar på Zacks fråga",
  "spec_update": "Uppdaterad eller tillagd text i SPEC.md (tom sträng om ingen ändring behövs)",
  "chosen_interpretation": "A" | "B" | "custom"
}
"""

ZACK_IMPL_SYSTEM = f"""\
Du är Zack, Dev Lead. Du implementerar exakt enligt SPEC.md.

{TOOLS_INFO}

Du skriver Python-kod. Koden ska:
- Vara fristående och körbar med: python src/main.py < testdata/TC-001.json
- Läsa input från stdin som JSON (om input finns)
- Skriva output till stdout som JSON
- Importera och använda befintliga tools via sys.path när det behövs
- Ha fullständig felhantering

Om något är oklart: returnera questions-listan ISTÄLLET för filer.

Returnera JSON med ANTINGEN "files" ELLER "questions":
{{
  "files": {{
    "src/main.py": "...fullständig Python-kod...",
    "tests/test_main.py": "...pytest-tester...",
    "RUNBOOK.md": "...körningsinstruktioner...",
    "CHANGELOG.md": "...vad som ändrats..."
  }},
  "questions": [
    {{
      "id": "Q-001",
      "unclear": "Vad är oklart",
      "option_a": "Tolkning A",
      "option_b": "Tolkning B",
      "recommended": "a"
    }}
  ]
}}
"""

ZACK_FIX_SYSTEM = f"""\
Du är Zack, Dev Lead. Micke har begärt Changes Required.

{TOOLS_INFO}

Dina uppgifter:
- Läs listan "required_changes" noga
- Fixa EXAKT de problem som Micke angett
- Förändra INTE krav eller testförväntningar

Returnera JSON med uppdaterade filer:
{{
  "files": {{
    "src/main.py": "...uppdaterad kod...",
    "CHANGELOG.md": "...vad som ändrats i denna fix..."
  }}
}}
"""

JOHAN_TEST_SYSTEM = """\
Du är Johan, QA/Test Operator. Du kör testplanen som svart låda.

Du kör koden mot testdata och jämför med expected output.
Du ändrar INTE produktionskod.

Returnera JSON:
{
  "test_results": [
    {
      "tc_id": "TC-001",
      "description": "...",
      "actual_output": "...faktisk output eller felmeddelande...",
      "expected_output": "...",
      "pass": true | false,
      "notes": "Ev. kommentar"
    }
  ],
  "bugs": [
    {
      "id": "BUG-001",
      "title": "Kort titel",
      "severity": "blocker | major | minor",
      "tc_id": "TC-002",
      "reproduce_steps": ["Steg 1", "Steg 2"],
      "expected": "...",
      "actual": "...",
      "env": "Python 3.x, commit: ..."
    }
  ],
  "summary": "Övergripande sammanfattning"
}
"""


# --------------------------------------------------------------------------- #
# Hjälpfunktioner
# --------------------------------------------------------------------------- #

def _ts() -> str:
    return datetime.now().isoformat()


def _log(log: list, event: str, agent: str = "", **kw) -> None:
    entry = {"time": _ts(), "event": event, "agent": agent}
    entry.update(kw)
    log.append(entry)


def _call_claude(system: str, user_msg: str, max_tokens: int = 6144) -> str:
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
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _parse_json(text: str) -> dict:
    """Extrahera JSON ur text (hanterar markdown-wrappers)."""
    s = text.strip()
    # Ta bort ```json ... ``` wrappers
    m = re.search(r"```(?:json)?\n?(.*?)```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Ingen JSON hittades. Svar: {text[:300]}")
    return json.loads(s[start:end + 1])


# --------------------------------------------------------------------------- #
# Projektmapp
# --------------------------------------------------------------------------- #

def _setup_project(feat_id: str) -> Path:
    proj = PROJECTS_DIR / feat_id
    for d in ["testdata", "expected", "src", "tests", "bugs"]:
        (proj / d).mkdir(parents=True, exist_ok=True)
    return proj


def _write_files(proj: Path, files: dict) -> None:
    """Skriv agenters fil-output till projektmappen."""
    for rel_path, content in files.items():
        target = proj / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False, indent=2)
        target.write_text(str(content), encoding="utf-8")


def _read_file(proj: Path, name: str) -> str:
    p = proj / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _list_files(proj: Path, subdir: str) -> dict[str, str]:
    d = proj / subdir
    if not d.exists():
        return {}
    return {
        f.name: f.read_text(encoding="utf-8")
        for f in sorted(d.iterdir())
        if f.is_file()
    }


# --------------------------------------------------------------------------- #
# Sandlåda – kör testkod
# --------------------------------------------------------------------------- #

def _run_code(code_path: Path, stdin_data: str = "", timeout: int = CODE_RUN_TIMEOUT) -> tuple[str, str, bool]:
    """Kör en Python-fil, returnerar (stdout, stderr, success)."""
    if not code_path.exists():
        return "", f"Filen {code_path} hittades inte", False
    try:
        proc = subprocess.run(
            [sys.executable, str(code_path)],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.stdout[:4000], proc.stderr[:2000], proc.returncode == 0
    except subprocess.TimeoutExpired:
        return "", f"TIMEOUT > {timeout}s", False
    except Exception as e:
        return "", str(e), False


# --------------------------------------------------------------------------- #
# Steg 1 – Micke: Spec & testplan
# --------------------------------------------------------------------------- #

def step1_micke_spec(task: str, url: str | None, log: list, proj: Path) -> dict:
    _log(log, "micke_spec_start", "Micke")
    print("[Micke] Skriver SPEC + TESTPLAN...", file=sys.stderr)

    url_line = f"URL (om relevant): {url}" if url else ""
    prompt = f"""Uppgift: {task}
{url_line}

Skriv SPEC.md, TESTPLAN.md, DoD och minst 5 testfall.
Returnera exakt JSON enligt instruktionerna."""

    raw = _call_claude(MICKE_SPEC_SYSTEM, prompt, max_tokens=6144)
    data = _parse_json(raw)

    # Skriv artefakter till projektmappen
    files_to_write = {
        "SPEC.md": data.get("spec_md", ""),
        "TESTPLAN.md": data.get("testplan_md", ""),
        "DOD.md": data.get("dod_md", ""),
    }
    for tc in data.get("testcases", []):
        tc_id = tc.get("id", "TC-XXX")
        files_to_write[f"testdata/{tc_id}.json"] = tc.get("input", {})
        files_to_write[f"expected/{tc_id}.json"] = tc.get("expected_output", {})

    _write_files(proj, files_to_write)

    testcases = data.get("testcases", [])
    _log(log, "micke_spec_done", "Micke",
         testcases=len(testcases),
         categories=[tc.get("category") for tc in testcases])

    print(f"[Micke] SPEC klar. {len(testcases)} testfall.", file=sys.stderr)
    return data


# --------------------------------------------------------------------------- #
# Steg 2 – Zack: Implementation (med Q-### loop mot Micke)
# --------------------------------------------------------------------------- #

def step2_zack_impl(task: str, proj: Path, log: list, is_fix: bool = False, required_changes: list = None) -> None:
    spec_md   = _read_file(proj, "SPEC.md")
    testplan  = _read_file(proj, "TESTPLAN.md")
    changelog = _read_file(proj, "CHANGELOG.md")

    if is_fix:
        system = ZACK_FIX_SYSTEM
        changes_str = "\n".join(f"- {c}" for c in (required_changes or []))
        prompt = f"""SPEC.md:
{spec_md}

CHANGELOG (tidigare versioner):
{changelog}

MICKES REQUIRED CHANGES:
{changes_str}

Implementera fixarna exakt. Returnera JSON med uppdaterade filer."""
        agent_name = "Zack(fix)"
    else:
        system = ZACK_IMPL_SYSTEM
        prompt = f"""Uppgift: {task}

SPEC.md:
{spec_md}

TESTPLAN.md:
{testplan}

Implementera exakt enligt spec. Returnera JSON."""
        agent_name = "Zack"

    for attempt in range(MAX_ZACK_QUESTIONS + 1):
        _log(log, "zack_impl_attempt", agent_name, attempt=attempt + 1)
        print(f"[{agent_name}] Implementerar (försök {attempt + 1})...", file=sys.stderr)

        raw = _call_claude(system, prompt, max_tokens=8192)
        try:
            data = _parse_json(raw)
        except Exception as e:
            _log(log, "zack_parse_error", agent_name, error=str(e))
            break

        # Zack har frågor → Micke svarar
        questions = data.get("questions") or []
        if questions and not is_fix:
            answers = []
            for q in questions[:MAX_ZACK_QUESTIONS]:
                _log(log, "zack_question", agent_name, question_id=q.get("id"), unclear=q.get("unclear", "")[:150])
                print(f"[Zack→Micke] Fråga: {q.get('unclear', '')[:100]}", file=sys.stderr)

                answer_raw = _call_claude(
                    MICKE_ANSWER_SYSTEM,
                    f"SPEC.md:\n{spec_md}\n\nZacks fråga:\n{json.dumps(q, ensure_ascii=False)}",
                    max_tokens=1024,
                )
                try:
                    ans = _parse_json(answer_raw)
                except Exception:
                    ans = {"answer": answer_raw[:500], "spec_update": "", "chosen_interpretation": "A"}

                _log(log, "micke_answer", "Micke",
                     question_id=q.get("id"),
                     answer=ans.get("answer", "")[:200])
                print(f"[Micke→Zack] Svar: {ans.get('answer', '')[:100]}", file=sys.stderr)

                # Uppdatera spec om Micke ändrat något
                if ans.get("spec_update"):
                    spec_md += f"\n\n### Uppdatering (svar på {q.get('id')}):\n{ans['spec_update']}"
                    (proj / "SPEC.md").write_text(spec_md, encoding="utf-8")
                    _log(log, "spec_updated", "Micke", question_id=q.get("id"))

                answers.append(f"{q.get('id')}: {ans.get('answer', '')}")

            # Ge Zack svaren i nästa iteration
            prompt += f"\n\nMICKES SVAR PÅ DINA FRÅGOR:\n" + "\n".join(answers) + "\n\nImplementera nu."
            continue

        # Zack har filer → skriv dem
        files = data.get("files") or {}
        if files:
            _write_files(proj, files)
            _log(log, "zack_impl_done", agent_name,
                 files_written=list(files.keys()),
                 code_lines=sum(len(str(c).splitlines()) for c in files.values()))
            print(f"[{agent_name}] Kod skriven: {list(files.keys())}", file=sys.stderr)
            return

        _log(log, "zack_no_output", agent_name)
        break

    _log(log, "zack_impl_warning", agent_name, note="Inga filer skrevs")


# --------------------------------------------------------------------------- #
# Steg 3 – Johan: Testkörning
# --------------------------------------------------------------------------- #

def step3_johan_test(proj: Path, log: list, tc_filter: list[str] | None = None) -> dict:
    _log(log, "johan_test_start", "Johan")
    print("[Johan] Kör testplan...", file=sys.stderr)

    testplan = _read_file(proj, "TESTPLAN.md")
    testdata  = _list_files(proj, "testdata")
    expected  = _list_files(proj, "expected")
    src_files = _list_files(proj, "src")
    spec_md   = _read_file(proj, "SPEC.md")

    # Kör faktisk kod per testfall
    main_py = proj / "src" / "main.py"
    run_results: dict[str, dict] = {}
    for tc_id, td_content in testdata.items():
        tc_bare = tc_id.replace(".json", "")
        if tc_filter and tc_bare not in tc_filter:
            continue
        stdout, stderr, success = _run_code(main_py, stdin_data=td_content, timeout=CODE_RUN_TIMEOUT)
        run_results[tc_bare] = {
            "stdout": stdout,
            "stderr": stderr,
            "success": success,
            "expected": expected.get(tc_id, expected.get(tc_bare + ".json", "")),
        }

    # Johan analyserar resultaten med Claude
    run_summary = json.dumps(run_results, ensure_ascii=False, indent=2)[:8000]
    prompt = f"""TESTPLAN.md:
{testplan}

SPEC (Acceptance Criteria):
{spec_md[:2000]}

KÖD (src/main.py):
{src_files.get('main.py', '(saknas)')[:3000]}

KÖRNINGSRESULTAT (testfall → stdout/stderr/success/expected):
{run_summary}

Utvärdera varje testfall. Returnera JSON med test_results och bugs."""

    raw = _call_claude(JOHAN_TEST_SYSTEM, prompt, max_tokens=4096)
    try:
        data = _parse_json(raw)
    except Exception as e:
        _log(log, "johan_parse_error", "Johan", error=str(e))
        data = {"test_results": [], "bugs": [], "summary": f"Parse-fel: {e}"}

    # Skriv rapport + buggar
    test_results = data.get("test_results", [])
    bugs = data.get("bugs", [])

    passed = sum(1 for t in test_results if t.get("pass"))
    failed = sum(1 for t in test_results if not t.get("pass"))

    report_lines = [
        "# TESTREPORT",
        f"Datum: {_ts()}",
        f"Testfall körda: {len(test_results)}  |  Godkända: {passed}  |  Underkända: {failed}",
        "",
        "## Sammanfattning",
        data.get("summary", ""),
        "",
        "## Testfall",
    ]
    for tr in test_results:
        status = "PASS" if tr.get("pass") else "FAIL"
        report_lines.append(f"- [{status}] {tr.get('tc_id')}: {tr.get('description', '')}")
        if not tr.get("pass"):
            report_lines.append(f"  Faktiskt: {str(tr.get('actual_output', ''))[:200]}")
            report_lines.append(f"  Förväntat: {str(tr.get('expected_output', ''))[:200]}")

    if bugs:
        report_lines += ["", "## Öppna buggar"]
        for b in bugs:
            report_lines.append(f"- [{b.get('severity', '?').upper()}] {b.get('id')}: {b.get('title')}")

    _write_files(proj, {"TESTREPORT.md": "\n".join(report_lines)})

    for bug in bugs:
        bug_id = bug.get("id", "BUG-XXX")
        bug_md = f"""# {bug_id}: {bug.get('title', '')}

**Allvarlighetsgrad:** {bug.get('severity', '?').upper()}
**Testfall:** {bug.get('tc_id', '')}
**Miljö:** {bug.get('env', 'Python 3')}

## Steg att reproducera
{chr(10).join(f'{i+1}. {s}' for i, s in enumerate(bug.get('reproduce_steps', [])))}

## Förväntat resultat
{bug.get('expected', '')}

## Faktiskt resultat
{bug.get('actual', '')}
"""
        _write_files(proj, {f"bugs/{bug_id}.md": bug_md})

    _log(log, "johan_test_done", "Johan",
         passed=passed, failed=failed,
         bugs=[b.get("id") for b in bugs],
         blockers=[b.get("id") for b in bugs if b.get("severity") == "blocker"])
    print(f"[Johan] Test klart: {passed} pass, {failed} fail, {len(bugs)} buggar.", file=sys.stderr)

    return data


# --------------------------------------------------------------------------- #
# Steg 4 – Micke: Review
# --------------------------------------------------------------------------- #

def step4_micke_review(proj: Path, log: list) -> dict:
    _log(log, "micke_review_start", "Micke")
    print("[Micke] Gör slutlig review...", file=sys.stderr)

    testreport = _read_file(proj, "TESTREPORT.md")
    spec_md    = _read_file(proj, "SPEC.md")
    dod_md     = _read_file(proj, "DOD.md")
    src_files  = _list_files(proj, "src")
    bug_files  = _list_files(proj, "bugs")

    code_preview = "\n\n".join(
        f"=== {name} ===\n{content[:2000]}"
        for name, content in list(src_files.items())[:3]
    )
    bug_content = "\n\n".join(
        f"{name}:\n{c[:600]}" for name, c in list(bug_files.items())[:5]
    )

    prompt = f"""SPEC.md:
{spec_md[:3000]}

DOD.md:
{dod_md[:1000]}

TESTREPORT.md:
{testreport[:2000]}

BUGGAR:
{bug_content or "(inga)"}

KOD (preview):
{code_preview[:3000]}

Fatta beslut: Approve eller Changes Required. Returnera JSON."""

    raw = _call_claude(MICKE_REVIEW_SYSTEM, prompt, max_tokens=2048)
    try:
        data = _parse_json(raw)
    except Exception as e:
        _log(log, "micke_review_parse_error", "Micke", error=str(e))
        data = {
            "verdict": "changes_required",
            "summary": f"Review-parse-fel: {e}",
            "required_changes": ["Kontrollera output-format"],
            "approved_ac": [], "failed_ac": [],
        }

    _log(log, "micke_review_done", "Micke",
         verdict=data.get("verdict"),
         required_changes=data.get("required_changes", []))
    print(f"[Micke] Review: {data.get('verdict')}", file=sys.stderr)
    return data


# --------------------------------------------------------------------------- #
# Resultatjämförelse
# --------------------------------------------------------------------------- #

def _write_result_summary(
    proj: Path,
    feat_id: str,
    task: str,
    spec_data: dict,
    cycle_summaries: list[dict],
) -> None:
    final = cycle_summaries[-1] if cycle_summaries else {}
    verdict = final.get("verdict", "pending")
    total_cycles = len(cycle_summaries)
    verdict_label = "GODKÄND" if verdict == "approve" else "EJ GODKÄND"

    lines = [
        f"# Resultatjämförelse — {feat_id}",
        "",
        "## Uppgift",
        task,
        "",
        f"## Slutresultat: {verdict_label} ({total_cycles} cykel{'er' if total_cycles != 1 else ''})",
    ]

    # Testfall: vad var målet vs utfall
    testcases = spec_data.get("testcases", [])
    if testcases:
        final_results = {t.get("tc_id"): t for t in final.get("test_results", [])}
        lines += ["", "## Testfall (mål vs utfall)"]
        for tc in testcases:
            tc_id = tc.get("id", "?")
            cat = tc.get("category", "")
            desc = tc.get("description", "")
            result = final_results.get(tc_id)
            if result:
                sym = "✓" if result.get("pass") else "✗"
            else:
                sym = "?"
            lines.append(f"- [{sym}] {tc_id} ({cat}): {desc}")

    # Acceptance Criteria
    approved_ac = final.get("approved_ac", [])
    failed_ac = final.get("failed_ac", [])
    if approved_ac or failed_ac:
        lines += ["", "## Acceptance Criteria"]
        for ac in approved_ac:
            lines.append(f"- [✓] {ac}")
        for ac in failed_ac:
            lines.append(f"- [✗] {ac}")

    # Per cykel
    lines += ["", "## Per cykel"]
    for cs in cycle_summaries:
        passed = cs.get("passed", 0)
        total = cs.get("total", 0)
        bugs = cs.get("bugs", [])
        blockers = [b for b in bugs if b.get("severity") == "blocker"]
        v = cs.get("verdict", "?")
        v_label = "Godkänd" if v == "approve" else "Changes Required"

        lines += ["", f"### Cykel {cs['cycle']}"]
        lines.append(f"- Tester: {passed}/{total} PASS")
        if bugs:
            bug_ids = ", ".join(
                f"{b.get('id', '?')}({b.get('severity', '?')})" for b in bugs[:5]
            )
            lines.append(f"- Buggar: {bug_ids}" + (f"  [{len(blockers)} blocker]" if blockers else ""))
        else:
            lines.append("- Buggar: inga")
        lines.append(f"- Micke: {v_label}")
        for rc in cs.get("required_changes", [])[:5]:
            lines.append(f"  → {rc}")

    _write_files(proj, {"RESULT_SUMMARY.md": "\n".join(lines)})


# --------------------------------------------------------------------------- #
# Orkestrator – huvudloop
# --------------------------------------------------------------------------- #

def run(
    task: str,
    url: str | None = None,
    feat_id: str | None = None,
    max_cycles: int = 4,
    progress_cb=None,   # callable(str) för progress-meddelanden
) -> dict:
    """
    Kör hela Micke→Zack→Johan→Micke-loopen.
    Returnerar ett dict med status, artefakter och logg.
    """
    if not feat_id:
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        feat_id = f"FEAT-{ts}"

    def notify(msg: str):
        print(f"[agent_team] {msg}", file=sys.stderr)
        if progress_cb:
            progress_cb(msg)

    log: list[dict] = []
    _log(log, "start", feat_id=feat_id, task=task, url=url, max_cycles=max_cycles)
    notify(f"Startar {feat_id}: {task[:80]}")

    proj = _setup_project(feat_id)
    _log(log, "project_created", path=str(proj))

    # ─── Steg 1: Micke – spec + testplan ────────────────────────────────── #
    notify("Steg 1/4 – Micke skriver SPEC + TESTPLAN...")
    try:
        spec_data = step1_micke_spec(task, url, log, proj)
    except Exception as e:
        _log(log, "step1_error", error=str(e))
        return _result(feat_id, task, proj, log, "error", error=str(e))

    tc_count = len(spec_data.get("testcases", []))
    notify(f"  Gate 1 OK – {tc_count} testfall skapade")

    # ─── Steg 2→4: Zack→Johan→Micke (loop) ─────────────────────────────── #
    verdict = "pending"
    required_changes: list[str] = []
    cycle_summaries: list[dict] = []

    for cycle in range(1, max_cycles + 1):
        is_fix = cycle > 1
        notify(f"Steg 2/4 – Zack implementerar (cykel {cycle}/{max_cycles})...")
        try:
            step2_zack_impl(task, proj, log,
                            is_fix=is_fix,
                            required_changes=required_changes)
        except Exception as e:
            _log(log, "step2_error", error=str(e))
            break

        notify(f"Steg 3/4 – Johan testar (cykel {cycle}/{max_cycles})...")
        try:
            test_data = step3_johan_test(proj, log)
        except Exception as e:
            _log(log, "step3_error", error=str(e))
            break

        passed = sum(1 for t in test_data.get("test_results", []) if t.get("pass"))
        total  = len(test_data.get("test_results", []))
        bugs   = test_data.get("bugs", [])
        blockers = [b for b in bugs if b.get("severity") == "blocker"]
        notify(f"  {passed}/{total} testfall pass | {len(bugs)} buggar ({len(blockers)} blockers)")

        notify(f"Steg 4/4 – Micke reviewar (cykel {cycle}/{max_cycles})...")
        try:
            review_data = step4_micke_review(proj, log)
        except Exception as e:
            _log(log, "step4_error", error=str(e))
            break

        verdict = review_data.get("verdict", "changes_required")
        required_changes = review_data.get("required_changes", [])
        summary = review_data.get("summary", "")

        cycle_summaries.append({
            "cycle": cycle,
            "passed": passed,
            "total": total,
            "bugs": bugs,
            "test_results": test_data.get("test_results", []),
            "verdict": verdict,
            "approved_ac": review_data.get("approved_ac", []),
            "failed_ac": review_data.get("failed_ac", []),
            "required_changes": required_changes,
            "summary": summary,
        })

        if verdict == "approve":
            notify(f"  GODKÄND av Micke! {summary[:100]}")
            break
        else:
            notify(f"  Changes Required: {', '.join(required_changes[:2])}")

    # ─── Resultatjämförelse ──────────────────────────────────────────────── #
    if cycle_summaries:
        _write_result_summary(proj, feat_id, task, spec_data, cycle_summaries)

    # ─── Sammanställ artefakter ──────────────────────────────────────────── #
    _log(log, "done", feat_id=feat_id, verdict=verdict, cycles=cycle)

    artifacts = {
        "SPEC.md":            _read_file(proj, "SPEC.md"),
        "TESTPLAN.md":        _read_file(proj, "TESTPLAN.md"),
        "DOD.md":             _read_file(proj, "DOD.md"),
        "TESTREPORT.md":      _read_file(proj, "TESTREPORT.md"),
        "RUNBOOK.md":         _read_file(proj, "RUNBOOK.md"),
        "CHANGELOG.md":       _read_file(proj, "CHANGELOG.md"),
        "RESULT_SUMMARY.md":  _read_file(proj, "RESULT_SUMMARY.md"),
    }
    src_files = _list_files(proj, "src")
    bug_files = _list_files(proj, "bugs")

    return {
        "feat_id": feat_id,
        "task": task,
        "status": "approved" if verdict == "approve" else "changes_required" if verdict == "changes_required" else "error",
        "cycles": cycle,
        "project_path": str(proj),
        "artifacts": artifacts,
        "src_files": src_files,
        "bug_files": bug_files,
        "required_changes": required_changes,
        "cycle_summaries": cycle_summaries,
        "log": log,
    }


def _result(feat_id, task, proj, log, status, **kw):
    return {
        "feat_id": feat_id, "task": task, "status": status,
        "cycles": 0, "project_path": str(proj),
        "artifacts": {}, "src_files": {}, "bug_files": {},
        "required_changes": [], "log": log, **kw,
    }


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    if os.environ.get("LOCAL_AGENT_TOOL_MODE") == "1":
        data = json.loads(sys.stdin.read())
        result = run(
            task=data["task"],
            url=data.get("url"),
            feat_id=data.get("feat_id"),
            max_cycles=int(data.get("max_cycles", 4)),
        )
        print(json.dumps(result, ensure_ascii=False))
    else:
        import argparse
        parser = argparse.ArgumentParser(description="Agent-team: Micke + Zack + Johan")
        parser.add_argument("task", help="Uppgiftsbeskrivning")
        parser.add_argument("--url", default=None, help="Relevant URL")
        parser.add_argument("--feat-id", default=None, help="FEAT-ID (skapas auto)")
        parser.add_argument("--cycles", type=int, default=4, help="Max cyklar (default 4)")
        parser.add_argument("--json", action="store_true", help="Skriv ut hela JSON-resultatet")
        args = parser.parse_args()

        result = run(
            task=args.task,
            url=args.url,
            feat_id=args.feat_id,
            max_cycles=args.cycles,
        )

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"\n{'='*60}")
            print(f"  FEAT: {result['feat_id']}")
            print(f"  Status: {result['status'].upper()}")
            print(f"  Cyklar: {result['cycles']}")
            print(f"  Projektmapp: {result['project_path']}")
            print(f"{'='*60}")
            if result.get("required_changes"):
                print("\nRequired changes:")
                for c in result["required_changes"]:
                    print(f"  - {c}")
            print(f"\nLogg ({len(result['log'])} händelser):")
            for entry in result["log"]:
                t = entry["time"][11:19]
                ev = entry["event"]
                ag = f"[{entry['agent']}] " if entry.get("agent") else ""
                print(f"  {t} {ag}{ev}")
