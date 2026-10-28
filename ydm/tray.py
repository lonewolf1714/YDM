"""Notification-area (tray) icon, like IDM's: YDM keeps running there with its window hidden.

pystray runs its own thread; every menu click is handed to the Tk thread through `post`.
Without pystray installed the app falls back to minimizing to the taskbar.
"""
try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - optional dependency
    pystray = None


def _image(size=64):
    """Blue rounded square with a white download arrow (same artwork as the extension icon)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=(30, 115, 216, 255))
    s = size / 64
    d.rectangle((27 * s, 11 * s, 37 * s, 35 * s), fill="white")                  # shaft
    d.polygon([(15 * s, 29 * s), (49 * s, 29 * s), (32 * s, 46 * s)], fill="white")  # head
    d.rectangle((14 * s, 50 * s, 50 * s, 55 * s), fill="white")                  # base line
    return img


class Tray:
    def __init__(self, post, limiter_on):
        """post(action) is called from the tray thread with "open", "add_url", "toggle_limit" or "exit"."""
        self.icon = None
        self.notified = False
        if pystray is None:
            return
        item = pystray.MenuItem
        menu = pystray.Menu(
            item("Open YDM", lambda icon, it: post("open"), default=True),
            item("Add new download…", lambda icon, it: post("add_url")),
            item("Speed limiter", lambda icon, it: post("toggle_limit"), checked=lambda it: limiter_on()),
            pystray.Menu.SEPARATOR,
            item("Exit", lambda icon, it: post("exit")),
        )
        try:
            self.icon = pystray.Icon("YDM", _image(), "YDM Download Manager", menu)
            self.icon.run_detached()
        except Exception:  # no notification area (e.g. some remote sessions): run without it
            self.icon = None

    @property
    def available(self):
        return self.icon is not None

    def set_tooltip(self, text):
        if self.icon and self.icon.title != text:
            self.icon.title = text[:127]

    def refresh_menu(self):
        if self.icon:
            self.icon.update_menu()

    def notify_once(self, message):
        """Tell the user, the first time the window is closed, that YDM is still running."""
        if self.icon and not self.notified:
            self.notified = True
            try:
                self.icon.notify(message, "YDM")
            except Exception:
                pass

    def stop(self):
        if self.icon:
            self.icon.stop()
            self.icon = None
