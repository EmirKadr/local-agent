"""
watcher.py
----------
Startar bot.py och håller den igång.
Kollar git var CHECK_INTERVAL sekunder – vid ny commit:
  1. Stänger ner boten
  2. Kör git pull
  3. Installerar ev. nya beroenden
  4. Startar om boten

Körs via start.bat istället för att anropa bot.py direkt.
"""

import subprocess
import sys
import time

CHECK_INTERVAL = 60  # sekunder mellan git-kontroller


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
        stop_bot(proc)
        git_pull()
        pip_install()
        proc = start_bot()


if __name__ == "__main__":
    main()
