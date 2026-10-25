"""Tkinter desktop UI modelled on IDM's layout: categories on the left, download list on the right,
a "Download File Info" dialog for new downloads, a per-download progress window with a
connection map, a completion dialog, options, scheduler and batch downloads."""
import datetime
import os
import queue
import re
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import api, config, engine as engine_mod, ytdlp

URL_RE = re.compile(r"^(https?|ftp)://\S+$", re.I)
CATEGORY_ICONS = {"All Downloads": "📁", "Compressed": "🗜", "Documents": "📄", "Music": "🎵",
                  "Programs": "💿", "Video": "🎬", "General": "📦", "Unfinished": "⏳",
                  "Finished": "✅", "Queues": "📋"}
STATUS_TEXT = {"stopped": "Stopped", "queued": "Queued", "connecting": "Connecting…",
               "downloading": None, "merging": "Merging…", "complete": "Complete", "error": "Error"}


def fmt_size(n):
    if n is None or n < 0:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def fmt_time(sec):
    if sec is None:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {s:02d}s" if m else f"{s}s")


def open_path(p):
    try:
        os.startfile(str(p))  # type: ignore[attr-defined]
    except (AttributeError, OSError) as e:
        messagebox.showerror(config.APP_NAME, str(e))


def reveal(p):
    if os.name == "nt":
        subprocess.Popen(["explorer", "/select,", str(p)] if Path(p).exists() else ["explorer", str(Path(p).parent)])
    else:
        open_path(Path(p).parent)


