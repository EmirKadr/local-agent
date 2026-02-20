# Helautomatisk förbättringsloop för boten (20 testfrågor → utvärdering → Codex-fix → commit/push → omtest)

Den här guiden beskriver ett praktiskt sätt att köra en helt automatisk kvalitetsloop för din bot.

## Vad du vill uppnå

För varje iteration ska systemet:
1. ställa 20 fasta testfrågor till boten,
2. mäta svarkvalitet mot dina kriterier,
3. bygga ett förbättringsunderlag,
4. låta Codex implementera förbättringar,
5. committa + pusha ändringarna,
6. starta om boten,
7. köra samma 20 frågor igen,
8. fortsätta tills mål uppnås eller max antal rundor är nådd.

## Arkitektur (rekommenderad)

Bygg loopen i fyra separata delar:

- **A. Testkörning (Evaluator Runner)**
  - Läser `tests/questions.yaml` med 20 frågor.
  - Anropar boten via API eller Telegram-testkonto.
  - Sparar råsvar i `artifacts/run_<timestamp>/answers.json`.

- **B. Bedömning (Quality Judge)**
  - Jämför varje svar mot regler i `tests/rubric.yaml`.
  - Sätter delpoäng per fråga, t.ex. korrekthet, tydlighet, policy-following.
  - Skriver `scorecard.json` + `improvement_prompt.md`.

- **C. Kodagent (Codex Worker)**
  - Startas med förbättringsprompten.
  - Gör kodändringar i ny branch.
  - Kör lokala tester.
  - Commit + push automatiskt.

- **D. Orkestrering (Loop Controller)**
  - Driver hela sekvensen i ordning.
  - Stoppar på fel, loggar allt, och skickar notis.

## Förslag på filstruktur

```text
automation/
  loop_controller.py
  run_questions.py
  evaluate_answers.py
  build_codex_prompt.py
  codex_worker.py
  restart_bot.sh
tests/
  questions.yaml
  rubric.yaml
artifacts/
  run_2026-.../
```

## Steg 1: Definiera dina 20 testfrågor

Skapa `tests/questions.yaml` med stabil uppsättning.

Exempel:

```yaml
- id: q01
  question: "Hur startar jag agent-läge?"
  must_include:
    - "agent"
    - "start"
  forbidden:
    - "vet inte"

- id: q02
  question: "Hur installerar jag dependencies?"
  must_include:
    - "pip install -r requirements.txt"
```

Tips:
- Håll frågorna versionskontrollerade.
- Dela upp: enkla, edge cases, felaktig input, policy-säkerhet.

## Steg 2: Definiera bedömningsrubric

Skapa `tests/rubric.yaml`:

```yaml
weights:
  correctness: 0.5
  relevance: 0.2
  clarity: 0.2
  safety: 0.1
thresholds:
  pass_score: 0.85
  min_correctness: 0.75
hard_fail:
  - hallucination
  - unsafe_instruction
```

Bedömningsmotor ska ge:
- total score (0–1),
- score per fråga,
- top 5 svagheter,
- konkret fixlista ("ändra router", "lägg fallback", "förbättra prompt").

## Steg 3: Kör 20 frågor automatiskt

`automation/run_questions.py` ska:
- starta session (nytt testkontext-ID),
- skicka alla 20 frågor i ordning,
- vänta på svar med timeout/retry,
- logga latens + svarstext.

Exempelkommando:

```bash
python automation/run_questions.py \
  --questions tests/questions.yaml \
  --out artifacts/latest/answers.json
```

## Steg 4: Utvärdera kvalitet och bygg förbättringsprompt

1) Kör evaluator:

```bash
python automation/evaluate_answers.py \
  --answers artifacts/latest/answers.json \
  --rubric tests/rubric.yaml \
  --out artifacts/latest/scorecard.json
```

2) Bygg prompt till Codex:

```bash
python automation/build_codex_prompt.py \
  --score artifacts/latest/scorecard.json \
  --out artifacts/latest/improvement_prompt.md
```

Prompten bör innehålla:
- exakt vad som failade,
- vilka filer sannolikt berörs,
- krav på test som måste läggas till,
- tydligt acceptanskriterium (t.ex. `score >= 0.85`).

## Steg 5: Låt Codex fixa, committa och pusha

`automation/codex_worker.py` kan köra ungefär:

```bash
codex exec --prompt-file artifacts/latest/improvement_prompt.md
pytest -q
git add -A
git commit -m "Auto-fix: improve bot response quality (run ${RUN_ID})"
git push origin HEAD
```

Bra skydd:
- commit endast om tester passerar,
- max antal filer/LOC per iteration,
- blocklista för högriskfiler.

## Steg 6: Starta om boten och kör om testsetet

Exempel `automation/restart_bot.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
pkill -f "python bot.py" || true
nohup python bot.py > bot.log 2>&1 &
sleep 3
```

Sedan:
- kör `run_questions.py` igen,
- jämför ny score mot föregående,
- spara diff-rapport.

## Steg 7: Styr loopens stoppvillkor

I `loop_controller.py`, sätt t.ex.:
- `MAX_ITERS=5`
- stoppa om `score >= 0.90`
- stoppa om förbättring < `+0.01` i två rundor
- stoppa direkt vid hard fail

Pseudoflöde:

```text
for iter in 1..MAX_ITERS:
  run_questions
  evaluate
  if pass: break
  build_prompt
  codex_worker (fix + test + commit + push)
  restart_bot
```

## CI/CD-variant (helt utan manuell handpåläggning)

Du kan flytta loopen till GitHub Actions:

1. Scheduled workflow (t.ex. varje natt) kör 20 frågor.
2. Vid fail skapas issue + Codex-jobb.
3. Codex öppnar PR.
4. CI verifierar (tester + evaluator).
5. Auto-merge vid grönt.
6. Deploy + omtest.

Fördel: fullt spårbart, reproducerbart, revisionsvänligt.

## Kvalitet och säkerhet (viktigt)

Lägg alltid in:
- **Branch protection** (inga direkta pushes till main).
- **Krav på gröna tester** före commit/push i automation.
- **Budgetgräns** (antal iterationer + tidsgräns).
- **Audit-logg**: prompt, diff, testresultat, commit SHA per varv.

## Minimal "kom igång"-checklista

1. Skapa `questions.yaml` (20 frågor).
2. Skapa `rubric.yaml` med viktning och passgräns.
3. Implementera `run_questions.py` och `evaluate_answers.py`.
4. Implementera `build_codex_prompt.py`.
5. Implementera `codex_worker.py` (inkl. commit/push).
6. Implementera `restart_bot.sh`.
7. Lägg allt i `loop_controller.py` och testa 1 iteration lokalt.
8. Lägg i CI när lokal loop är stabil.

När detta är gjort har du exakt den kedja du efterfrågar: **fråga 20 gånger → bedömning → Codex-fix → commit/push → restart → retest**, helt automatiserat.
