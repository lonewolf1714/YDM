"""Find or start the YDM app, both from source (python ydm_app.py) and from the installed YDM.exe."""
import subprocess
import sys
import time
from pathlib import Path

from . import api

FROZEN = getattr(sys, "frozen", False)
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def install_dir() -> Path:
    """Folder holding YDM.exe (installed) or ydm_app.py (source checkout)."""
    return Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parents[1]


def app_command(*args):
    if FROZEN:
        return [str(install_dir() / "YDM.exe"), *args]
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return [str(pyw if pyw.exists() else exe), str(install_dir() / "ydm_app.py"), *args]


def app_running():
    try:
        return api.call("/ping", timeout=1).get("ok", False)
    except Exception:
        return False


def launch_background():
    """Start the app minimized and wait until its API answers.

    Browsers run native hosts inside a job object that is closed when the host exits;
    breaking away from it keeps the app alive after the host process ends."""
    base = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    for flags in (base | CREATE_BREAKAWAY_FROM_JOB, base):
        try:
            subprocess.Popen(app_command("--minimized"), creationflags=flags, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            break
        except OSError:  # the job does not allow breakaway
            continue
    for _ in range(75):
        time.sleep(0.2)
        if app_running():
            return True
    return False
