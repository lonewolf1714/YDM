# PyInstaller spec: one folder holding YDM.exe (the app) and ydm-host.exe (the browser's native host).
# Built by tools/build.py; run from the repository root.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(SPECPATH, "ydm.ico")

app = Analysis([os.path.join(ROOT, "ydm_app.py")], pathex=[ROOT], excludes=["pytest", "unittest"],
               hiddenimports=["pystray._win32"])  # pystray picks its backend at runtime
host = Analysis([os.path.join(ROOT, "ydm", "native_host.py")], pathex=[ROOT], excludes=["tkinter", "pytest", "unittest"])

app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="YDM", icon=ICON,
              console=False, version=os.path.join(SPECPATH, "version.txt"))
# Console subsystem so stdin/stdout are real pipes; browsers start native hosts without a window.
host_exe = EXE(PYZ(host.pure), host.scripts, [], exclude_binaries=True, name="ydm-host", icon=ICON,
               console=True, version=os.path.join(SPECPATH, "version.txt"))

COLLECT(app_exe, app.binaries, app.datas, host_exe, host.binaries, host.datas, name="YDM")
