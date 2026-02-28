"""
watcher.py
----------
Startar bot.py och håller den igång.
Kollar git var CHECK_INTERVAL sekunder – vid ny commit:
  - Auto-mergar claude-branchen till main med -X theirs (Claude vinner alltid)
  - Om kärnfiler ändrades (bot.py, session_store.py m.fl.): stop → pull → pip install → start
  - Om bara tool-filer ändrades: pull utan restart (tools laddas dynamiskt)

Körs via start.bat istället för att anropa bot.py direkt.
"""

import subprocess
import sys
import time
from datetime import datetime

CHECK_INTERVAL = 60  # sekunder mellan git-kontroller
CLAUDE_BRANCH = "claude/explain-codebase-mlw2u0afar3taosd-erup8"

# Filer som kräver restart om de ändras
RESTART_FILES = {"bot.py", "watcher.py", "session_store.py", "requirements.txt"}


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def git_fetch() -> None:
    _run(["git", "fetch", "origin", "main"])


def local_hash() -> str:
    return _run(["git", "rev-parse", "HEAD"]).stdout.strip()


def remote_hash() -> str:
    return _run(["git", "rev-parse", "origin/main"]).stdout.strip()


def git_pull() -> None:
    subprocess.run(["git", "pull", "origin", "main"])


def pip_install() -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"])


def changed_files(old_hash: str, new_hash: str) -> set[str]:
    r = _run(["git", "diff", "--name-only", old_hash, new_hash])
    return set(r.stdout.strip().splitlines())


def needs_restart(old_hash: str, new_hash: str) -> bool:
    return bool(changed_files(old_hash, new_hash) & RESTART_FILES)


def auto_merge_claude() -> None:
    """Mergar claude-branchen till main automatiskt om det finns nya commits."""
    _run(["git", "fetch", "origin", CLAUDE_BRANCH])
    claude_hash = _run(["git", "rev-parse", f"origin/{CLAUDE_BRANCH}"]).stdout.strip()
    if not claude_hash:
        return

    # Redan mergad? Kolla om claude-hash är en ancestor av HEAD
    already_merged = _run(["git", "merge-base", "--is-ancestor", claude_hash, "HEAD"]).returncode == 0
    if already_merged:
        return

    print(f"[watcher] {ts()} Claude-branch har ny commit – auto-mergar (-X theirs)...", flush=True)
    r = _run(["git", "merge", "-X", "theirs", "--no-edit", f"origin/{CLAUDE_BRANCH}"])
    if r.returncode != 0:
        print(f"[watcher] {ts()} Merge-fel:\n{r.stderr}", flush=True)
        return

    push = _run(["git", "push", "origin", "main"])
    if push.returncode == 0:
        print(f"[watcher] {ts()} Merge klar och pushad till main.", flush=True)
    else:
        print(f"[watcher] {ts()} Merge klar men push misslyckades:\n{push.stderr}", flush=True)


def start_bot() -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "bot.py"])
    print(f"[watcher] {ts()} Bot startad (PID {proc.pid})", flush=True)
    return proc


def stop_bot(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    print(f"[watcher] {ts()} Bot stoppad (PID {proc.pid})", flush=True)


def main() -> None:
    proc = start_bot()

    while True:
        time.sleep(CHECK_INTERVAL)

        # Kolla om boten kraschade
        if proc.poll() is not None:
            print(f"[watcher] {ts()} Bot avslutades (kod {proc.returncode}) – startar om...", flush=True)
            proc = start_bot()
            continue

        old_hash = local_hash()

        # Auto-merga claude-branchen om ny commit finns
        auto_merge_claude()

        # Hämta senaste main (externa ändringar)
        git_fetch()
        loc = local_hash()
        rem = remote_hash()
        if loc != rem:
            git_pull()

        new_hash = local_hash()

        if new_hash == old_hash:
            continue

        print(f"[watcher] {ts()} Uppdatering hittad! {old_hash[:7]} → {new_hash[:7]}", flush=True)

        if needs_restart(old_hash, new_hash):
            print(f"[watcher] {ts()} Kärnfiler ändrade – startar om boten.", flush=True)
            stop_bot(proc)
            pip_install()
            proc = start_bot()
        else:
            print(f"[watcher] {ts()} Bara tool-filer uppdaterade – ingen restart.", flush=True)


if __name__ == "__main__":
    main()
