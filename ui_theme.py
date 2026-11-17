"""Look of the Mito Recorder window: ghost white and forest green, flat, no cards, a honeycomb strip on the right edge.

Pure tkinter, no extra packages. Everything that draws is created lazily, so importing this module costs nothing
until the window is built (and the unit tests run without a display)."""
import math
import sys

GHOST = dict(bg="#F8F8FF", surface="#F8F8FF", raised="#ECEEF8", border="#D6DBEA", text="#1C2A1F", muted="#6A776D",
             accent="#228B22", accent_hover="#1B721B", accent_fg="#F8F8FF", danger="#B3261E", danger_hover="#8E1D17",
             warn="#B26A00", info="#228B22", ok_bg="#E1F0E1", warn_bg="#FFF0D2", bad_bg="#FBE2E0", sel="#DCEADC",
             hive="#CFE4CF")
PAL = GHOST

FAMILY = "Segoe UI" if sys.platform == "win32" else "TkDefaultFont"
MONO = "Consolas" if sys.platform == "win32" else "TkFixedFont"


def font(size=10, bold=False, mono=False):
    return (MONO if mono else FAMILY, size, "bold" if bold else "normal")


def scale(root):
    """Display scale (1.0 = 96 dpi): pixel sizes follow it, fonts already do (they are in points)."""
    try:
        return max(1.0, root.winfo_fpixels("1i") / 96.0)
    except Exception:
        return 1.0


