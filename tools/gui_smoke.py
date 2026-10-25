import os, sys, tempfile, threading, time
os.environ["YDM_HOME"] = tempfile.mkdtemp()
root = sys.argv[1]; shot = sys.argv[2]
sys.path.insert(0, root); sys.path.insert(0, os.path.join(root, "tests"))
import test_engine as te
from http.server import ThreadingHTTPServer
from ydm import gui, engine, config
te.Handler.delay = 1.5
srv = ThreadingHTTPServer(("127.0.0.1", 0), te.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
s = config.Settings(); s["download_dir"] = tempfile.mkdtemp(); s["clipboard_monitor"] = False
eng = engine.Engine(s)
app = gui.App(eng)
te.Handler.delay = 0; done = eng.add(base + "/file.bin?done", filename="finished-archive.zip")
while done.status != "complete": time.sleep(0.1)
te.Handler.delay = 1.5
eng.add(base + "/file.bin?later", filename="setup.exe", start=False, in_queue=True)
dl = eng.add(base + "/file.bin?live", filename="big-video.mp4")
app.geometry("1100x420+20+20")
def step():
    app.show_progress(dl.id)
    w = app.progress_windows[dl.id]; w.geometry("560x400+620+300")
    gui.FileInfoDialog(app, url=base + "/named").geometry("+60+480")
    for cls in (gui.OptionsDialog, gui.SchedulerDialog, gui.BatchPatternDialog):
        d = cls(app); app.update(); d.destroy()
    gui.CompleteDialog(app, done).destroy()
    gui.PropertiesDialog(app, done).destroy()
    gui.BatchConfirmDialog(app, [base + "/a.zip", base + "/b.pdf"], {}).destroy()
    app.after(4000, snap)
def snap():
    app.update()
    import subprocess
    subprocess.run(["powershell", "-NoProfile", "-Command",
      "Add-Type -AssemblyName System.Drawing,System.Windows.Forms; $b=New-Object Drawing.Bitmap 1200,900; $g=[Drawing.Graphics]::FromImage($b); $g.CopyFromScreen(0,0,0,0,$b.Size); $b.Save('%s')" % shot])
    print("segments:", len(dl.segments), "status:", dl.status, "pct: %.1f" % dl.percent(), "conns:", dl.active_connections)
    app.on_close(force=True)
app.after(800, step)
app.mainloop()
print("gui ok")
