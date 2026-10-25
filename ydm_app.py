"""YDM entry point.

    python ydm_app.py                       open the window
    python ydm_app.py --minimized           start in the background (used by autostart / native host)
    python ydm_app.py /d URL [/p DIR] [/f NAME] [/n] [/a] [/s]
        IDM-compatible command line:
        /d URL  download URL        /p DIR  save to folder     /f NAME  file name
        /n      silent (no dialog)  /a      add to queue only  /s       start the queue
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ydm import api  # noqa: E402


def parse_cli(argv):
    opts, i = {}, 0
    flags = {"/n": "silent", "/a": "queue", "/s": "start_queue", "--minimized": "minimized"}
    values = {"/d": "url", "/p": "dir", "/f": "filename"}
    while i < len(argv):
        a = argv[i].lower()
        if a in values and i + 1 < len(argv):
            opts[values[a]] = argv[i + 1]
            i += 2
            continue
        if a in flags:
            opts[flags[a]] = True
        i += 1
    return opts


def app_running():
    try:
        return api.call("/ping", timeout=1).get("ok", False)
    except Exception:
        return False


def launch_background():
    """Start the GUI process detached and wait until its API answers."""
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    cmd = [str(pyw if pyw.exists() else exe), str(Path(__file__).resolve()), "--minimized"]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        time.sleep(0.2)
        if app_running():
            return True
    return False


def main():
    opts = parse_cli(sys.argv[1:])
    payload = None
    if opts.get("url"):
        payload = {"url": opts["url"], "filename": opts.get("filename", ""), "silent": opts.get("silent", False)}
        if opts.get("dir"):
            payload["directory"] = opts["dir"]
    if app_running():
        if payload:
            if opts.get("queue"):
                payload["queue"] = True
            api.call("/add", payload)
        else:
            api.call("/show", {})
        return
    if opts.get("url") is None and opts.get("start_queue") and not opts.get("minimized"):
        opts["minimized"] = True
    from ydm import gui
    gui.run(start_minimized=opts.get("minimized", False), initial=payload)


if __name__ == "__main__":
    main()