def work_area(root):
    """(x, y, w, h) of the screen without the taskbar, in pixels."""
    try:
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0):  # SPI_GETWORKAREA
            return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def mix(a, b, t):
    """Colour a blended t (0..1) towards colour b, both '#rrggbb'."""
    ca, cb = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def apply(root, pal=PAL):
    """Configure ttk (clam) and the window background. Call before building widgets."""
    from tkinter import ttk
    s, sc = ttk.Style(root), scale(root)
    px = lambda n: max(1, round(n * sc))  # noqa: E731
    s.theme_use("clam")
    root.configure(bg=pal["bg"])
    root.option_add("*TCombobox*Listbox.background", pal["raised"])
    root.option_add("*TCombobox*Listbox.foreground", pal["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", pal["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", pal["accent_fg"])
    root.option_add("*TCombobox*Listbox.font", font(10))
    s.configure(".", background=pal["bg"], foreground=pal["text"], font=font(10), bordercolor=pal["border"],
                focuscolor=pal["bg"], troughcolor=pal["raised"])
    s.configure("TFrame", background=pal["bg"])
    s.configure("TLabel", background=pal["bg"], foreground=pal["text"])
    base = dict(relief="flat", borderwidth=0, padding=(px(12), px(6)), font=font(9, True), focuscolor=pal["bg"])
    s.configure("TButton", background=pal["raised"], foreground=pal["text"], **base)
    s.map("TButton", background=[("disabled", pal["raised"]), ("active", pal["sel"])],
          foreground=[("disabled", pal["muted"])])
    s.configure("Accent.TButton", background=pal["accent"], foreground=pal["accent_fg"], **base)
    s.map("Accent.TButton", background=[("disabled", pal["raised"]), ("active", pal["accent_hover"])],
          foreground=[("disabled", pal["muted"])])
    big = dict(base, padding=(px(12), px(10)), font=font(11, True))
    s.configure("Big.Accent.TButton", background=pal["accent"], foreground=pal["accent_fg"], **big)
    s.map("Big.Accent.TButton", background=[("disabled", pal["raised"]), ("active", pal["accent_hover"])],
          foreground=[("disabled", pal["muted"])])
    s.configure("Big.Danger.TButton", background=pal["danger"], foreground="#ffffff", **big)
    s.map("Big.Danger.TButton", background=[("disabled", pal["raised"]), ("active", pal["danger_hover"])],
          foreground=[("disabled", pal["muted"])])
    s.configure("Seg.Toolbutton", background=pal["raised"], foreground=pal["muted"], relief="flat", borderwidth=0,
                padding=(px(11), px(5)), font=font(9, True), focuscolor=pal["raised"])
    s.map("Seg.Toolbutton", background=[("selected", pal["accent"]), ("active", pal["sel"])],
          foreground=[("selected", pal["accent_fg"]), ("active", pal["text"])])
    field = dict(fieldbackground=pal["raised"], foreground=pal["text"], bordercolor=pal["raised"],
                 lightcolor=pal["raised"], darkcolor=pal["raised"], insertcolor=pal["text"], padding=px(4))
    s.configure("TEntry", **field)
    s.map("TEntry", bordercolor=[("focus", pal["accent"])], lightcolor=[("focus", pal["accent"])],
          darkcolor=[("focus", pal["accent"])])
    s.configure("TCombobox", background=pal["raised"], arrowcolor=pal["muted"], **field)
    s.map("TCombobox", fieldbackground=[("readonly", pal["raised"])], foreground=[("readonly", pal["text"])],
          selectbackground=[("readonly", pal["raised"])], selectforeground=[("readonly", pal["text"])],
          bordercolor=[("focus", pal["accent"])], arrowcolor=[("active", pal["text"])],
          background=[("active", pal["raised"])])
    s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    s.configure("Treeview", background=pal["bg"], fieldbackground=pal["bg"], foreground=pal["text"],
                rowheight=px(26), borderwidth=0, relief="flat", font=font(10))
    s.map("Treeview", background=[("selected", pal["sel"])], foreground=[("selected", pal["text"])])
    s.configure("Treeview.Heading", background=pal["bg"], foreground=pal["muted"], relief="flat", borderwidth=0,
                padding=(px(6), px(5)), font=font(9, True))
    s.map("Treeview.Heading", background=[("active", pal["bg"])])


# ---------------------------------------------------------------- hive

def hive(root, pal, w, h, r):
    """Honeycomb strip on the right edge of the window (w x h px): every second cell is centred on the edge, so
    half of it is outside the window. Drawn once on a canvas behind everything. Returns (canvas, strip width)."""
    import tkinter as tk
    c = tk.Canvas(root, width=w, height=h, bg=pal["bg"], highlightthickness=0, borderwidth=0)
    c.place(x=0, y=0)
    dx, dy = math.sqrt(3) * r, 1.5 * r
    filled = {2: pal["accent"], 3: mix(pal["accent"], pal["bg"], 0.45), 5: mix(pal["accent"], pal["bg"], 0.72),
              9: mix(pal["accent"], pal["bg"], 0.88)}  # a few solid cells, the rest only an outline
    for k in range(-1, int(h / dy) + 2):
        cx, cy = (w if k % 2 == 0 else w - dx / 2), k * dy + r
        pts = [(cx + r * math.cos(math.radians(60 * i - 30)), cy + r * math.sin(math.radians(60 * i - 30)))
               for i in range(6)]
        c.create_polygon(pts, fill=filled.get(k, pal["bg"]), outline=pal["hive"], width=1)
    return c, dx


# ---------------------------------------------------------------- small widgets

def label(parent, pal, text="", kind="body", bg=None, **kw):
    """Text in the palette. kinds: title, h, body, muted, small, num (big monospaced digits)."""
    import tkinter as tk
    spec = {"title": (font(13, True), "text"), "h": (font(10, True), "text"), "body": (font(10), "text"),
            "muted": (font(10), "muted"), "small": (font(9), "muted"), "num": (font(22, True, mono=True), "text")}
    f, color = spec[kind]
    opts = dict(text=text, font=f, fg=pal[color], bg=bg or parent.cget("bg"), anchor="w", justify="left")
    opts.update(kw)  # callers may override anything (fg, wraplength, ...)
    return tk.Label(parent, **opts)


CHIP = {"ok": ("ok_bg", "accent"), "warn": ("warn_bg", "warn"), "bad": ("bad_bg", "danger"),
        "idle": ("raised", "muted")}


def chip(parent, pal, text="", state="idle", **kw):
    import tkinter as tk
    sc = scale(parent)
    c = tk.Label(parent, font=font(8, True), padx=round(7 * sc), pady=round(3 * sc), **kw)
    set_chip(c, pal, text, state)
    return c


def set_chip(c, pal, text, state):
    bg, fg = CHIP[state]
    if (c.cget("text"), c.cget("bg")) != (text, pal[bg]):  # no redraw when nothing changed
        c.config(text=text, bg=pal[bg], fg=pal[fg])


class Tabs:
    """Underlined tab strip + content area. API mirrors the bits of ttk.Notebook the app used."""

    def __init__(self, parent, pal):
        import tkinter as tk
        self.pal, self.sc = pal, scale(parent)
        self.root = tk.Frame(parent, bg=pal["bg"])
        self.bar = tk.Frame(self.root, bg=pal["bg"])
        self.bar.pack(fill="x", padx=(round(14 * self.sc), 0))
        self.body = tk.Frame(self.root, bg=pal["bg"])
        self.body.pack(fill="both", expand=True)
        self.items, self.current = [], None

    def add(self, frame, text):
        import tkinter as tk
        pal = self.pal
        box = tk.Frame(self.bar, bg=pal["bg"], cursor="hand2")
        box.pack(side="left")
        lab = tk.Label(box, text=text, font=font(10, True), bg=pal["bg"], fg=pal["muted"],
                       padx=round(8 * self.sc), pady=round(6 * self.sc))
        lab.pack()
        line = tk.Frame(box, bg=pal["bg"], height=max(2, round(2 * self.sc)))
        line.pack(fill="x", padx=round(8 * self.sc))
        for w in (box, lab):
            w.bind("<Button-1>", lambda _e, f=frame: self.select(f))
            w.bind("<Enter>", lambda _e, f=frame: self._hover(f, True))
            w.bind("<Leave>", lambda _e, f=frame: self._hover(f, False))
        self.items.append({"frame": frame, "label": lab, "line": line})
        if self.current is None:
            self.select(frame)

    def _item(self, frame):
        return next(i for i in self.items if i["frame"] is frame)

    def _hover(self, frame, on):
        it = self._item(frame)
        if frame is not self.current:
            it["label"].config(fg=self.pal["text"] if on else self.pal["muted"])

    def select(self, frame):
        for it in self.items:
            on = it["frame"] is frame
            it["label"].config(fg=self.pal["accent"] if on else self.pal["muted"])
            it["line"].config(bg=self.pal["accent"] if on else self.pal["bg"])
            if on:
                it["frame"].pack(in_=self.body, fill="both", expand=True)
            else:
                it["frame"].pack_forget()
        self.current = frame

    def tab(self, frame, text=None):
        if text is not None:
            self._item(frame)["label"].config(text=text)
