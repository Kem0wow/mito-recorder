"""Mito Recorder: record gameplay as training data for a game-playing AI (VLA).

Each run becomes one zip in the save folder, named after the game: <Game>_<N>.zip, holding
  video.mp4        H.264 (x264 veryfast, CRF 23), scaled down to at most 720p, constant FPS (default 30)
  frames.csv       frame, t (grab time), src_t (source frame arrival), fresh (0 = repeat of the previous frame)
  inputs.jsonl     keyboard + mouse from Windows Raw Input, logged only while the game window is focused,
                   plus focus / cursor-lock / window-move events
  game_audio.ogg   what the PC plays (WASAPI loopback), Opus 96 kbps, on the session clock
  meta.json        game, sizes, timing, system (pointer speed, refresh rate), user (DPI, sensitivity),
                   quality numbers + warnings
Everything shares one clock: session time 0 = Start. Thoughts / dataset labelling happen later, elsewhere.

CLI:  recorder.py                  -> UI
      recorder.py --selftest DIR   -> record the screen a few seconds with injected input, zip, verify
"""
import argparse
import csv
import itertools
import json
import os
import platform
import queue
import re
import shutil
import statistics
import sys
import threading
import time
import types
import webbrowser
import zipfile
from fractions import Fraction
from pathlib import Path

import numpy as np

VERSION = "2.0"
UPLOAD_URL = "https://drive.google.com/drive/folders/1g-eHWyhihvk86EOgC-639riVl9WZGBXo?usp=sharing"
HOWTO = ("HOW TO RECORD\n"
         "1. Start your game in windowed or borderless mode (not exclusive fullscreen).\n"
         "2. Pick the game window above and check the game name.\n"
         "3. Press Start, or F9 while in the game. Play. Press F9 again to stop. The zip is saved for you.\n"
         "4. Click \"Upload recordings\" and drop the .zip files (do not unzip them) into the Drive folder.")
TIPS = ("TIPS FOR GOOD DATA\n"
        "- Play seriously and normally, like you want to win or finish a level. Do not idle, pause or AFK.\n"
        "- Sessions of 10-60 minutes are ideal. Different games and different situations help more than one long run.\n"
        "- Keep the game window focused. When you alt-tab, nothing is recorded.\n"
        "- Mute voice chat and music apps: all sound your PC plays is recorded. Turn off Discord/Steam overlays.\n"
        "- Close heavy programs. If the result says low fps, lower the game's graphics or resolution.\n"
        "- Use your normal mouse settings. If you know your mouse DPI and the game's sensitivity, enter them.\n"
        "- After you stop, read the result line. \"Quality OK\" is good. If it shows a warning (black video, "
        "no sound, low fps), fix it and record again.")
IS_WIN = sys.platform == "win32"
FPS = 30
MIN_FREE = 3e9   # refuse to start with less free disk than this
LOW_FREE = 5e8   # stop (and save) a running session below this
MAX_H = 720
X264 = {"preset": "veryfast", "crf": "23"}
AUDIO_BPS = 96_000
LOG = Path.home() / "MitoRecorder.log"
CONSENT = ("Records only the chosen game window, all sound your PC plays (mute voice chat), and your keyboard "
           "and mouse while the game is focused. The data is used to train a game-playing AI.")

if sys.stdout is None or sys.stderr is None:  # --windowed exe: no console, send prints to a log file
    sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)

if IS_WIN:  # physical pixels, so window rects, capture and cursor positions agree
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()
    ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def mss_cls():
    import mss
    return getattr(mss, "MSS", None) or mss.mss  # mss 10 renamed the class


# ---------------------------------------------------------------- pure helpers (tested)

def safe_name(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s)
    return re.sub(r"\s+", " ", s).strip(" .")[:60].strip(" .") or "game"


def next_index(root, game):
    """1 + highest N among <game>_N folders / zips in root."""
    root = Path(root)
    pat = re.compile(re.escape(game) + r"_(\d+)(\.zip)?$", re.I)
    ns = [int(m.group(1)) for p in (root.iterdir() if root.is_dir() else []) if (m := pat.match(p.name))]
    return max(ns, default=0) + 1


def out_size(w, h, max_h=MAX_H):
    if h > max_h:
        w, h = round(w * max_h / h), max_h
    return int(w) // 2 * 2, int(h) // 2 * 2  # H.264 needs even sizes


def audio_pad(written, t, n, rate, tol=0.03):
    """Silent samples to insert before an n-sample chunk that ended at session time t.
    WASAPI loopback delivers nothing while the PC is silent; padding keeps audio on the session clock."""
    gap = round(t * rate) - n - written
    return gap if gap > tol * rate else 0