class App(tk.Tk):
    def __init__(self, eng: engine_mod.Engine, start_minimized=False):
        super().__init__()
        self.engine = eng
        self.settings = eng.settings
        self.ui_queue = queue.Queue()
        self.progress_windows: dict[str, ProgressDialog] = {}
        self.filter = "All Downloads"
        self.last_clip = ""
        self.scheduler_fired = set()

        self.title(f"{config.APP_NAME} — Download Manager")
        self.geometry("1100x600")
        self.minsize(800, 400)
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=24)
        style.configure("Tool.TButton", padding=(8, 6))

        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind_all("<Control-n>", lambda e: self.add_url())
        self.bind_all("<Delete>", lambda e: self.delete_selected())

        self.api = api.ApiServer(eng, self.ui_queue)
        try:
            self.api.start()
        except OSError:
            messagebox.showwarning(config.APP_NAME, f"Port {config.API_PORT} is busy; browser integration is off.")
        try:
            self.last_clip = self.clipboard_get()
        except tk.TclError:
            pass
        if start_minimized:
            self.iconify()
        self.refresh_list()
        self.after(500, self._poll)
        self.after(1500, self._clipboard_poll)
        self.after(5000, self._scheduler_poll)

    # ------------------------------------------------------------------ layout
    def _build_menu(self):
        m = tk.Menu(self)
        tasks = tk.Menu(m, tearoff=0)
        tasks.add_command(label="Add new download…", accelerator="Ctrl+N", command=self.add_url)
        tasks.add_command(label="Add batch download…", command=self.batch_dialog)
        tasks.add_command(label="Add batch download from clipboard", command=self.batch_from_clipboard)
        tasks.add_separator()
        tasks.add_command(label="Import list of URLs from text file…", command=self.import_list)
        tasks.add_command(label="Export list of URLs…", command=self.export_list)
        tasks.add_separator()
        tasks.add_command(label="Exit", command=self.on_close)
        m.add_cascade(label="Tasks", menu=tasks)

        dls = tk.Menu(m, tearoff=0)
        dls.add_command(label="Resume", command=self.resume_selected)
        dls.add_command(label="Stop", command=self.stop_selected)
        dls.add_command(label="Stop all", command=self.stop_all)
        dls.add_command(label="Resume all", command=self.resume_all)
        dls.add_separator()
        dls.add_command(label="Delete", command=self.delete_selected)
        dls.add_command(label="Delete all completed", command=self.delete_completed)
        dls.add_separator()
        self.limit_var = tk.BooleanVar(value=self.settings["speed_limit_kbps"] > 0)
        dls.add_checkbutton(label="Speed limiter on", variable=self.limit_var, command=self.toggle_limiter)
        dls.add_command(label="Speed limiter settings…", command=self.limiter_settings)
        dls.add_separator()
        dls.add_command(label="Options…", command=self.options_dialog)
        m.add_cascade(label="Downloads", menu=dls)

        q = tk.Menu(m, tearoff=0)
        q.add_command(label="Start queue", command=self.engine.start_queue)
        q.add_command(label="Stop queue", command=self.engine.stop_queue)
        q.add_command(label="Scheduler…", command=self.scheduler_dialog)
        m.add_cascade(label="Queue", menu=q)

        h = tk.Menu(m, tearoff=0)
        h.add_command(label="About", command=lambda: messagebox.showinfo(
            config.APP_NAME, f"{config.APP_NAME} {api.VERSION}\nOpen-source download manager.\n"
            f"Data folder: {config.DATA_DIR}"))
        m.add_cascade(label="Help", menu=h)
        self.config(menu=m)

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(4, 4))
        bar.pack(fill="x")
        buttons = [("➕  Add URL", self.add_url), ("▶  Resume", self.resume_selected),
                   ("⏸  Stop", self.stop_selected), ("⏹  Stop all", self.stop_all),
                   ("🗑  Delete", self.delete_selected), ("🧹  Delete completed", self.delete_completed),
                   ("⚙  Options", self.options_dialog), ("⏰  Scheduler", self.scheduler_dialog),
                   ("▶  Start queue", self.engine.start_queue), ("⏹  Stop queue", self.engine.stop_queue),
                   ("📋  Batch", self.batch_dialog)]
        for text, cmd in buttons:
            ttk.Button(bar, text=text, command=cmd, style="Tool.TButton").pack(side="left", padx=2)
        ttk.Separator(self).pack(fill="x")

    def _build_body(self):
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True)

        left = ttk.Frame(pane)
        self.cat_tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.cat_tree.pack(fill="both", expand=True)
        root = self.cat_tree.insert("", "end", "All Downloads", text="📁 All Downloads", open=True)
        for cat in list(config.CATEGORIES) + ["General"]:
            self.cat_tree.insert(root, "end", cat, text=f"{CATEGORY_ICONS[cat]} {cat}")
        for special in ("Unfinished", "Finished", "Queues"):
            self.cat_tree.insert("", "end", special, text=f"{CATEGORY_ICONS[special]} {special}")
        self.cat_tree.selection_set("All Downloads")
        self.cat_tree.bind("<<TreeviewSelect>>", self._on_category)
        pane.add(left, weight=0)

        right = ttk.Frame(pane)
        cols = ("name", "size", "status", "left", "rate", "date", "desc")
        heads = ("File Name", "Size", "Status", "Time left", "Transfer rate", "Last Try Date", "Description")
        widths = (330, 90, 110, 80, 100, 140, 200)
        self.tree = ttk.Treeview(right, columns=cols, show="headings", selectmode="extended")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h, command=lambda c=c: self._sort(c))
            self.tree.column(c, width=w, anchor="w" if c in ("name", "desc", "date") else "e")
        ysb = ttk.Scrollbar(right, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.tag_configure("complete", foreground="#1b7f3b")
        self.tree.tag_configure("error", foreground="#c62828")
        self.tree.tag_configure("active", foreground="#1565c0")
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Button-3>", self._on_right_click)
        pane.add(right, weight=1)
        self.sort_key, self.sort_rev = None, False

    def _build_statusbar(self):
        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status, anchor="w", relief="sunken", padding=(6, 2)).pack(fill="x")

    # ------------------------------------------------------------------ list
    def _visible(self, d):
        f = self.filter
        if f == "All Downloads":
            return True
        if f == "Unfinished":
            return d.status != "complete"
        if f == "Finished":
            return d.status == "complete"
        if f == "Queues":
            return d.in_queue
        return d.category == f

    def _row(self, d):
        st = STATUS_TEXT.get(d.status)
        if st is None:
            st = f"{d.percent():.1f}%" if d.size > 0 else fmt_size(d.progress_bytes())
        if d.status == "error":
            st = "Error"
        rate = fmt_size(d.speed()) + "/s" if d.status == "downloading" else ""
        when = datetime.datetime.fromtimestamp(d.finished or d.added).strftime("%b %d %Y %H:%M")
        desc = d.error if d.status == "error" else (d.description or d.media_title)
        return (d.filename, fmt_size(d.size), st, fmt_time(d.eta()), rate, when, desc)

    def refresh_list(self):
        ids = [i for i in self.engine.order if i in self.engine.downloads and self._visible(self.engine.downloads[i])]
        if self.sort_key:
            idx = ("name", "size", "status", "left", "rate", "date", "desc").index(self.sort_key)
            ids.sort(key=lambda i: self._sort_value(self.engine.downloads[i], idx), reverse=self.sort_rev)
        existing = set(self.tree.get_children())
        for i in existing - set(ids):
            self.tree.delete(i)
        for pos, i in enumerate(ids):
            d = self.engine.downloads[i]
            tag = "complete" if d.status == "complete" else "error" if d.status == "error" else \
                "active" if d.status in ("downloading", "connecting", "merging") else ""
            if i in existing:
                self.tree.item(i, values=self._row(d), tags=(tag,))
                if self.tree.index(i) != pos:
                    self.tree.move(i, "", pos)
            else:
                self.tree.insert("", pos, iid=i, values=self._row(d), tags=(tag,))
        running = self.engine.running()
        total = sum(d.speed() for d in running)
        q = "  •  Queue running" if self.engine.queue_running else ""
        lim = f"  •  Limit {self.settings['speed_limit_kbps']} KB/s" if self.settings["speed_limit_kbps"] else ""
        self.status.set(f"{len(running)} active  •  {fmt_size(total)}/s  •  "
                        f"{len(self.engine.downloads)} downloads{q}{lim}")

    @staticmethod
    def _sort_value(d, idx):
        return [d.filename.lower(), d.size, d.percent(), d.eta() or 0, d.speed(),
                d.finished or d.added, d.description][idx]

    def _sort(self, col):
        self.sort_rev = not self.sort_rev if self.sort_key == col else False
        self.sort_key = col
        self.refresh_list()

    def _on_category(self, _):
        sel = self.cat_tree.selection()
        self.filter = sel[0] if sel else "All Downloads"
        self.refresh_list()

    def selected(self):
        return [self.engine.downloads[i] for i in self.tree.selection() if i in self.engine.downloads]

    # ------------------------------------------------------------------ event pump
    def _poll(self):
        while True:
            try:
                ev, dl_id = self.engine.events.get_nowait()
            except queue.Empty:
                break
            if ev == "complete":
                self._on_complete(dl_id)
            elif ev == "queue_done":
                self._after_queue()
            elif ev == "changed":
                d = self.engine.downloads.get(dl_id)
                if d and d.status == "error" and dl_id in self.progress_windows:
                    self.progress_windows[dl_id].refresh()
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_api(kind, payload)
        self.refresh_list()
        for w in list(self.progress_windows.values()):
            w.refresh()
        self.after(500, self._poll)

    def _handle_api(self, kind, p):
        if kind == "show":
            self.deiconify()
            self.lift()
            return
        headers = self.engine.headers_for(referer=p.get("referer", ""), cookies=p.get("cookies", ""),
                                          user_agent=p.get("user_agent", ""))
        if kind == "batch":
            urls = [u for u in p.get("urls", []) if URL_RE.match(u)]
            if urls:
                BatchConfirmDialog(self, urls, headers)
            return
        kw = dict(url=p["url"], headers=headers, filename=p.get("filename", ""),
                  kind=p.get("kind", "http"), page_url=p.get("page_url", ""),
                  fmt=p.get("format", ""), size=p.get("size", -1), media_title=p.get("title", ""))
        if p.get("silent") or p.get("queue"):
            queued = bool(p.get("queue"))
            kw.update(directory=p.get("directory", ""), start=not queued, in_queue=queued)
            threading.Thread(target=lambda: self.engine.add(**kw), daemon=True).start()
        else:
            FileInfoDialog(self, **kw)

    def _on_complete(self, dl_id):
        d = self.engine.downloads.get(dl_id)
        if not d or d.status != "complete":
            return
        w = self.progress_windows.pop(dl_id, None)
        if w:
            w.destroy()
        if self.settings["show_complete_dialog"]:
            CompleteDialog(self, d)

    # ------------------------------------------------------------------ actions
    def add_url(self, url=""):
        if not url:
            try:
                clip = self.clipboard_get().strip()
                url = clip if URL_RE.match(clip) else ""
            except tk.TclError:
                pass
        dlg = tk.Toplevel(self)
        dlg.title("Enter new address to download")
        dlg.transient(self)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill="both")
        ttk.Label(frm, text="Address:").grid(row=0, column=0, sticky="w")
        var = tk.StringVar(value=url)
        ent = ttk.Entry(frm, textvariable=var, width=70)
        ent.grid(row=0, column=1, padx=6)
        ent.focus_set()
        ent.select_range(0, "end")
        auth = tk.BooleanVar()
        ttk.Checkbutton(frm, text="Use authorization", variable=auth).grid(row=1, column=1, sticky="w", pady=4)
        ua = ttk.Frame(frm)
        user, pwd = tk.StringVar(), tk.StringVar()
        ttk.Label(ua, text="Login").pack(side="left")
        ttk.Entry(ua, textvariable=user, width=20).pack(side="left", padx=4)
        ttk.Label(ua, text="Password").pack(side="left")
        ttk.Entry(ua, textvariable=pwd, width=20, show="•").pack(side="left", padx=4)
        auth.trace_add("write", lambda *a: ua.grid(row=2, column=1, sticky="w") if auth.get() else ua.grid_remove())

        def ok(*_):
            u = var.get().strip()
            if not URL_RE.match(u):
                messagebox.showerror(config.APP_NAME, "Please enter a valid http(s) or ftp address.", parent=dlg)
                return
            headers = self.engine.headers_for()
            if auth.get() and user.get():
                import base64
                headers["Authorization"] = "Basic " + base64.b64encode(f"{user.get()}:{pwd.get()}".encode()).decode()
            dlg.destroy()
            FileInfoDialog(self, url=u, headers=headers)

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=1, sticky="e", pady=(8, 0))
        ttk.Button(btns, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="left")
        dlg.bind("<Return>", ok)
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def resume_selected(self):
        for d in self.selected():
            if d.status == "error" and d.kind == "http" and not d.resumable:
                self.engine.redownload(d.id)
            else:
                self.engine.start(d.id)
            if self.settings["show_progress_dialog"]:
                self.show_progress(d.id)

    def stop_selected(self):
        for d in self.selected():
            self.engine.stop(d.id)

    def stop_all(self):
        self.engine.stop_queue()
        for d in self.engine.running():
            self.engine.stop(d.id)

    def resume_all(self):
        for d in self.engine.downloads.values():
            if d.status in ("stopped", "error"):
                self.engine.start(d.id)

    def delete_selected(self):
        sel = self.selected()
        if not sel:
            return
        dlg = tk.Toplevel(self)
        dlg.title("Confirm deletion")
        dlg.transient(self)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=14)
        frm.pack()
        ttk.Label(frm, text=f"Delete {len(sel)} download(s) from the list?").pack(anchor="w")
        also = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Also delete the downloaded file(s) from disk", variable=also).pack(anchor="w", pady=6)

        def go():
            dlg.destroy()
            for d in sel:
                w = self.progress_windows.pop(d.id, None)
                if w:
                    w.destroy()
                threading.Thread(target=self.engine.remove, args=(d.id, also.get()), daemon=True).start()

        b = ttk.Frame(frm)
        b.pack(anchor="e")
        ttk.Button(b, text="OK", command=go).pack(side="left", padx=4)
        ttk.Button(b, text="Cancel", command=dlg.destroy).pack(side="left")

    def delete_completed(self):
        done = [d for d in self.engine.downloads.values() if d.status == "complete"]
        if done and messagebox.askyesno(config.APP_NAME, f"Remove {len(done)} completed download(s) from the list?\n"
                                                          "Files on disk are kept."):
            for d in done:
                self.engine.remove(d.id)

    def show_progress(self, dl_id):
        w = self.progress_windows.get(dl_id)
        if w and w.winfo_exists():
            w.deiconify()
            w.lift()
        else:
            self.progress_windows[dl_id] = ProgressDialog(self, self.engine.downloads[dl_id])

    def toggle_limiter(self):
        if self.limit_var.get():
            kbps = self.settings.data.get("last_limit_kbps", 512)
            self.engine.set_speed_limit(kbps)
        else:
            self.settings["last_limit_kbps"] = self.settings["speed_limit_kbps"] or 512
            self.engine.set_speed_limit(0)
        self.settings.save()

    def limiter_settings(self):
        from tkinter import simpledialog
        v = simpledialog.askinteger(config.APP_NAME, "Maximum total download speed (KB/s, 0 = unlimited):",
                                    initialvalue=self.settings["speed_limit_kbps"] or 512, minvalue=0, parent=self)
        if v is not None:
            self.engine.set_speed_limit(v)
            self.limit_var.set(v > 0)
            self.settings.save()

    def _on_double(self, event):
        iid = self.tree.identify_row(event.y)
        d = self.engine.downloads.get(iid)
        if not d:
            return
        if d.status == "complete":
            open_path(d.path)
        else:
            self.show_progress(d.id)

    def _on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        d = self.engine.downloads[iid]
        m = tk.Menu(self, tearoff=0)
        if d.status == "complete":
            m.add_command(label="Open", command=lambda: open_path(d.path))
            m.add_command(label="Open with…", command=lambda: subprocess.Popen(
                ["rundll32", "shell32.dll,OpenAs_RunDLL", str(d.path)]))
        m.add_command(label="Open folder", command=lambda: reveal(d.path))
        m.add_separator()
        m.add_command(label="Resume", command=self.resume_selected)
        m.add_command(label="Stop", command=self.stop_selected)
        m.add_command(label="Redownload", command=lambda: self.engine.redownload(d.id))
        m.add_command(label="Show progress", command=lambda: self.show_progress(d.id))
        m.add_separator()
        m.add_command(label="Remove from queue" if d.in_queue else "Add to queue",
                      command=lambda: setattr(d, "in_queue", not d.in_queue) or self.engine.save())
        m.add_command(label="Copy address", command=lambda: (self.clipboard_clear(), self.clipboard_append(d.url)))
        m.add_command(label="Move / Rename…", command=lambda: self.move_rename(d))
        m.add_separator()
        m.add_command(label="Delete", command=self.delete_selected)
        m.add_command(label="Properties", command=lambda: PropertiesDialog(self, d))
        m.tk_popup(event.x_root, event.y_root)

    def move_rename(self, d):
        if d.status != "complete":
            messagebox.showinfo(config.APP_NAME, "Stop or finish the download first.")
            return
        new = filedialog.asksaveasfilename(initialdir=d.directory, initialfile=d.filename, parent=self)
        if new:
            os.replace(d.path, new)
            d.directory, d.filename = str(Path(new).parent), Path(new).name
            self.engine.save()

    # ------------------------------------------------------------------ batch / import
    def batch_dialog(self):
        BatchPatternDialog(self)

    def batch_from_clipboard(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        urls = [u for u in re.findall(r"(?:https?|ftp)://\S+", text)]
        if urls:
            BatchConfirmDialog(self, urls, self.engine.headers_for())
        else:
            messagebox.showinfo(config.APP_NAME, "No links found in the clipboard.")

    def import_list(self):
        p = filedialog.askopenfilename(filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if p:
            urls = re.findall(r"(?:https?|ftp)://\S+", Path(p).read_text("utf-8", "replace"))
            if urls:
                BatchConfirmDialog(self, urls, self.engine.headers_for())

    def export_list(self):
        p = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text files", "*.txt")])
        if p:
            Path(p).write_text("\n".join(d.url for d in self.engine.downloads.values()), "utf-8")

    # ------------------------------------------------------------------ clipboard monitor
    def _clipboard_poll(self):
        if self.settings["clipboard_monitor"]:
            try:
                clip = self.clipboard_get().strip()
            except tk.TclError:
                clip = ""
            if clip != self.last_clip:
                self.last_clip = clip
                if URL_RE.match(clip) and "\n" not in clip:
                    ext = Path(clip.split("?")[0]).suffix.lower().lstrip(".")
                    if ext in self.settings["intercept_extensions"]:
                        FileInfoDialog(self, url=clip, headers=self.engine.headers_for())
        self.after(1500, self._clipboard_poll)

    # ------------------------------------------------------------------ scheduler
    def _scheduler_poll(self):
        s = self.settings.data.get("scheduler") or {}
        if s.get("enabled"):
            now = datetime.datetime.now()
            today = now.strftime("%Y-%m-%d")
            day_ok = not s.get("days") or now.weekday() in s["days"]
            hm = now.strftime("%H:%M")
            if day_ok and s.get("start") == hm and ("start", today) not in self.scheduler_fired:
                self.scheduler_fired.add(("start", today))
                self.engine.start_queue()
            if day_ok and s.get("stop_enabled") and s.get("stop") == hm and ("stop", today) not in self.scheduler_fired:
                self.scheduler_fired.add(("stop", today))
                self.engine.stop_queue()
        self.after(5000, self._scheduler_poll)

    def _after_queue(self):
        action = (self.settings.data.get("scheduler") or {}).get("on_done", "none")
        if action == "exit":
            self.on_close(force=True)
        elif action in ("shutdown", "hibernate", "sleep"):
            ShutdownCountdown(self, action)

    def scheduler_dialog(self):
        SchedulerDialog(self)

    def options_dialog(self):
        OptionsDialog(self)

    def on_close(self, force=False):
        if not force and self.engine.running() and not messagebox.askyesno(
                config.APP_NAME, "Downloads are in progress. Stop them and exit?\n(They can be resumed later.)"):
            return
        self.api.stop()
        self.engine.shutdown()
        self.settings.save()
        self.destroy()


# ====================================================================== dialogs
class FileInfoDialog(tk.Toplevel):
    """IDM's "Download File Info": where to save, category, start now or later."""

    def __init__(self, app: App, url, headers=None, filename="", kind="http", page_url="", fmt="",
                 size=-1, media_title=""):
        super().__init__(app)
        self.app, self.url, self.headers = app, url, headers or app.engine.headers_for()
        self.kind, self.page_url, self.fmt, self.size, self.media_title = kind, page_url, fmt, size, media_title
        self.info = None
        self.title("Download File Info")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both")
        r = 0
        ttk.Label(frm, text="URL").grid(row=r, column=0, sticky="w")
        self.url_var = tk.StringVar(value=url)
        ttk.Entry(frm, textvariable=self.url_var, width=72, state="readonly").grid(row=r, column=1, columnspan=2, sticky="we", pady=2)
        r += 1
        ttk.Label(frm, text="Category").grid(row=r, column=0, sticky="w")
        self.cat_var = tk.StringVar(value="General")
        cb = ttk.Combobox(frm, textvariable=self.cat_var, values=list(config.CATEGORIES) + ["General"], state="readonly", width=20)
        cb.grid(row=r, column=1, sticky="w", pady=2)
        cb.bind("<<ComboboxSelected>>", lambda e: self._update_dir())
        r += 1
        ttk.Label(frm, text="Save As").grid(row=r, column=0, sticky="w")
        self.path_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.path_var, width=64).grid(row=r, column=1, sticky="we", pady=2)
        ttk.Button(frm, text="…", width=3, command=self._browse).grid(row=r, column=2, padx=(4, 0))
        r += 1
        self.remember = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Remember this path for this category", variable=self.remember).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="Description").grid(row=r, column=0, sticky="w")
        self.desc_var = tk.StringVar(value=media_title)
        ttk.Entry(frm, textvariable=self.desc_var, width=64).grid(row=r, column=1, sticky="we", pady=2)
        r += 1
        self.quality_row = r
        self.q_label = ttk.Label(frm, text="Quality")
        self.q_var = tk.StringVar()
        self.q_box = ttk.Combobox(frm, textvariable=self.q_var, state="readonly", width=40)
        self.formats = []
        r += 1
        self.size_var = tk.StringVar(value="Size: probing…")
        ttk.Label(frm, textvariable=self.size_var, foreground="#555").grid(row=r, column=1, sticky="w", pady=(6, 0))
        r += 1
        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=3, sticky="e", pady=(12, 0))
        self.start_btn = ttk.Button(btns, text="Start Download", command=lambda: self._go(True))
        self.start_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="Download Later", command=lambda: self._go(False)).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=4)
        self.bind("<Escape>", lambda e: self.destroy())

        guess = filename or Path(url.split("?")[0]).name or "download"
        self._set_name(guess)
        self.focus_force()
        if kind == "ytdlp":
            self.size_var.set("Fetching available qualities…")
            threading.Thread(target=self._fetch_formats, daemon=True).start()
        elif kind == "http":
            threading.Thread(target=self._probe, daemon=True).start()
        else:
            self.size_var.set("Stream (size known after download starts)")
            self.info = {}

    def _set_name(self, name):
        name = Path(name).name or "download"
        self.cat_var.set(config.category_for(name))
        self.path_var.set(str(self._cat_dir() / name))

    def _cat_dir(self):
        remembered = self.app.settings.data.get("category_dirs", {}).get(self.cat_var.get())
        return Path(remembered) if remembered else self.app.settings.target_dir(self.cat_var.get())

    def _update_dir(self):
        self.path_var.set(str(self._cat_dir() / Path(self.path_var.get()).name))

    def _browse(self):
        p = Path(self.path_var.get())
        new = filedialog.asksaveasfilename(parent=self, initialdir=p.parent, initialfile=p.name)
        if new:
            self.path_var.set(new)

    def _probe(self):
        try:
            info = self.app.engine.probe(self.url, self.headers)
        except Exception as e:
            info = {"size": -1, "resumable": False, "filename": "", "error": str(e)}
        self.after(0, self._probed, info)

    def _probed(self, info):
        if not self.winfo_exists():
            return
        self.info = info
        if info.get("kind") == "hls":
            self.kind = "hls"
        if info.get("filename"):
            name = info["filename"]
            if self.kind == "hls" and name.lower().endswith(".m3u8"):
                name = name[:-5] + ".mp4"
            self._set_name(name)
        if info.get("error"):
            self.size_var.set(f"Could not query the server: {info['error']}")
        else:
            res = "Yes" if info.get("resumable") else "No"
            self.size_var.set(f"Size: {fmt_size(info.get('size', -1))}    Resume capability: {res}")

    def _fetch_formats(self):
        try:
            res = ytdlp.formats(self.app.engine, self.url, self.headers)
        except Exception as e:
            res = {"error": str(e)}
        self.after(0, self._got_formats, res)

    def _got_formats(self, res):
        if not self.winfo_exists():
            return
        if res.get("error"):
            self.size_var.set("Could not list qualities: " + res["error"][:120])
            self.info = {}
            return
        self.formats = res["formats"]
        self.q_label.grid(row=self.quality_row, column=0, sticky="w")
        self.q_box.grid(row=self.quality_row, column=1, sticky="w", pady=2)
        prefix = {"audio": "Audio · ", "all": "Format "}
        self.q_box["values"] = [
            f"{prefix.get(f.get('group'), '')}{f['label']}  —  {f.get('detail', '')}  ({fmt_size(f['size']) if f['size'] else '?'})"
            for f in self.formats]
        self.q_box["width"] = 64
        self.q_box["height"] = 25
        idx = 0
        if self.fmt:
            idx = next((i for i, f in enumerate(self.formats) if f["format"] == self.fmt), 0)
        if self.formats:
            self.q_box.current(idx)
        self.q_box.bind("<<ComboboxSelected>>", lambda e: self._fix_ext())
        title = re.sub(r'[<>:"/\\|?*]', "_", res["title"])[:150]
        self.desc_var.set(res["title"])
        self._set_name(title + ".mp4")
        self._fix_ext()
        self.size_var.set(f"{len(self.formats)} qualities available")
        self.info = {}

    def _fix_ext(self):
        if self.formats:
            f = self.formats[self.q_box.current()]
            p = Path(self.path_var.get())
            self.path_var.set(str(p.with_suffix("." + f["ext"])))

    def _go(self, start_now):
        p = Path(self.path_var.get())
        fmt = self.fmt
        if self.formats:
            fmt = self.formats[self.q_box.current()]["format"]
        if self.remember.get():
            self.app.settings.data.setdefault("category_dirs", {})[self.cat_var.get()] = str(p.parent)
            self.app.settings.save()
        info = self.info if self.kind == "http" and self.info and not self.info.get("error") else None
        eng = self.app.engine
        kw = dict(url=self.url, filename=p.name, directory=str(p.parent), headers=self.headers,
                  start=start_now, kind=self.kind, description=self.desc_var.get(), page_url=self.page_url,
                  fmt=fmt, in_queue=not start_now, probe_info=info, size=self.size, media_title=self.media_title)
        self.destroy()
        app = self.app

        def worker():
            d = eng.add(**kw)
            if start_now and app.settings["show_progress_dialog"]:
                app.after(0, app.show_progress, d.id)
        threading.Thread(target=worker, daemon=True).start()


