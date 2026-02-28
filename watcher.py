"""
watcher.py
----------
Startar bot.py och håller den igång.
Kollar git var CHECK_INTERVAL sekunder – vid ny commit:
  - Om kärnfiler ändrades (bot.py, session_store.py m.fl.): stop → pull → pip install → start
  - Om bara tool-filer ändrades: pull utan restart (tools laddas dynamiskt)

Körs via start.bat istället för att anropa bot.py direkt.
"""

import subprocess
import sys
import time

CHECK_INTERVAL = 60  # sekunder mellan git-kontroller

# Filer som kräver restart om de ändras
RESTART_FILES = {"bot.py", "watcher.py", "session_store.py", "requirements.txt"}


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


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


def start_bot() -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "bot.py"])
    print(f"[watcher] Bot startad (PID {proc.pid})", flush=True)
    return proc


def stop_bot(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    print(f"[watcher] Bot stoppad (PID {proc.pid})", flush=True)


def main() -> None:
    proc = start_bot()

    while True:
        time.sleep(CHECK_INTERVAL)

        # Kolla om boten kraschade
        if proc.poll() is not None:
            print(f"[watcher] Bot avslutades (kod {proc.returncode}) – startar om...", flush=True)
            proc = start_bot()
            continue

        # Kolla git-uppdateringar
        git_fetch()
        loc = local_hash()
        rem = remote_hash()

        if loc == rem:
            continue

        print(f"[watcher] Uppdatering hittad! {loc[:7]} → {rem[:7]}", flush=True)
        restart = needs_restart(loc, rem)
        git_pull()

        if restart:
            print("[watcher] Kärnfiler ändrade – startar om boten.", flush=True)
            stop_bot(proc)
            pip_install()
            proc = start_bot()
        else:
            print("[watcher] Bara tool-filer uppdaterade – ingen restart.", flush=True)


if __name__ == "__main__":
    main()