def quality(dur, fps, fresh, lags, bright, events, audio_s, silent, dropped):
    """Health numbers + human warnings for one session.
    fresh: 0/1 per frame (1 = a new source frame); lags: s between source frame arrival and our grab (fresh frames);
    bright: sampled mean pixel values; events: input dicts; audio_s/silent: file seconds, share of silent chunks."""
    n, on, last, kinds = len(fresh), 0.0, None, {}
    for e in events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        if e["kind"] == "focus":
            if e["on"]:
                last = e["t"]
            elif last is not None:
                on, last = on + e["t"] - last, None
    if last is not None:
        on += dur - last
    q = {"fps": round(n / dur, 2) if dur else 0, "fresh_frame_share": round(sum(fresh) / n, 3) if n else 0,
         "capture_lag_ms_median": round(statistics.median(lags) * 1000, 1) if lags else None,
         "black_share": round(sum(b < 3 for b in bright) / len(bright), 3) if bright else None,
         "focused_s": round(on, 1), "input_events": kinds, "audio_s": audio_s, "audio_silent_share": silent,
         "dropped": dropped}
    w = []
    if q["black_share"] and q["black_share"] > 0.5:
        w.append("video is mostly black: run the game in windowed or borderless mode")
    if dur and q["fps"] < 0.9 * fps:
        w.append(f"only {q['fps']} fps (target {fps}): close other programs or lower the game's graphics")
    if dropped > 0.01 * max(n, 1):
        w.append(f"{dropped} frames dropped")
    if audio_s is None:
        w.append("no game audio recorded")
    elif silent is not None and silent > 0.95:
        w.append("game audio is silent: is the game muted?")
    if dur > 10 and on < 0.5 * dur:
        w.append("the game window was focused less than half of the time")
    if dur > 10 and not any(k in kinds for k in ("key_down", "mouse_down", "move")):
        w.append("no keyboard or mouse input recorded")
    return q, w


def system_info():
    info = {"os": platform.platform()}
    if not IS_WIN:
        return info
    try:
        u, gdi = ctypes.windll.user32, ctypes.windll.gdi32
        spd, m = ctypes.c_uint(), (ctypes.c_int * 3)()
        u.SystemParametersInfoW(0x70, 0, ctypes.byref(spd), 0)  # SPI_GETMOUSESPEED, 1-20, 10 = default
        u.SystemParametersInfoW(3, 0, m, 0)                       # SPI_GETMOUSE: [thr1, thr2, accel]
        info.update(pointer_speed=spd.value, pointer_precision=bool(m[2]), system_dpi=u.GetDpiForSystem())
        u.GetDC.restype = ctypes.c_void_p
        u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
        dc = u.GetDC(None)
        info["refresh_hz"] = gdi.GetDeviceCaps(dc, 116)  # VREFRESH
        u.ReleaseDC(None, dc)
    except Exception as e:
        info["error"] = repr(e)
    return info


def cursor_state():
    """Cursor visible? Clipped to a region (games lock it for mouse-look)? clip=None when free."""
    from ctypes import byref, sizeof
    from ctypes import wintypes as W

    class CI(ctypes.Structure):
        _fields_ = [("cb", W.DWORD), ("flags", W.DWORD), ("h", W.HANDLE), ("pt", W.POINT)]

    u, ci, r = ctypes.windll.user32, CI(), W.RECT()
    ci.cb = sizeof(CI)
    u.GetCursorInfo(byref(ci))
    u.GetClipCursor(byref(r))
    x, y, w, h = (u.GetSystemMetrics(i) for i in (76, 77, 78, 79))  # virtual screen
    free = (r.left, r.top, r.right, r.bottom) == (x, y, x + w, y + h)
    return {"visible": bool(ci.flags & 1), "clip": None if free else [r.left, r.top, r.right, r.bottom]}


VK_NAMES = {8: "backspace", 9: "tab", 13: "enter", 16: "shift", 17: "ctrl", 18: "alt", 19: "pause",
            20: "caps_lock", 27: "esc", 32: "space", 33: "page_up", 34: "page_down", 35: "end", 36: "home",
            37: "left", 38: "up", 39: "right", 40: "down", 44: "print_screen", 45: "insert", 46: "delete",
            91: "win", 92: "win", 93: "menu", 160: "shift", 161: "shift", 162: "ctrl", 163: "ctrl", 164: "alt",
            165: "alt", 186: ";", 187: "=", 188: ",", 189: "-", 190: ".", 191: "/", 192: "`", 219: "[",
            220: "\\", 221: "]", 222: "'"}


def vk_name(vk):
    """US-layout name for a virtual-key code (layout-independent: the raw vk/sc are logged too)."""
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    if 0x60 <= vk <= 0x69:
        return f"num{vk - 0x60}"
    if 0x70 <= vk <= 0x87:
        return f"f{vk - 0x6F}"
    return VK_NAMES.get(vk, f"vk{vk}")


STORED = {".mp4", ".ogg", ".m4a"}  # already compressed: store, don't deflate


def zip_session(d):
    """<dir>/ -> <dir>.zip (written as .part, then renamed), folder removed. Returns the zip path."""
    d = Path(d)
    z = d.parent / f"{d.name}.zip"
    tmp = d.parent / f"{d.name}.zip.part"
    with zipfile.ZipFile(tmp, "w") as zf:
        for p in sorted(d.iterdir()):
            zf.write(p, f"{d.name}/{p.name}", zipfile.ZIP_STORED if p.suffix in STORED else zipfile.ZIP_DEFLATED)
    tmp.replace(z)
    shutil.rmtree(d)
    return z


class AudioOut:
    """Audio file on the session clock: chunks land at their session time, silent gaps become zeros."""

    def __init__(self, base, rate):
        import av
        codec, ext = ("libopus", ".ogg") if "libopus" in av.codecs_available else ("aac", ".m4a")
        self.path = Path(f"{base}{ext}")
        self.c = av.open(str(self.path), "w")
        self.st = self.c.add_stream(codec, rate=48000, layout="stereo")
        self.st.bit_rate = AUDIO_BPS
        self.rate, self.written = rate, 0

    def _put(self, a):
        import av
        fr = av.AudioFrame.from_ndarray(np.ascontiguousarray(a, np.int16).reshape(1, -1), format="s16",
                                        layout="stereo")
        fr.sample_rate, fr.pts, fr.time_base = self.rate, self.written, Fraction(1, self.rate)
        self.c.mux(self.st.encode(fr))
        self.written += len(a)

    def pad_to(self, t, n=0):
        pad = audio_pad(self.written, t, n, self.rate)
        while pad > 0:
            k = min(pad, self.rate)
            self._put(np.zeros((k, 2), np.int16))
            pad -= k

    def write(self, t, a):
        """a: int16 (n, channels) that ended at session time t. Keeps the first two channels."""
        a = a[:, :2] if a.shape[1] >= 2 else np.repeat(a, 2, axis=1)
        self.pad_to(t, len(a))
        self._put(a)

    def close(self):
        self.c.mux(self.st.encode(None))
        self.c.close()