class ProgressDialog(tk.Toplevel):
    """Per-download window with a connection map like IDM's."""
    COLORS = ["#1e88e5", "#43a047", "#fb8c00", "#8e24aa", "#00acc1", "#e53935", "#6d4c41", "#3949ab"]

    def __init__(self, app: App, d: engine_mod.Download):
        super().__init__(app)
        self.app, self.d = app, d
        self.title(f"{d.percent():.0f}% {d.filename}")
        self.geometry("560x400")
        self.protocol("WM_DELETE_WINDOW", self._close)
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="Download status")
        lim_tab = ttk.Frame(nb, padding=10)
        nb.add(lim_tab, text="Speed Limiter")

        ttk.Label(tab, text=d.url, foreground="#1565c0", wraplength=520).pack(anchor="w")
        grid = ttk.Frame(tab)
        grid.pack(fill="x", pady=8)
        self.vars = {}
        for i, key in enumerate(("Status", "File size", "Downloaded", "Transfer rate", "Time left",
                                 "Resume capability", "Connections")):
            ttk.Label(grid, text=key).grid(row=i, column=0, sticky="w")
            v = tk.StringVar()
            ttk.Label(grid, textvariable=v).grid(row=i, column=1, sticky="w", padx=12)
            self.vars[key] = v
        self.bar = ttk.Progressbar(tab, maximum=1000)
        self.bar.pack(fill="x", pady=(4, 6))
        ttk.Label(tab, text="Connection map").pack(anchor="w")
        self.canvas = tk.Canvas(tab, height=26, background="#e8e8e8", highlightthickness=1,
                                highlightbackground="#bbb")
        self.canvas.pack(fill="x")
        btns = ttk.Frame(tab)
        btns.pack(fill="x", pady=(10, 0))
        self.pause_btn = ttk.Button(btns, text="Pause", command=self._toggle)
        self.pause_btn.pack(side="right", padx=4)
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(btns, text="Hide", command=self._close).pack(side="left")

        self.lim_on = tk.BooleanVar(value=d.speed_limiter.rate > 0)
        self.lim_val = tk.IntVar(value=(d.speed_limiter.rate // 1024) or 256)
        ttk.Checkbutton(lim_tab, text="Use speed limiter for this download", variable=self.lim_on,
                        command=self._apply_limit).pack(anchor="w")
        row = ttk.Frame(lim_tab)
        row.pack(anchor="w", pady=6)
        ttk.Label(row, text="Maximum speed").pack(side="left")
        ttk.Spinbox(row, from_=8, to=1_000_000, increment=64, textvariable=self.lim_val, width=10,
                    command=self._apply_limit).pack(side="left", padx=6)
        ttk.Label(row, text="KB/s").pack(side="left")
        self.refresh()

    def _apply_limit(self):
        try:
            v = int(self.lim_val.get())
        except (tk.TclError, ValueError):
            return
        self.d.speed_limiter.set_rate(v * 1024 if self.lim_on.get() else 0)

    def _toggle(self):
        if self.d.status in ("downloading", "connecting", "merging"):
            self.app.engine.stop(self.d.id)
        else:
            self.app.engine.start(self.d.id)

    def _cancel(self):
        self.app.engine.stop(self.d.id)
        self._close()

    def _close(self):
        self.app.progress_windows.pop(self.d.id, None)
        self.destroy()

    def refresh(self):
        if not self.winfo_exists():
            return
        d = self.d
        pct = d.percent()
        self.title(f"{pct:.0f}% {d.filename}")
        st = {"downloading": "Receiving data…", "connecting": "Connecting…", "merging": "Merging / remuxing…",
              "stopped": "Paused", "queued": "Queued", "complete": "Complete",
              "error": f"Error: {d.error}"}.get(d.status, d.status)
        self.vars["Status"].set(st)
        self.vars["File size"].set(fmt_size(d.size))
        self.vars["Downloaded"].set(f"{fmt_size(d.progress_bytes())} ({pct:.2f}%)")
        self.vars["Transfer rate"].set(fmt_size(d.speed()) + "/s")
        self.vars["Time left"].set(fmt_time(d.eta()) or "—")
        self.vars["Resume capability"].set({"http": "Yes" if d.resumable else "No",
                                            "hls": "Yes (per segment)", "ytdlp": "Yes"}[d.kind])
        self.vars["Connections"].set(str(d.active_connections))
        self.bar["value"] = pct * 10
        self.pause_btn["text"] = "Pause" if d.status in ("downloading", "connecting", "merging") else "Resume"
        self._draw_map()

    def _draw_map(self):
        c, d = self.canvas, self.d
        c.delete("all")
        w = max(c.winfo_width(), 400)
        h = 26
        if d.kind == "http" and d.segments and d.size > 0:
            for i, s in enumerate(sorted(d.segments, key=lambda s: s.start)):
                x0 = w * s.start / d.size
                x1 = w * (s.pos) / d.size
                xe = w * ((s.end if s.end is not None else s.start) + 1) / d.size
                c.create_rectangle(x0, 0, xe, h, fill="#ffffff", outline="")
                c.create_rectangle(x0, 0, x1, h, fill=self.COLORS[i % len(self.COLORS)], outline="")
                c.create_line(x0, 0, x0, h, fill="#555")
        else:
            c.create_rectangle(0, 0, w * d.percent() / 100, h, fill=self.COLORS[0], outline="")


class CompleteDialog(tk.Toplevel):
    def __init__(self, app, d):
        super().__init__(app)
        self.title("Download complete")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        frm = ttk.Frame(self, padding=14)
        frm.pack()
        ttk.Label(frm, text="Download complete", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(frm, text=f"Downloaded {fmt_size(d.size)}").pack(anchor="w", pady=(4, 0))
        ttk.Label(frm, text="Address:").pack(anchor="w", pady=(8, 0))
        ttk.Label(frm, text=d.url[:100], foreground="#1565c0").pack(anchor="w")
        ttk.Label(frm, text="The file saved as:").pack(anchor="w", pady=(6, 0))
        ttk.Label(frm, text=str(d.path)).pack(anchor="w")
        b = ttk.Frame(frm)
        b.pack(fill="x", pady=(12, 0))
        ttk.Button(b, text="Open", command=lambda: (open_path(d.path), self.destroy())).pack(side="left", padx=2)
        ttk.Button(b, text="Open with…", command=lambda: (subprocess.Popen(
            ["rundll32", "shell32.dll,OpenAs_RunDLL", str(d.path)]), self.destroy())).pack(side="left", padx=2)
        ttk.Button(b, text="Open folder", command=lambda: (reveal(d.path), self.destroy())).pack(side="left", padx=2)
        ttk.Button(b, text="Close", command=self.destroy).pack(side="right", padx=2)
        never = tk.BooleanVar()

        def toggle():
            app.settings["show_complete_dialog"] = not never.get()
            app.settings.save()
        ttk.Checkbutton(frm, text="Don't show this dialog again", variable=never, command=toggle).pack(anchor="w", pady=(8, 0))
        self.bell()


class PropertiesDialog(tk.Toplevel):
    def __init__(self, app, d):
        super().__init__(app)
        self.title("Properties")
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        rows = [("File name", d.filename), ("Saved to", d.directory), ("Address", d.url),
                ("Page", d.page_url), ("Type", d.kind), ("Size", fmt_size(d.size)), ("Status", d.status),
                ("Error", d.error), ("Referer", d.headers.get("Referer", "")), ("Description", d.description)]
        for i, (k, v) in enumerate(rows):
            ttk.Label(frm, text=k).grid(row=i, column=0, sticky="nw", pady=2)
            e = ttk.Entry(frm, width=80)
            e.insert(0, v or "")
            e.configure(state="readonly")
            e.grid(row=i, column=1, sticky="we", padx=8, pady=2)
        ttk.Button(frm, text="Close", command=self.destroy).grid(row=len(rows), column=1, sticky="e", pady=8)


class BatchPatternDialog(tk.Toplevel):
    """IDM's batch download: http://site/img[001-120].jpg or [a-z]."""

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.title("Add batch download")
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both")
        ttk.Label(frm, text="Address with wildcard, e.g. https://site.com/pics/img*.jpg").grid(row=0, column=0, columnspan=4, sticky="w")
        self.url = tk.StringVar()
        ttk.Entry(frm, textvariable=self.url, width=70).grid(row=1, column=0, columnspan=4, sticky="we", pady=4)
        self.mode = tk.StringVar(value="num")
        ttk.Radiobutton(frm, text="From", variable=self.mode, value="num").grid(row=2, column=0, sticky="w")
        self.a, self.b, self.width = tk.IntVar(value=1), tk.IntVar(value=10), tk.IntVar(value=0)
        ttk.Spinbox(frm, from_=0, to=10**9, textvariable=self.a, width=8).grid(row=2, column=1, sticky="w")
        ttk.Label(frm, text="to").grid(row=2, column=2)
        ttk.Spinbox(frm, from_=0, to=10**9, textvariable=self.b, width=8).grid(row=2, column=3, sticky="w")
        ttk.Label(frm, text="Wildcard size (zero padded digits, 0 = none)").grid(row=3, column=0, columnspan=3, sticky="w")
        ttk.Spinbox(frm, from_=0, to=10, textvariable=self.width, width=4).grid(row=3, column=3, sticky="w")
        ttk.Radiobutton(frm, text="Letters", variable=self.mode, value="abc").grid(row=4, column=0, sticky="w")
        self.la, self.lb = tk.StringVar(value="a"), tk.StringVar(value="z")
        ttk.Entry(frm, textvariable=self.la, width=4).grid(row=4, column=1, sticky="w")
        ttk.Label(frm, text="to").grid(row=4, column=2)
        ttk.Entry(frm, textvariable=self.lb, width=4).grid(row=4, column=3, sticky="w")
        self.preview = tk.StringVar()
        ttk.Label(frm, textvariable=self.preview, foreground="#555").grid(row=5, column=0, columnspan=4, sticky="w", pady=6)
        b = ttk.Frame(frm)
        b.grid(row=6, column=0, columnspan=4, sticky="e")
        ttk.Button(b, text="OK", command=self._ok).pack(side="left", padx=4)
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="left")
        for v in (self.url, self.a, self.b, self.width, self.mode, self.la, self.lb):
            v.trace_add("write", lambda *_: self._update())

    def expand(self):
        u = self.url.get().strip()
        if "*" not in u:
            return []
        try:
            if self.mode.get() == "num":
                w = self.width.get()
                return [u.replace("*", str(i).zfill(w)) for i in range(self.a.get(), self.b.get() + 1)]
            return [u.replace("*", chr(c)) for c in range(ord(self.la.get()[:1]), ord(self.lb.get()[:1]) + 1)]
        except (tk.TclError, ValueError, TypeError):
            return []

    def _update(self):
        urls = self.expand()
        self.preview.set(f"{len(urls)} files: {urls[0]} … {urls[-1]}" if urls else "Put * where the number goes")

    def _ok(self):
        urls = self.expand()
        if urls:
            self.destroy()
            BatchConfirmDialog(self.app, urls, self.app.engine.headers_for())


class BatchConfirmDialog(tk.Toplevel):
    """Pick which of many links to download (batch, clipboard, "download all links" from the browser)."""

    def __init__(self, app: App, urls, headers):
        super().__init__(app)
        self.app, self.headers = app, headers
        self.title(f"Select files to download ({len(urls)})")
        self.geometry("760x460")
        self.attributes("-topmost", True)
        top = ttk.Frame(self, padding=(8, 8, 8, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Filter by extension:").pack(side="left")
        self.ext = tk.StringVar()
        exts = sorted({Path(u.split("?")[0]).suffix.lower() for u in urls} - {""})
        cb = ttk.Combobox(top, textvariable=self.ext, values=["(all)"] + exts, width=12)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._filter())
        self.lb = tk.Listbox(self, selectmode="extended")
        self.lb.pack(fill="both", expand=True, padx=8, pady=8)
        self.urls = list(dict.fromkeys(urls))
        self._filter()
        b = ttk.Frame(self, padding=(8, 0, 8, 8))
        b.pack(fill="x")
        self.start = tk.BooleanVar(value=False)
        ttk.Checkbutton(b, text="Start queue now", variable=self.start).pack(side="left")
        ttk.Button(b, text="Select all", command=lambda: self.lb.select_set(0, "end")).pack(side="left", padx=8)
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(b, text="Add to queue", command=self._ok).pack(side="right", padx=4)

    def _filter(self):
        e = self.ext.get()
        self.lb.delete(0, "end")
        for u in self.urls:
            if not e or e == "(all)" or u.split("?")[0].lower().endswith(e):
                self.lb.insert("end", u)
        self.lb.select_set(0, "end")

    def _ok(self):
        chosen = [self.lb.get(i) for i in self.lb.curselection()]
        self.destroy()
        eng, start = self.app.engine, self.start.get()

        def worker():
            for u in chosen:
                eng.add(u, headers=dict(self.headers), start=False, in_queue=True)
            if start:
                eng.start_queue()
        threading.Thread(target=worker, daemon=True).start()


class OptionsDialog(tk.Toplevel):
    def __init__(self, app: App):
        super().__init__(app)
        self.app, s = app, app.settings
        self.title("Options")
        self.resizable(False, False)
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.v = {}

        def check(parent, key, text):
            self.v[key] = tk.BooleanVar(value=bool(s[key]))
            ttk.Checkbutton(parent, text=text, variable=self.v[key]).pack(anchor="w", pady=2)

        def number(parent, key, text, lo, hi):
            row = ttk.Frame(parent)
            row.pack(anchor="w", pady=3)
            ttk.Label(row, text=text, width=42).pack(side="left")
            self.v[key] = tk.IntVar(value=int(s[key]))
            ttk.Spinbox(row, from_=lo, to=hi, textvariable=self.v[key], width=8).pack(side="left")

        g = ttk.Frame(nb, padding=12)
        nb.add(g, text="General")
        check(g, "browser_integration", "Use advanced browser integration (take over browser downloads)")
        check(g, "clipboard_monitor", "Monitor the clipboard for downloadable links")
        check(g, "show_progress_dialog", "Show the download progress dialog")
        check(g, "show_complete_dialog", "Show the download complete dialog")
        self.startup = tk.BooleanVar(value=_autostart_enabled())
        ttk.Checkbutton(g, text="Launch at Windows startup", variable=self.startup).pack(anchor="w", pady=2)

        ft = ttk.Frame(nb, padding=12)
        nb.add(ft, text="File types")
        ttk.Label(ft, text="Automatically start downloading the following file types:").pack(anchor="w")
        self.types = tk.Text(ft, width=60, height=6, wrap="word")
        self.types.insert("1.0", " ".join(s["intercept_extensions"]).upper())
        self.types.pack(fill="x", pady=4)
        number(ft, "min_intercept_size_kb", "Don't take over files smaller than (KB)", 0, 10**7)

        c = ttk.Frame(nb, padding=12)
        nb.add(c, text="Connection")
        number(c, "connections", "Default max. connections per download", 1, 32)
        number(c, "max_concurrent", "Downloads running at once in the queue", 1, 20)
        number(c, "min_split_size_kb", "Minimum segment size (KB)", 64, 102400)
        number(c, "retries", "Number of retries", 0, 1000)
        number(c, "timeout", "Connection timeout (seconds)", 5, 600)
        row = ttk.Frame(c)
        row.pack(anchor="w", pady=3)
        ttk.Label(row, text="Proxy (http://host:port, empty = none)", width=42).pack(side="left")
        self.proxy = tk.StringVar(value=s["proxy"])
        ttk.Entry(row, textvariable=self.proxy, width=30).pack(side="left")

        sv = ttk.Frame(nb, padding=12)
        nb.add(sv, text="Save to")
        ttk.Label(sv, text="Default download directory").pack(anchor="w")
        row = ttk.Frame(sv)
        row.pack(fill="x", pady=4)
        self.dir = tk.StringVar(value=s["download_dir"])
        ttk.Entry(row, textvariable=self.dir, width=56).pack(side="left")
        ttk.Button(row, text="Browse…", command=lambda: self.dir.set(
            filedialog.askdirectory(initialdir=self.dir.get(), parent=self) or self.dir.get())).pack(side="left", padx=4)
        check(sv, "use_category_folders", "Put files in per-category sub folders (Video, Music, …)")
        row = ttk.Frame(sv)
        row.pack(anchor="w", pady=6)
        ttk.Label(row, text="If the file already exists").pack(side="left")
        self.dup = tk.StringVar(value=s["duplicate_action"])
        ttk.Combobox(row, textvariable=self.dup, values=["rename", "overwrite"], state="readonly", width=12).pack(side="left", padx=6)

        tools = ttk.Frame(nb, padding=12)
        nb.add(tools, text="Video tools")
        ttk.Label(tools, text="yt-dlp is used for YouTube and other sites; ffmpeg joins HLS streams and merges audio+video.",
                  wraplength=460).pack(anchor="w")
        self.ff, self.yt = tk.StringVar(value=s["ffmpeg_path"]), tk.StringVar(value=s["ytdlp_path"])
        for label, var in (("ffmpeg", self.ff), ("yt-dlp", self.yt)):
            row = ttk.Frame(tools)
            row.pack(anchor="w", pady=4)
            ttk.Label(row, text=label, width=10).pack(side="left")
            ttk.Entry(row, textvariable=var, width=50).pack(side="left")
        ttk.Label(tools, text=f"yt-dlp found: {'yes' if ytdlp.available(app.engine) else 'no — pip install yt-dlp'}").pack(anchor="w", pady=6)

        b = ttk.Frame(self, padding=(8, 0, 8, 8))
        b.pack(fill="x")
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(b, text="OK", command=self._ok).pack(side="right", padx=4)

    def _ok(self):
        s = self.app.settings
        try:
            for k, var in self.v.items():
                s[k] = var.get()
        except tk.TclError:
            messagebox.showerror(config.APP_NAME, "Please enter valid numbers.", parent=self)
            return
        s["intercept_extensions"] = sorted({t.lower().lstrip(".") for t in self.types.get("1.0", "end").split() if t})
        s["proxy"] = self.proxy.get().strip()
        s["download_dir"] = self.dir.get()
        s["duplicate_action"] = self.dup.get()
        s["ffmpeg_path"], s["ytdlp_path"] = self.ff.get().strip() or "ffmpeg", self.yt.get().strip() or "yt-dlp"
        _set_autostart(self.startup.get())
        s.save()
        self.destroy()


class SchedulerDialog(tk.Toplevel):
    DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        cur = app.settings.data.get("scheduler") or {}
        self.title("Scheduler — Main queue")
        self.resizable(False, False)
        frm = ttk.Frame(self, padding=12)
        frm.pack()
        self.enabled = tk.BooleanVar(value=cur.get("enabled", False))
        ttk.Checkbutton(frm, text="Start the queue at", variable=self.enabled).grid(row=0, column=0, sticky="w")
        self.start = tk.StringVar(value=cur.get("start", "02:00"))
        ttk.Entry(frm, textvariable=self.start, width=7).grid(row=0, column=1, sticky="w")
        ttk.Label(frm, text="(HH:MM, 24h)").grid(row=0, column=2, sticky="w")
        daysf = ttk.Frame(frm)
        daysf.grid(row=1, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(daysf, text="On days (none = every day):").pack(side="left")
        self.days = []
        for i, n in enumerate(self.DAYS):
            v = tk.BooleanVar(value=i in cur.get("days", []))
            ttk.Checkbutton(daysf, text=n, variable=v).pack(side="left")
            self.days.append(v)
        self.stop_enabled = tk.BooleanVar(value=cur.get("stop_enabled", False))
        ttk.Checkbutton(frm, text="Stop the queue at", variable=self.stop_enabled).grid(row=2, column=0, sticky="w")
        self.stop = tk.StringVar(value=cur.get("stop", "07:00"))
        ttk.Entry(frm, textvariable=self.stop, width=7).grid(row=2, column=1, sticky="w")
        ttk.Label(frm, text="When the queue finishes:").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.on_done = tk.StringVar(value=cur.get("on_done", "none"))
        ttk.Combobox(frm, textvariable=self.on_done, state="readonly", width=18,
                     values=["none", "exit", "sleep", "hibernate", "shutdown"]).grid(row=3, column=1, columnspan=2, sticky="w", pady=(8, 0))
        b = ttk.Frame(frm)
        b.grid(row=4, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(b, text="Start now", command=lambda: (self._save(), app.engine.start_queue())).pack(side="left", padx=4)
        ttk.Button(b, text="Apply", command=lambda: (self._save(), self.destroy())).pack(side="left", padx=4)
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="left")

    def _save(self):
        for t in (self.start.get(), self.stop.get()):
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                messagebox.showerror(config.APP_NAME, f"Invalid time: {t}", parent=self)
                raise ValueError(t)
        self.app.settings["scheduler"] = {
            "enabled": self.enabled.get(), "start": self.start.get(), "stop_enabled": self.stop_enabled.get(),
            "stop": self.stop.get(), "on_done": self.on_done.get(),
            "days": [i for i, v in enumerate(self.days) if v.get()]}
        self.app.settings.save()


class ShutdownCountdown(tk.Toplevel):
    def __init__(self, app, action):
        super().__init__(app)
        self.action, self.left = action, 60
        self.title(config.APP_NAME)
        self.attributes("-topmost", True)
        self.var = tk.StringVar()
        ttk.Label(self, textvariable=self.var, padding=16).pack()
        ttk.Button(self, text="Cancel", command=self.destroy).pack(pady=(0, 12))
        self._tick()

    def _tick(self):
        if not self.winfo_exists():
            return
        if self.left <= 0:
            cmd = {"shutdown": ["shutdown", "/s", "/t", "0"], "hibernate": ["shutdown", "/h"],
                   "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]}[self.action]
            self.destroy()
            subprocess.Popen(cmd)
            return
        self.var.set(f"All downloads finished. The computer will {self.action} in {self.left} seconds.")
        self.left -= 1
        self.after(1000, self._tick)


# ------------------------------------------------------------------ autostart (HKCU Run key)
def _autostart_cmd():
    import sys
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
    main = Path(__file__).resolve().parents[1] / "ydm_app.py"
    return f'"{pyw}" "{main}" --minimized'


def _autostart_enabled():
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.QueryValueEx(k, config.APP_NAME)
            return True
    except OSError:
        return False


def _set_autostart(on):
    if os.name != "nt" or on == _autostart_enabled():
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                        winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, config.APP_NAME, 0, winreg.REG_SZ, _autostart_cmd())
        else:
            winreg.DeleteValue(k, config.APP_NAME)


def run(start_minimized=False, initial=None):
    eng = engine_mod.Engine()
    app = App(eng, start_minimized)
    if initial:
        app.ui_queue.put(("add", initial))
    app.mainloop()
