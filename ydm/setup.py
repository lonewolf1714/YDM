"""Installer and uninstaller for the packaged app (YDM-Setup.exe / YDM.exe --uninstall).

Everything is per-user (no admin rights): files go to %LOCALAPPDATA%\\Programs\\YDM, browser
registration, autostart and the "Installed apps" entry go under HKEY_CURRENT_USER.
The native-host manifest is kept inside the install folder rather than %APPDATA%.
"""
import json
import os
import shutil
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import api, config

HOST_ID = "com.ydm.host"
EXTENSION_ID = "jimfaknamlepkebnaemlonbhcemcoimc"  # fixed by the "key" in extension/manifest.json
BROWSERS = {
    "Google Chrome": r"Software\Google\Chrome\NativeMessagingHosts",
    "Microsoft Edge": r"Software\Microsoft\Edge\NativeMessagingHosts",
    "Brave": r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts",
    "Chromium": r"Software\Chromium\NativeMessagingHosts",
    "Vivaldi": r"Software\Vivaldi\NativeMessagingHosts",
    "Opera": r"Software\Opera Software\NativeMessagingHosts",
}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\YDM"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def default_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / "YDM"


def payload_dir() -> Path:
    """The packaged app inside YDM-Setup.exe."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1] / "dist")) / "payload"


# ------------------------------------------------------------------ registry helpers
def _reg_set(path, name, value):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ, value)


def _reg_delete_tree(path):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    _reg_delete_tree(path + "\\" + winreg.EnumKey(k, 0))
                except OSError:
                    break
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        pass


def _reg_delete_value(path, name):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except OSError:
        pass


# ------------------------------------------------------------------ steps
def stop_running_app():
    try:
        api.call("/quit", {}, timeout=2)
    except Exception:
        pass
    for exe in ("YDM.exe", "ydm-host.exe"):
        subprocess.run(["taskkill", "/f", "/im", exe], capture_output=True, creationflags=NO_WINDOW)


def copy_files(src: Path, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, dirs_exist_ok=True)


def register_browsers(dest: Path):
    host_dir = dest / "host"
    host_dir.mkdir(parents=True, exist_ok=True)
    manifest = host_dir / f"{HOST_ID}.json"
    manifest.write_text(json.dumps({
        "name": HOST_ID, "description": "YDM download manager", "path": str(dest / "ydm-host.exe"),
        "type": "stdio", "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"]}, indent=2), "utf-8")
    for key in BROWSERS.values():
        _reg_set(f"{key}\\{HOST_ID}", "", str(manifest))


def _shell_folders():
    ps = "[Environment]::GetFolderPath('Programs'); [Environment]::GetFolderPath('Desktop')"
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout.split("\n")
    return Path(out[0].strip()), Path(out[1].strip())


def create_shortcuts(dest: Path, desktop: bool):
    programs, desk = _shell_folders()
    links = [programs / "YDM.lnk"] + ([desk / "YDM.lnk"] if desktop else [])
    script = "$s = New-Object -ComObject WScript.Shell; " + "; ".join(
        f"$l = $s.CreateShortcut('{lnk}'); $l.TargetPath = '{dest / 'YDM.exe'}'; "
        f"$l.WorkingDirectory = '{dest}'; $l.IconLocation = '{dest / 'YDM.exe'},0'; "
        f"$l.Description = 'YDM download manager'; $l.Save()" for lnk in links)
    subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, creationflags=NO_WINDOW)


def set_autostart(dest: Path, on: bool):
    if on:
        _reg_set(RUN_KEY, config.APP_NAME, f'"{dest / "YDM.exe"}" --minimized')
    else:
        _reg_delete_value(RUN_KEY, config.APP_NAME)


def register_uninstaller(dest: Path, version: str):
    size_kb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) // 1024
    for name, value in {
        "DisplayName": "YDM Download Manager", "DisplayVersion": version, "Publisher": "YDM",
        "DisplayIcon": f"{dest / 'YDM.exe'},0", "InstallLocation": str(dest),
        "UninstallString": f'"{dest / "YDM.exe"}" --uninstall', "EstimatedSize": int(size_kb),
        "NoModify": 1, "NoRepair": 1,
    }.items():
        _reg_set(UNINSTALL_KEY, name, value)


def install(dest: Path, desktop=True, autostart=True, browsers=True, shortcuts=True, log=print, files_only=False):
    log("Closing YDM if it is running…")
    stop_running_app()
    log("Copying files…")
    copy_files(payload_dir(), dest)
    if files_only:  # used by the build test: no registry, no shortcuts
        return
    if browsers:
        log("Registering with Chrome, Edge, Brave, Vivaldi and Opera…")
        register_browsers(dest)
    if shortcuts:
        log("Creating shortcuts…")
        create_shortcuts(dest, desktop)
    log("Configuring startup…")
    set_autostart(dest, autostart)
    register_uninstaller(dest, api.VERSION)
    log("Done.")


def uninstall(dest: Path):
    stop_running_app()
    for key in BROWSERS.values():
        _reg_delete_tree(f"{key}\\{HOST_ID}")
    set_autostart(dest, False)
    _reg_delete_tree(UNINSTALL_KEY)
    programs, desk = _shell_folders()
    for lnk in (programs / "YDM.lnk", desk / "YDM.lnk"):
        lnk.unlink(missing_ok=True)
    # The running YDM.exe cannot delete its own folder; let cmd do it once we have exited.
    subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 3 >nul & rmdir /s /q "{dest}"',
                     creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0), close_fds=True)


def uninstall_interactive():
    dest = Path(sys.executable).parent
    root = tk.Tk()
    root.withdraw()
    if not messagebox.askyesno("Uninstall YDM", "Remove YDM Download Manager from this computer?\n\n"
                               "Your downloaded files are kept. Remove the browser extension yourself "
                               "from chrome://extensions."):
        return
    keep = messagebox.askyesno("Uninstall YDM", "Keep your YDM settings and download list?")
    if not keep:
        shutil.rmtree(config.DATA_DIR, ignore_errors=True)
    uninstall(dest)
    messagebox.showinfo("Uninstall YDM", "YDM has been removed.")


# ------------------------------------------------------------------ wizard
class Wizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YDM Download Manager Setup")
        self.geometry("560x400")
        self.resizable(False, False)
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        try:
            self.iconbitmap(str(payload_dir() / "YDM.exe"))
        except tk.TclError:
            pass
        head = tk.Frame(self, bg="#1565c0", height=70)
        head.pack(fill="x")
        tk.Label(head, text="YDM Download Manager", bg="#1565c0", fg="white",
                 font=("Segoe UI", 16, "bold")).pack(anchor="w", padx=18, pady=(12, 0))
        self.sub = tk.Label(head, text=f"Version {api.VERSION} setup", bg="#1565c0", fg="#dbe8fb",
                            font=("Segoe UI", 9))
        self.sub.pack(anchor="w", padx=18, pady=(0, 10))
        self.body = ttk.Frame(self, padding=18)
        self.body.pack(fill="both", expand=True)
        ttk.Separator(self).pack(fill="x")
        self.buttons = ttk.Frame(self, padding=10)
        self.buttons.pack(fill="x")
        self.dest = tk.StringVar(value=str(default_dir()))
        self.desktop, self.autostart, self.launch = tk.BooleanVar(value=True), tk.BooleanVar(value=True), tk.BooleanVar(value=True)
        self.page_options()

    def _clear(self):
        for w in list(self.body.winfo_children()) + list(self.buttons.winfo_children()):
            w.destroy()

    def page_options(self):
        self._clear()
        ttk.Label(self.body, text="Install YDM for your Windows account (no administrator rights needed).",
                  wraplength=500).pack(anchor="w")
        ttk.Label(self.body, text="Install to:").pack(anchor="w", pady=(14, 2))
        row = ttk.Frame(self.body)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.dest).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=lambda: self.dest.set(
            filedialog.askdirectory(initialdir=self.dest.get()) or self.dest.get())).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(self.body, text="Create a desktop shortcut", variable=self.desktop).pack(anchor="w", pady=(14, 2))
        ttk.Checkbutton(self.body, text="Start YDM with Windows (needed for instant browser downloads)",
                        variable=self.autostart).pack(anchor="w", pady=2)
        ttk.Label(self.body, text="Setup also connects YDM to Chrome, Edge, Brave, Vivaldi and Opera.",
                  foreground="#555").pack(anchor="w", pady=(14, 0))
        ttk.Button(self.buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(self.buttons, text="Install", command=self.page_install).pack(side="right", padx=6)

    def page_install(self):
        self._clear()
        ttk.Label(self.body, text="Installing…", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        bar = ttk.Progressbar(self.body, mode="indeterminate")
        bar.pack(fill="x", pady=10)
        bar.start(12)
        status = ttk.Label(self.body, text="")
        status.pack(anchor="w")
        self.update()
        try:
            install(Path(self.dest.get()), desktop=self.desktop.get(), autostart=self.autostart.get(),
                    log=lambda t: (status.configure(text=t), self.update()))
        except Exception as e:
            bar.stop()
            messagebox.showerror("YDM Setup", f"Installation failed:\n{e}", parent=self)
            self.page_options()
            return
        self.page_done()

    def page_done(self):
        self._clear()
        dest = Path(self.dest.get())
        ttk.Label(self.body, text="YDM is installed.", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(self.body, wraplength=510, justify="left", text=(
            "One last step, because browsers only let you add extensions yourself:\n"
            "1. Open chrome://extensions (or edge://extensions) and turn on Developer mode.\n"
            "2. Click \"Load unpacked\" and choose the extension folder below.\n"
            "   If YDM's extension is already loaded from this folder, just click its reload icon.\n"
            "3. Refresh any open YouTube tabs.")).pack(anchor="w", pady=(10, 6))
        ext = dest / "extension"
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=4)
        e = ttk.Entry(row)
        e.insert(0, str(ext))
        e.configure(state="readonly")
        e.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Copy", command=lambda: (self.clipboard_clear(), self.clipboard_append(str(ext)))).pack(side="left", padx=4)
        ttk.Button(row, text="Open folder", command=lambda: os.startfile(ext)).pack(side="left")
        ttk.Checkbutton(self.body, text="Launch YDM now", variable=self.launch).pack(anchor="w", pady=(12, 0))

        def finish():
            if self.launch.get():
                subprocess.Popen([str(dest / "YDM.exe")], close_fds=True)
            self.destroy()
        ttk.Button(self.buttons, text="Finish", command=finish).pack(side="right")


def main(argv=None):
    """YDM-Setup.exe [/S] [/D=folder] [--no-browsers] [--no-shortcuts] [--no-autostart] [--files-only]"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "/S" in argv or "/s" in argv:
        dest = next((Path(a[3:]) for a in argv if a.upper().startswith("/D=")), default_dir())
        install(dest, desktop="--no-shortcuts" not in argv, autostart="--no-autostart" not in argv,
                browsers="--no-browsers" not in argv, shortcuts="--no-shortcuts" not in argv,
                files_only="--files-only" in argv)
        return
    Wizard().mainloop()