# ---------------------------------------------------------------- capture targets

def list_monitors():
    with mss_cls()() as s:
        mons = s.monitors
    return [(f"Monitor {i} ({m['width']}x{m['height']})", (i, m)) for i, m in enumerate(mons) if i]


def window_rect(hwnd):
    """Visible window bounds (what Windows Graphics Capture records), in physical px."""
    from ctypes import byref, sizeof, wintypes
    r = wintypes.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, byref(r), sizeof(r)) and \
            not ctypes.windll.user32.GetWindowRect(hwnd, byref(r)):  # 9 = DWMWA_EXTENDED_FRAME_BOUNDS
        return None
    return {"left": r.left, "top": r.top, "width": r.right - r.left, "height": r.bottom - r.top}


def is_cloaked(hwnd):  # hidden UWP/background windows report visible but are not on screen
    from ctypes import byref, c_int, sizeof
    v = c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, byref(v), sizeof(v))
    return v.value != 0


def list_windows():
    """[(label, hwnd, title)] of visible top-level windows."""
    if not IS_WIN:
        return []
    from ctypes import wintypes
    u, out = ctypes.windll.user32, []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):
        if u.IsWindowVisible(h) and u.GetWindowTextLengthW(h) and not is_cloaked(h):
            buf = ctypes.create_unicode_buffer(512)
            u.GetWindowTextW(h, buf, 512)
            r = window_rect(h)
            if r and r["width"] > 64 and r["height"] > 64 and buf.value != "Mito Recorder":
                out.append((f"{buf.value[:70]} ({r['width']}x{r['height']})", h, buf.value))
        return True

    u.EnumWindows(cb, 0)
    return out


# ---------------------------------------------------------------- input (Windows Raw Input)

BUTTONS = {0x1: ("left", 1), 0x2: ("left", 0), 0x4: ("right", 1), 0x8: ("right", 0), 0x10: ("middle", 1),
           0x20: ("middle", 0), 0x40: ("x1", 1), 0x80: ("x1", 0), 0x100: ("x2", 1), 0x200: ("x2", 0)}


