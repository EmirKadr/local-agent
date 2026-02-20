# Förslag: automatiserad Codex-loop (fixa → testa → merge → förbättra)

Det här är ett praktiskt upplägg för att automatisera en "självläkande" utvecklingsloop med Codex.

## Målbild

1. **Codex får ett issue** och skapar PR med kodfix.
2. **CI kör tester, lint och säkerhetskontroller**.
3. **Automatisk merge** sker bara om alla kvalitetsgrindar passerar.
4. Efter merge: **Codex får ny feedback** baserat på testresultat, incidenter och kodkvalitet för nästa förbättring.

## Rekommenderad arkitektur

- **Branch protection**: kräv gröna checks, review policy, och blockerad direkt-push till `main`.
- **CI workflow** (pull_request):
  - installera dependencies
  - kör tester
  - kör lint/type checks
  - publicera testresultat/coverage som artifact
- **Automerge workflow**:
  - aktivera automerge på PR när checks är gröna
  - eller explicit merge-jobb med policy-kontroll
- **Post-merge workflow** (`push` på `main`):
  - sammanfatta resultat från CI
  - skapa strukturerat "förbättringsprompt" till Codex
  - öppna nytt issue (eller starta nästa Codex-jobb) med prioriterade förbättringar

## Promptmall till Codex efter merge

Använd en strikt mall så att nästa iteration blir konsekvent:

```text
Context:
- Senast mergad PR: <id>
- Berörda filer: <lista>
- Teststatus: <pass/fail + detaljer>
- Fel/varningar från produktion eller loggar: <lista>

Task:
1) Föreslå 3 förbättringar med störst nytta/riskreduktion.
2) Implementera #1 i en ny branch.
3) Lägg till/uppdatera tester.
4) Öppna PR med tydlig risk- och rollback-plan.

Constraints:
- Ingen förändring utan test.
- Behåll bakåtkompatibilitet.
- Max 1 feature-ändring per PR.
```

## Governance (viktigt)

- Sätt **max antal autonoma iterationer** (t.ex. 1–2) innan mänsklig checkpoint.
- Kräv **human approval** för högrisk-filer (auth, betalning, datamigrering).
- Lägg in **budget- och tidsgräns** per körning.
- Logga alla agentbeslut för spårbarhet.

## Minimal startplan (30–60 min)

1. Aktivera branch protection i repo.
2. Lägg in en CI-workflow som alltid kör test/lint på PR.
3. Aktivera automerge när checks är gröna.
4. Lägg in post-merge-jobb som skapar ett nytt förbättrings-issue till Codex.
5. Mät: lead time, failed builds, antal regressions per vecka.

## KPI:er att följa

- PR lead time
- Change failure rate
- MTTR (mean time to recovery)
- Andel PR med testtäckning
- Reopen-rate på buggar

Med detta får ni en kontrollerad "closed-loop" där Codex kontinuerligt förbättrar kodbasen utan att tumma på kvalitet och styrning.
