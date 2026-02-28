@echo off
:: Hämtar och mergar senaste Claude-branchen automatiskt.
:: Väljer alltid inkommande ändringar (-X theirs) vid konflikt.

set BRANCH=claude/explain-codebase-mlw2u0afar3taosd-erup8

echo [merge] Hämtar origin...
git fetch origin

echo [merge] Mergar %BRANCH% med -X theirs...
git merge -X theirs origin/%BRANCH% --no-edit

if %ERRORLEVEL% neq 0 (
    echo [merge] FEL: merge misslyckades.
    pause
    exit /b 1
)

echo [merge] Klar! Pushar main...
git push origin main

echo [merge] Done.
pause