class RawInput:
    """Keyboard + mouse via Raw Input on a message-only window. Gives raw mouse counts (still moving when a game
    locks the cursor) and installs no low-level hooks, so it adds no input lag to the game. handler(dict) runs on
    this thread; mouse motion is summed and flushed every ~16 ms."""

    def __init__(self, handler):
        self.handler, self.tid, self.err, self.ready = handler, None, None, threading.Event()

    def start(self):
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()
        self.ready.wait(5)
        if self.err:
            raise RuntimeError(f"Raw Input: {self.err}")
        return self

    def stop(self):
        if self.tid:
            ctypes.windll.user32.PostThreadMessageW(self.tid, 0x0012, 0, 0)  # WM_QUIT
        self.th.join(2)

    def _run(self):
        from ctypes import POINTER, byref, c_int, sizeof
        from ctypes import wintypes as W

        class RID(ctypes.Structure):
            _fields_ = [("page", W.USHORT), ("usage", W.USHORT), ("flags", W.DWORD), ("hwnd", W.HWND)]

        class HDR(ctypes.Structure):
            _fields_ = [("type", W.DWORD), ("size", W.DWORD), ("dev", W.HANDLE), ("wparam", W.WPARAM)]

        class MOUSE(ctypes.Structure):
            _fields_ = [("flags", W.USHORT), ("buttons", W.ULONG), ("raw", W.ULONG), ("dx", W.LONG),
                        ("dy", W.LONG), ("extra", W.ULONG)]

        class KBD(ctypes.Structure):
            _fields_ = [("sc", W.USHORT), ("flags", W.USHORT), ("res", W.USHORT), ("vk", W.USHORT),
                        ("msg", W.UINT), ("extra", W.ULONG)]

        class DATA(ctypes.Union):
            _fields_ = [("m", MOUSE), ("k", KBD)]

        class RAW(ctypes.Structure):
            _fields_ = [("hdr", HDR), ("d", DATA)]

        u = ctypes.windll.user32
        u.CreateWindowExW.restype = W.HWND
        u.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, c_int, c_int, c_int, c_int,
                                      W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
        u.GetMessageW.argtypes = [POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
        u.DispatchMessageW.argtypes = [POINTER(W.MSG)]
        u.GetRawInputData.argtypes = [W.HANDLE, W.UINT, W.LPVOID, POINTER(W.UINT), W.UINT]
        u.GetRawInputData.restype = W.UINT
        try:
            self.tid = ctypes.windll.kernel32.GetCurrentThreadId()
            hwnd = u.CreateWindowExW(0, "STATIC", "mito-raw", 0, 0, 0, 0, 0, W.HWND(-3), None, None, None)
            if not hwnd:
                raise ctypes.WinError()
            rids = (RID * 2)(RID(1, 2, 0x100, hwnd), RID(1, 6, 0x100, hwnd))  # mouse, keyboard; INPUTSINK
            if not u.RegisterRawInputDevices(rids, 2, sizeof(RID)):
                raise ctypes.WinError()
            u.SetTimer(hwnd, 1, 16, None)
        except Exception as e:
            self.err = str(e)
            self.ready.set()
            return
        self.ready.set()
        msg, raw, size, pt, acc, t0m = W.MSG(), RAW(), W.UINT(), W.POINT(), [0, 0], [None]

        def cursor():
            u.GetCursorPos(byref(pt))
            return pt.x, pt.y

        def flush():  # "pc" = perf_counter of the slice's first count, so motion is stamped when it began
            if acc[0] or acc[1]:
                x, y = cursor()
                self.handler({"kind": "move", "dx": acc[0], "dy": acc[1], "x": x, "y": y, "pc": t0m[0]})
                acc[0] = acc[1] = 0
            t0m[0] = None

        while u.GetMessageW(byref(msg), None, 0, 0) > 0:
            try:
                if msg.message == 0x00FF:  # WM_INPUT
                    size.value = sizeof(raw)
                    if u.GetRawInputData(msg.lParam, 0x10000003, byref(raw), byref(size), sizeof(HDR)) not in \
                            (0, 0xFFFFFFFF):
                        if raw.hdr.type == 0:
                            m = raw.d.m
                            if not m.flags & 1:  # relative motion (absolute = tablet / remote desktop)
                                if t0m[0] is None:
                                    t0m[0] = time.perf_counter()
                                acc[0] += m.dx
                                acc[1] += m.dy
                            bf, bd = m.buttons & 0xFFFF, m.buttons >> 16 & 0xFFFF
                            if bf:
                                flush()
                                x, y = cursor()
                                for bit, (name, down) in BUTTONS.items():
                                    if bf & bit:
                                        self.handler({"kind": "mouse_down" if down else "mouse_up",
                                                      "button": name, "x": x, "y": y, "pc": time.perf_counter()})
                                for bit, axis in ((0x400, "dy"), (0x800, "dx")):  # 120 = one wheel notch
                                    if bf & bit:
                                        self.handler({"kind": "scroll", axis: (bd ^ 0x8000) - 0x8000, "x": x, "y": y,
                                                      "pc": time.perf_counter()})
                        elif raw.hdr.type == 1 and raw.d.k.vk != 255:
                            k = raw.d.k
                            self.handler({"kind": "key_up" if k.flags & 1 else "key_down", "key": vk_name(k.vk),
                                          "vk": k.vk, "sc": k.sc | (0xE000 if k.flags & 2 else 0),
                                          "pc": time.perf_counter()})
                elif msg.message == 0x0113:  # WM_TIMER
                    flush()
            except Exception as e:
                log(f"raw input: {e!r}")
            u.DispatchMessageW(byref(msg))
        u.DestroyWindow(hwnd)


# ---------------------------------------------------------------- recorder

class Recorder:
    def __init__(self, out, fps, region_fn, target=None, hwnd=None, game="", title="", audio=True, user=None):
        """target: ("window", hwnd) | ("monitor", 1-based index) for Windows Graphics Capture; None = GDI/mss.
        hwnd: inputs are kept only while this window is in the foreground (None = always)."""
        self.out, self.fps, self.region_fn = Path(out), fps, region_fn
        self.target, self.hwnd, self.game, self.title, self.audio = target, hwnd, game, title, audio
        self.backend, self.error, self.audio_info, self.audio_error = None, None, None, None
        self.stop_ev, self.lock = threading.Event(), threading.Lock()
        self.q = queue.Queue(maxsize=fps)  # ~1 s of frames; full => drop, never stall capture
        self.frames = self.dropped = 0
        self.user, self.sysinfo, self.note = user or {}, system_info(), None
        self.down, self.focus, self.threads, self.fh = {}, None, [], None
        self.fresh, self.lags, self.bright, self.cstate, self.audio_stats = [], [], [], None, None

    def t(self):
        return time.perf_counter() - self.t0

    def start(self):
        r = self.region_fn()
        if not r or r["width"] < 2 or r["height"] < 2:
            raise RuntimeError("the game window is minimised or gone")
        self.region = {k: r[k] for k in ("left", "top", "width", "height")}
        self.vsize = out_size(r["width"], r["height"])
        self.out.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.out).free < MIN_FREE:
            self.out.rmdir()
            raise RuntimeError(f"less than {MIN_FREE / 1e9:.0f} GB free on that drive: pick another save folder")
        self.fh = open(self.out / "inputs.jsonl", "w", encoding="utf-8")  # streamed: a crash keeps the log
        self.region_now = dict(self.region)
        self.started = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.t0 = time.perf_counter()
        # focus is known from t=0, not after the capture backend finishes starting (WGC can take ~2 s)
        self._set_focus(self.hwnd is None or ctypes.windll.user32.GetForegroundWindow() == self.hwnd)
        workers =[self._capture, self._encode] + ([self._game_audio] if self.audio and IS_WIN else [])
        self.threads = [threading.Thread(target=f, daemon=True) for f in workers]
        for th in self.threads:
            th.start()
        return self

    def stop(self):
        self.stop_ev.set()
        for th in self.threads:
            th.join()
        dur = self.t()
        with self.lock:
            self.fh.close()
        p = self.out / "inputs.jsonl"
        events = sorted((json.loads(x) for x in open(p, encoding="utf-8")), key=lambda e: e["t"])
        p.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        q, warnings = quality(dur, self.fps, self.fresh, self.lags, self.bright, events,
                              *(self.audio_stats or (None, None)), self.dropped)
        if self.audio_error:
            warnings = [w for w in warnings if w != "no game audio recorded"] + [f"game audio failed: {self.audio_error}"]
        if self.note:
            warnings.append(self.note)
        meta = {"recorder": VERSION, "game": self.game, "window_title": self.title, "started": self.started,
                "duration_s": round(dur, 3), "fps_requested": self.fps,
                "fps_actual": round(self.frames / dur, 2) if dur else 0, "frames": self.frames,
                "dropped": self.dropped, "capture": self.backend, "region": self.region,
                "video_size": list(self.vsize), "video": f"H.264 {X264}", "audio": self.audio_info,
                "audio_error": self.audio_error,
                "inputs": "Windows Raw Input, only while the game window is focused. t = session s. "
                          "x/y = absolute screen px (see region). move dx/dy = raw mouse counts summed over "
                          "~16 ms, stamped when the motion began. scroll 120 = one notch. vk/sc = Windows "
                          "virtual-key / scan code. Sorted by t. cursor events: visible + clip (clip set = cursor "
                          "locked to a region, i.e. mouse-look). region events: window moved/resized. focus: game "
                          "window gained/lost focus (keys still held are released with forced=true).",
                "error": self.error, "user": self.user, "system": self.sysinfo, "quality": q, "warnings": warnings}
        (self.out / "meta.json").write_text(json.dumps(meta, indent=1))
        return meta

    # input (Raw Input thread)
    def _log(self, t, ev):  # caller holds self.lock
        if not self.fh.closed:
            self.fh.write(json.dumps({"t": round(max(t, 0.0), 4), **ev}) + "\n")

    def on_raw(self, ev):
        if not self.focus:
            return
        pc = ev.pop("pc", None)
        t = pc - self.t0 if pc is not None else self.t()
        kind = ev["kind"]
        with self.lock:
            if kind in ("key_down", "key_up"):
                kid = (ev["vk"], ev["sc"])
                if (kind == "key_down") == (kid in self.down):
                    return  # OS auto-repeat, or release of a key pressed before focus
                if kind == "key_down":
                    self.down[kid] = ev
                else:
                    del self.down[kid]
            self._log(t, ev)

    def _set_focus(self, f):
        with self.lock:
            t = self.t()
            self._log(t, {"kind": "focus", "on": f})
            if not f:  # keys held while alt-tabbing out would never see their key_up
                for ev in self.down.values():
                    self._log(t, {**ev, "kind": "key_up", "forced": True})
                self.down.clear()
            self.focus = f

    def _watch(self):
        """Twice a second: log cursor lock/visibility and window moves, flush the input log to disk."""
        with self.lock:
            if IS_WIN:
                s = cursor_state()
                if s != self.cstate:
                    self.cstate = s
                    self._log(self.t(), {"kind": "cursor", **s})
            if self.target and self.target[0] == "window":
                r = self.region_fn()
                r = {k: r[k] for k in ("left", "top", "width", "height")} if r else None
                if r and r != self.region_now:
                    self.region_now = r
                    self._log(self.t(), {"kind": "region", **r})
            self.fh.flush()

    # threads
    def _wgc(self):
        """Windows Graphics Capture: any window (DirectX/OpenGL/Vulkan, covered by other windows) or monitor."""
        sys.modules.setdefault("cv2", types.ModuleType("cv2"))  # cv2 only backs save_as_image; skip ~40 MB
        from windows_capture import WindowsCapture
        kind, val = self.target
        latest, got, seq = [None], threading.Event(), itertools.count(1)
        for border in (False, None):  # border toggle unsupported on older Windows 10
            try:
                cap = WindowsCapture(cursor_capture=True, draw_border=border,
                                     minimum_update_interval=max(1, int(500 / self.fps)),
                                     **({"window_hwnd": val} if kind == "window" else {"monitor_index": val}))

                @cap.event
                def on_frame_arrived(frame, _ctl):
                    # (BGRA (h, w, 4) copy: the buffer is only valid in here, frame id, arrival session time)
                    latest[0] = (frame.frame_buffer.copy(), next(seq), self.t())
                    got.set()

                @cap.event
                def on_closed():
                    log("capture target closed")

                ctl = cap.start_free_threaded()
                break
            except Exception:
                if border is None:
                    raise
        if not got.wait(3):
            ctl.stop()
            raise RuntimeError("no frames within 3 s")
        return (lambda: latest[0]), ctl.stop

    def _mss(self):
        sct, region, last = mss_cls()(), dict(self.region), [time.perf_counter()]

        def grab():
            if time.perf_counter() - last[0] > 1:  # follow a moved window, size stays fixed
                last[0], r = time.perf_counter(), self.region_fn()
                if r and r["width"] > 0:
                    region.update(left=r["left"], top=r["top"])
            shot = sct.grab(region)
            return np.frombuffer(shot.bgra, np.uint8).reshape(shot.height, shot.width, 4), None, self.t()
        return grab, sct.close

    def _capture(self):
        period, close = 1 / self.fps, None
        u = ctypes.windll.user32 if IS_WIN else None
        try:
            if IS_WIN and self.target and self.target[1]:
                try:
                    grab, close = self._wgc()
                    self.backend = "wgc"
                except Exception as e:
                    log(f"Windows Graphics Capture unavailable ({e}); using GDI")
            if not close:
                grab, close = self._mss()
                self.backend = "gdi"
            black, last_seq, last_watch = None, None, -1.0
            nxt = time.perf_counter()
            while not self.stop_ev.is_set():
                now = time.perf_counter()
                if now < nxt:
                    time.sleep(min(nxt - now, 0.05))
                    continue
                nxt = nxt + period if nxt + period > now else now + period  # behind => skip, don't burst
                f = self.hwnd is None or u.GetForegroundWindow() == self.hwnd
                if f != self.focus:
                    self._set_focus(f)
                if now - last_watch >= 0.5:
                    last_watch = now
                    self._watch()
                try:
                    if self.backend == "gdi" and not f:  # GDI films whatever covers the game: never record that
                        if black is None:
                            black = np.zeros((self.region["height"], self.region["width"], 4), np.uint8)
                        img, seq, src_t = black, -1, self.t()
                    else:
                        img, seq, src_t = grab()  # WGC: latest frame; unchanged screen repeats it (constant fps)
                    fresh, last_seq = seq is None or seq != last_seq, seq
                    self.q.put_nowait((self.t(), img, src_t, int(fresh)))
                except Exception:
                    self.dropped += 1
        except Exception as e:
            self.error = f"capture: {e}"
            log(self.error)
        finally:
            if close:
                close()
            self.q.put(None)

    def _encode(self):
        import av
        w, h = self.vsize
        # fragmented mp4: a crash or power loss leaves a playable file
        with av.open(str(self.out / "video.mp4"), "w",
                     options={"movflags": "frag_keyframe+empty_moov+default_base_moof"}) as c, \
                open(self.out / "frames.csv", "w", newline="") as f:
            try:  # GOP 2 s: cheap random access when sampling frames for training
                st = c.add_stream("libx264", rate=self.fps, options={**X264, "g": str(self.fps * 2)})
            except Exception:
                st = c.add_stream("mpeg4", rate=self.fps)
                st.bit_rate = 6_000_000
            st.width, st.height, st.pix_fmt = w, h, "yuv420p"
            wr = csv.writer(f)
            # t = session time we grabbed the frame; src_t = when the source frame arrived; fresh = 0 if repeated
            wr.writerow(["frame", "t", "src_t", "fresh"])
            while (item := self.q.get()) is not None:
                t, img, src_t, fresh = item
                # one swscale pass: BGRA -> yuv420p at <=720p; AREA keeps small UI text readable
                fr = av.VideoFrame.from_ndarray(img, format="bgra").reformat(
                    width=w, height=h, format="yuv420p", interpolation="AREA")
                fr.pts = self.frames
                c.mux(st.encode(fr))
                wr.writerow([self.frames, f"{t:.4f}", f"{src_t:.4f}", fresh])
                self.fresh.append(fresh)
                if fresh:
                    self.lags.append(t - src_t)
                if self.frames % 30 == 0:
                    self.bright.append(float(img[::16, ::16, :3].mean()))
                    f.flush()
                self.frames += 1
            c.mux(st.encode())

    def _game_audio(self):
        # ponytail: whole-PC loopback (voice chat leaks in). Per-process capture needs Win10 2004+
        # ActivateAudioInterfaceAsync(PROCESS_LOOPBACK); add if friends can't mute voice chat.
        out, stats = None, [0, 0]  # chunks, silent chunks
        try:
            import pyaudiowpatch as pa
            p = pa.PyAudio()
            try:
                dev = p.get_default_wasapi_loopback()
                rate, ch = int(dev["defaultSampleRate"]), int(dev["maxInputChannels"])
                q = queue.Queue()
                out = AudioOut(self.out / "game_audio", rate)

                def cb(data, n, _info, _status):
                    q.put((self.t(), data, n))
                    return None, pa.paContinue

                s = p.open(format=pa.paInt16, channels=ch, rate=rate, input=True, input_device_index=dev["index"],
                           frames_per_buffer=rate // 50, stream_callback=cb)
                self.audio_info = {"file": out.path.name, "device": dev["name"], "device_rate": rate,
                                   "device_channels": ch, "codec": out.st.codec_context.name,
                                   "bit_rate": AUDIO_BPS, "clock": "sample 0 = session t 0; silence padded"}

                def drain(block):
                    while True:
                        try:
                            t, data, n = q.get(timeout=0.2) if block else q.get_nowait()
                        except queue.Empty:
                            return
                        a = np.frombuffer(data, np.int16).reshape(n, ch)
                        stats[0] += 1
                        stats[1] += int(a.min() > -8 and a.max() < 8)
                        out.write(t, a)

                while not self.stop_ev.is_set():
                    drain(True)
                s.stop_stream()
                s.close()
                drain(False)
            finally:
                p.terminate()
        except Exception as e:
            self.audio_error = f"{type(e).__name__}: {e}"
            log(f"game audio: {self.audio_error}")
        finally:
            if out:
                out.pad_to(self.t())  # trailing silence: the file ends with the session
                out.close()
                self.audio_stats = (round(out.written / out.rate, 2), round(stats[1] / max(stats[0], 1), 3))


# ---------------------------------------------------------------- UI

class App:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.root = root = tk.Tk()
        root.title("Mito Recorder")
        root.resizable(False, False)
        self.fps, self.game = tk.IntVar(value=FPS), tk.StringVar()
        self.dpi, self.sens = tk.StringVar(), tk.StringVar()
        self.out = tk.StringVar(value=str(Path.home() / "MitoData"))
        self.status = tk.StringVar(value="Pick the game window, then press Start (or F9 in game).")
        self.rec, self.raw, self.busy, self.toggle_req, self.msg, self.f9 = None, None, False, False, None, False
        f = ttk.Frame(root, padding=12)
        f.grid()
        row = itertools.count()

        def line(label, *widgets):
            r = next(row)
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3)
            for c, w in enumerate(widgets, 1):
                w.grid(row=r, column=c, sticky="we", padx=3)

        self.target = ttk.Combobox(f, state="readonly", width=55)
        self.target.bind("<<ComboboxSelected>>", lambda _e: self._pick())
        line("Game window", self.target, ttk.Button(f, text="Refresh", command=self.refresh))
        line("Game name", ttk.Entry(f, textvariable=self.game, width=58))
        line("FPS", ttk.Spinbox(f, from_=1, to=60, textvariable=self.fps, width=6))
        line("Mouse DPI (optional)", ttk.Entry(f, textvariable=self.dpi, width=10))
        line("Game sensitivity (optional)", ttk.Entry(f, textvariable=self.sens, width=10))
        line("Save to", ttk.Entry(f, textvariable=self.out, width=58), ttk.Button(f, text="Browse", command=self.browse))
        self.btn = ttk.Button(f, text="Start (F9)", command=self.toggle)
        self.btn.grid(row=next(row), column=0, columnspan=3, sticky="we", pady=8)
        ttk.Label(f, textvariable=self.status, wraplength=520).grid(row=next(row), column=0, columnspan=3, sticky="w")
        bf = ttk.Frame(f)
        bf.grid(row=next(row), column=0, columnspan=3, sticky="we", pady=(0, 6))
        ttk.Button(bf, text="Upload recordings (Google Drive)", command=lambda: webbrowser.open(UPLOAD_URL)
                   ).pack(side="left", expand=True, fill="x", padx=(0, 3))
        ttk.Button(bf, text="Open my recordings folder", command=self.open_folder
                   ).pack(side="left", expand=True, fill="x", padx=(3, 0))
        for txt in (HOWTO, TIPS, CONSENT):
            ttk.Label(f, text=txt, wraplength=520, justify="left",
                      foreground="#666" if txt is CONSENT else "").grid(row=next(row), column=0, columnspan=3,
                                                                       sticky="w", pady=(6, 0))
        self.refresh()
        if IS_WIN:
            try:
                self.raw = RawInput(self._on_raw).start()
            except Exception as e:
                self.status.set(f"Keyboard/mouse capture unavailable: {e}")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(200, self.tick)

    def _on_raw(self, ev):  # Raw Input thread
        if ev.get("vk") == 0x78:  # F9 = start/stop, never logged
            down = ev["kind"] == "key_down"
            if down and not self.f9:
                self.toggle_req = True
            self.f9 = down
            return
        rec = self.rec
        if rec:
            rec.on_raw(ev)

    def open_folder(self):
        p = Path(self.out.get())
        p.mkdir(parents=True, exist_ok=True)
        if IS_WIN:
            os.startfile(p)

    def browse(self):
        from tkinter import filedialog
        p = filedialog.askdirectory(initialdir=self.out.get())
        if p:
            self.out.set(p)

    def refresh(self):
        self.targets = list_windows()
        self.target["values"] = [lbl for lbl, _, _ in self.targets]
        if self.targets:
            self.target.current(0)
        else:
            self.target.set("")
        self._pick()

    def _pick(self):
        i = self.target.current()
        if i >= 0:
            self.game.set(safe_name(self.targets[i][2]))

    def toggle(self):
        from tkinter import messagebox
        if self.busy:
            return
        if self.rec:
            rec, self.rec = self.rec, None
            self.busy = True
            self.btn.config(text="Saving...", state="disabled")
            self.status.set("Finishing video and zipping...")
            threading.Thread(target=self._finish, args=(rec,), daemon=True).start()
            return
        i = self.target.current()
        if i < 0:
            messagebox.showerror("Mito Recorder", "Pick the game window first.")
            return
        _, hwnd, title = self.targets[i]
        game, root = safe_name(self.game.get()), Path(self.out.get())
        d = root / f"{game}_{next_index(root, game)}"
        try:
            fps = max(1, min(60, int(self.fps.get())))
            self.rec = Recorder(d, fps, lambda: window_rect(hwnd), target=("window", hwnd), hwnd=hwnd,
                                game=game, title=title,
                                user={"mouse_dpi": self.dpi.get().strip(),
                                      "game_sensitivity": self.sens.get().strip()}).start()
        except Exception as e:
            messagebox.showerror("Mito Recorder", str(e))
            return
        self.btn.config(text="Stop (F9)")

    def _finish(self, rec):
        try:
            meta = rec.stop()
            z = zip_session(rec.out)
            self.msg = (f"Saved {z.name}: {meta['duration_s'] / 60:.1f} min, {meta['fps_actual']} fps, "
                        f"{z.stat().st_size / 1e9:.2f} GB." +
                        (f" WARNINGS: {'; '.join(meta['warnings'])}" if meta["warnings"] else " Quality OK.") +
                        " Next: upload the zip to the Drive folder (button above).")
        except Exception as e:
            log(f"save failed: {e!r}")
            self.msg = f"Save failed: {e}. Raw files are kept in {rec.out}"
        self.busy = False

    def close(self):
        if self.rec:  # never lose a running session on window close
            rec, self.rec = self.rec, None
            self.busy = True
            self._finish(rec)
        while self.busy:
            time.sleep(0.1)
        if self.raw:
            self.raw.stop()
        self.root.destroy()

    def tick(self):
        if self.toggle_req:
            self.toggle_req = False
            self.toggle()
        if self.msg:
            self.status.set(self.msg)
            self.msg = None
        if self.rec and shutil.disk_usage(self.rec.out).free < LOW_FREE:
            self.rec.note = "stopped early: the disk was almost full"
            self.toggle()
        if self.rec:
            r, t = self.rec, self.rec.t()
            self.status.set(f"REC {int(t // 60):02d}:{int(t % 60):02d}  {r.out.name}  {r.frames / max(t, 1e-6):.1f} fps"
                            f"  dropped {r.dropped}" + ("" if r.focus else "  (game not focused: inputs paused)") +
                            (f"  ! {r.error}" if r.error else ""))
        elif not self.busy and str(self.btn["state"]) == "disabled":
            self.btn.config(text="Start (F9)", state="normal")
        self.root.after(200, self.tick)


# ---------------------------------------------------------------- selftest / main

def inject_test_input():
    """SendInput: relative mouse move (40, 25), then press + release X. Returns events accepted."""
    from ctypes import wintypes as W

    class MI(ctypes.Structure):
        _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("data", W.DWORD), ("flags", W.DWORD), ("time", W.DWORD),
                    ("extra", ctypes.c_size_t)]

    class KI(ctypes.Structure):
        _fields_ = [("vk", W.WORD), ("sc", W.WORD), ("flags", W.DWORD), ("time", W.DWORD), ("extra", ctypes.c_size_t)]

    class U(ctypes.Union):
        _fields_ = [("mi", MI), ("ki", KI)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", W.DWORD), ("u", U)]

    ins = [INPUT(0, U(mi=MI(40, 25, 0, 1, 0, 0))), INPUT(1, U(ki=KI(0x58, 0, 0, 0, 0))),
           INPUT(1, U(ki=KI(0x58, 0, 2, 0, 0)))]
    return ctypes.windll.user32.SendInput(len(ins), (INPUT * len(ins))(*ins), ctypes.sizeof(INPUT))


def selftest(a):
    root, game = Path(a.selftest), "Self Test"
    shutil.rmtree(root, ignore_errors=True)
    d = root / f"{game}_{next_index(root, game)}"
    log(f"platform={sys.platform} windows={len(list_windows())}")
    idx, mon = list_monitors()[0][1]
    rec = Recorder(d, FPS, lambda: mon, target=("monitor", idx), game=game, title=game).start()
    raw = RawInput(rec.on_raw).start() if IS_WIN else None
    end = time.time() + 10
    while rec.frames == 0 and time.time() < end:  # capture backend startup is slow on CI
        time.sleep(0.1)
    time.sleep(0.5)
    sent = inject_test_input() if IS_WIN else 0
    time.sleep(a.seconds)
    meta = rec.stop()
    if raw:
        raw.stop()
    log(f"recorded {json.dumps(meta)}")
    assert meta["frames"] > 0 and not meta["error"], meta
    assert meta["video_size"][1] <= MAX_H, meta["video_size"]
    import av
    with av.open(str(d / "video.mp4")) as c:  # the fragmented mp4 must decode completely
        n = sum(1 for _ in c.decode(video=0))
    assert abs(n - meta["frames"]) <= 1, (n, meta["frames"])
    rows = list(csv.DictReader(open(d / "frames.csv")))
    assert len(rows) == meta["frames"] and set(rows[0]) == {"frame", "t", "src_t", "fresh"}, rows[:1]
    assert meta["quality"]["fresh_frame_share"] > 0, meta["quality"]
    log(f"quality {json.dumps(meta['quality'])} warnings {meta['warnings']}")
    inputs = [json.loads(x) for x in open(d / "inputs.jsonl", encoding="utf-8")]
    log(f"inputs {inputs}")
    assert all("pc" not in e for e in inputs) and [e["t"] for e in inputs] == sorted(e["t"] for e in inputs)
    if IS_WIN:
        assert any(e["kind"] == "cursor" for e in inputs), "cursor state missing"
        assert meta["system"].get("pointer_speed"), meta["system"]
    if sent == 3:
        assert any(e["kind"] == "move" and (e["dx"] or e["dy"]) for e in inputs), "raw mouse move missing"
        assert any(e["kind"] == "key_down" and e["key"] == "x" for e in inputs), "raw key missing"
    else:
        log(f"WARNING: SendInput accepted {sent}/3 events (no interactive desktop?); input path untested")
    z = zip_session(d)
    names = zipfile.ZipFile(z).namelist()
    need = [f"{d.name}/{n}" for n in ("video.mp4", "frames.csv", "inputs.jsonl", "meta.json")]
    assert all(n in names for n in need), names
    log(f"zip {z.name} {z.stat().st_size / 1e6:.2f} MB for {meta['duration_s']} s at {meta['video_size']}: {names}")
    log("SELFTEST OK")


def uitest():
    app = App()
    app.root.update()
    log(f"UI OK windows={len(app.targets)} raw_input={'on' if app.raw else 'off'}")
    app.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", metavar="DIR")
    ap.add_argument("--uitest", action="store_true")
    ap.add_argument("--seconds", type=float, default=3)
    a = ap.parse_args()
    try:
        if a.uitest:
            uitest()
        elif a.selftest:
            selftest(a)
        else:
            App().root.mainloop()
    except Exception as e:
        log(f"FAILED: {e!r}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
