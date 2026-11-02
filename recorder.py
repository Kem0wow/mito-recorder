"""Mito Recorder: record gameplay + spoken English thoughts + inputs into a VLM fine-tuning dataset.

Record: screen/window frames (mss -> H.264 mp4 + frames.csv), mic (audio.wav), keyboard/mouse (inputs.jsonl),
all on one clock. Stop: faster-whisper transcribes, each thought is matched to its frame range and actions,
and segments.jsonl + sft.jsonl (LLaMA-Factory / Qwen-VL "messages + images" format) are written.

CLI:  recorder.py                     -> UI
      recorder.py --process DIR       -> re-run transcription + dataset build on a recorded session
      recorder.py --selftest DIR [--audio x.wav --expect door,key]
"""
import argparse
import bisect
import csv
import itertools
import json
import os
import queue
import shutil
import sys
import threading
import time
import types
import wave
from pathlib import Path

IS_WIN = sys.platform == "win32"
SR = 16000
LEAD_S = 1.0          # frames from this long before a thought starts belong to it
MERGE_GAP_S = 0.6     # whisper segments closer than this merge into one thought
MAX_THOUGHT_S = 15.0
KEYFRAMES = 4
KEYFRAME_SIDE = 768
WHISPER_MODEL = "distil-large-v3"  # English-only, ~large-v3 accuracy, ~6x faster
PROMPT = "You are playing a video game. Given these frames, say what you see, what you think, and what you do next."
LOG = Path.home() / "MitoRecorder.log"

if sys.stdout is None or sys.stderr is None:  # --windowed exe: no console, send prints to a log file
    sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)

if IS_WIN:  # physical pixels, so window rects, mss and pynput agree on coordinates
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def mss_cls():
    import mss
    return getattr(mss, "MSS", None) or mss.mss  # mss 10 renamed the class


# ---------------------------------------------------------------- pure dataset logic (tested)

def merge_segments(segs, gap=MERGE_GAP_S, max_len=MAX_THOUGHT_S):
    out = []
    for s in segs:
        if not s["text"]:
            continue
        if out and s["start"] - out[-1]["end"] < gap and s["end"] - out[-1]["start"] <= max_len:
            out[-1]["end"] = s["end"]
            out[-1]["text"] += " " + s["text"]
        else:
            out.append({"start": s["start"], "end": s["end"], "text": s["text"]})
    return out


def frame_range(ft, t0, t1):
    """Inclusive frame indices covering [t0, t1]; nearest frame if none falls inside."""
    if not ft:
        return None
    a, b = bisect.bisect_left(ft, t0), bisect.bisect_right(ft, t1) - 1
    if a >= len(ft):
        return len(ft) - 1, len(ft) - 1
    return a, max(a, b)


def pick_frames(a, b, k=KEYFRAMES):
    if b - a + 1 <= k:
        return list(range(a, b + 1))
    return [a + round(i * (b - a) / (k - 1)) for i in range(k)]


def summarize(actions):
    parts = []
    for e in actions:
        k = e["kind"]
        if k == "key_down":
            parts.append(f"press {e['key']}")
        elif k == "mouse_down":
            parts.append(f"{e['button']} click at ({e['x']},{e['y']})")
        elif k == "scroll":
            parts.append("scroll " + ("up" if e["dy"] > 0 else "down" if e["dy"] < 0 else "sideways"))
    if not parts and any(e["kind"] == "move" for e in actions):
        parts.append("move mouse")
    out = []
    for p, g in itertools.groupby(parts):
        n = len(list(g))
        out.append(f"{p} x{n}" if n > 1 else p)
    return ", ".join(out) or "no input"


def build_dataset(thoughts, ft, inputs, region, lead=LEAD_S):
    """thoughts: [{start,end,text}] in session seconds. Coordinates become 0-1000 relative to the capture region."""
    ox, oy, w, h = region["left"], region["top"], region["width"], region["height"]
    items = []
    for th in thoughts:
        t0, t1 = max(0.0, th["start"] - lead), th["end"]
        fr = frame_range(ft, t0, t1)
        if fr is None:
            continue
        acts = []
        for e in inputs:
            if t0 <= e["t"] <= t1:
                e = dict(e)
                if "x" in e:
                    e["x"] = round((e["x"] - ox) * 1000 / w)
                    e["y"] = round((e["y"] - oy) * 1000 / h)
                acts.append(e)
        items.append({"id": len(items) + 1, "t0": round(t0, 3), "t1": round(t1, 3),
                      "frame_start": fr[0], "frame_end": fr[1], "thought": th["text"],
                      "action_summary": summarize(acts), "actions": acts,
                      "frames": pick_frames(*fr)})
    return items


def sft_record(it):
    imgs = it["keyframes"]
    return {"messages": [{"role": "user", "content": "<image>" * len(imgs) + PROMPT},
                         {"role": "assistant", "content": f"Thought: {it['thought']}\nAction: {it['action_summary']}"}],
            "images": imgs}


# ---------------------------------------------------------------- capture targets

def list_monitors():
    with mss_cls()() as s:
        mons = s.monitors
    return [(f"Monitor {i} ({m['width']}x{m['height']})", (i, m)) for i, m in enumerate(mons) if i] + \
           [(f"All monitors ({mons[0]['width']}x{mons[0]['height']})", (0, mons[0]))]


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
    if not IS_WIN:
        return []  # ponytail: window mode is Windows-only; Linux records screens
    from ctypes import wintypes
    u, out = ctypes.windll.user32, []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):
        if u.IsWindowVisible(h) and u.GetWindowTextLengthW(h) and not is_cloaked(h):
            buf = ctypes.create_unicode_buffer(512)
            u.GetWindowTextW(h, buf, 512)
            r = window_rect(h)
            if r and r["width"] > 64 and r["height"] > 64 and buf.value != "Mito Recorder":
                out.append((f"{buf.value[:70]} ({r['width']}x{r['height']})", h))
        return True

    u.EnumWindows(cb, 0)
    return out


def list_mics():
    import sounddevice as sd
    default = sd.default.device[0]
    api = sd.query_devices(default)["hostapi"] if default is not None and default >= 0 else None
    return [(d["name"], i) for i, d in enumerate(sd.query_devices())
            if d["max_input_channels"] > 0 and (api is None or d["hostapi"] == api)]


def key_name(key):
    ch = getattr(key, "char", None)
    if ch and ch.isprintable():
        return ch.lower()
    return getattr(key, "name", None) or f"vk{getattr(key, 'vk', '?')}"


# ---------------------------------------------------------------- recorder

class Recorder:
    def __init__(self, out, fps, region_fn, target=None, mic=None, audio=True, inputs=True):
        """target: ("window", hwnd) | ("monitor", 1-based index) for Windows Graphics Capture; None = GDI/mss."""
        self.out, self.fps, self.region_fn, self.mic = Path(out), fps, region_fn, mic
        self.target, self.backend = target, None
        self.audio, self.inputs_on = audio, inputs
        self.stop_ev = threading.Event()
        self.q = queue.Queue(maxsize=90)  # ~3 s of frames; full => drop, never stall capture
        self.aq = queue.Queue()
        self.frames = self.dropped = 0
        self.inputs, self.down, self.listeners, self.threads = [], set(), [], []
        self.audio_t0, self.error, self._last_move = None, None, (-1.0, 0, 0)

    def t(self):
        return time.perf_counter() - self.t0

    def start(self):
        r = self.region_fn()
        if not r or r["width"] < 2 or r["height"] < 2:
            raise RuntimeError("capture target is minimised or gone")
        r = dict(r, width=r["width"] - r["width"] % 2, height=r["height"] - r["height"] % 2)  # H.264 needs even size
        self.region = {k: r[k] for k in ("left", "top", "width", "height")}
        self.out.mkdir(parents=True, exist_ok=True)
        self.started = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.t0 = time.perf_counter()
        workers = [self._capture, self._encode] + ([self._audio] if self.audio else [])
        self.threads = [threading.Thread(target=f, daemon=True) for f in workers]
        for th in self.threads:
            th.start()
        if self.inputs_on:
            from pynput import keyboard, mouse
            self.listeners = [keyboard.Listener(on_press=self._press, on_release=self._release),
                              mouse.Listener(on_move=self._move, on_click=self._click, on_scroll=self._scroll)]
            for li in self.listeners:
                li.start()
        return self

    def stop(self):
        self.stop_ev.set()
        for li in self.listeners:
            li.stop()
        for th in self.threads:
            th.join()
        dur = self.t()
        with open(self.out / "inputs.jsonl", "w", encoding="utf-8") as f:
            f.writelines(json.dumps(e) + "\n" for e in self.inputs)
        meta = {"started": self.started, "duration_s": round(dur, 3), "fps_requested": self.fps,
                "fps_actual": round(self.frames / dur, 2) if dur else 0, "frames": self.frames,
                "dropped": self.dropped, "region": self.region, "audio_t0": self.audio_t0,
                "capture": self.backend,
                "coords": "inputs.jsonl: absolute screen px; segments/sft: 0-1000 relative to region",
                "error": self.error}
        (self.out / "meta.json").write_text(json.dumps(meta, indent=1))
        return meta

    # threads
    def _wgc(self):
        """Windows Graphics Capture: any window (DirectX/OpenGL/Vulkan, covered by other windows) or monitor."""
        sys.modules.setdefault("cv2", types.ModuleType("cv2"))  # cv2 only backs save_as_image; skip ~40 MB
        from windows_capture import WindowsCapture
        kind, val = self.target
        latest, got = [None], threading.Event()
        for border in (False, None):  # border toggle unsupported on older Windows 10
            try:
                cap = WindowsCapture(cursor_capture=True, draw_border=border,
                                     minimum_update_interval=max(1, int(500 / self.fps)),
                                     **({"window_hwnd": val} if kind == "window" else {"monitor_index": val}))

                @cap.event
                def on_frame_arrived(frame, _ctl):
                    latest[0] = (frame.frame_buffer.tobytes(), (frame.width, frame.height))
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
            return shot.bgra, shot.size
        return grab, sct.close

    def _capture(self):
        period, close = 1 / self.fps, None
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
            nxt = time.perf_counter()
            while not self.stop_ev.is_set():
                now = time.perf_counter()
                if now < nxt:
                    time.sleep(min(nxt - now, 0.05))
                    continue
                nxt = nxt + period if nxt + period > now else now + period  # behind => skip, don't burst
                try:
                    buf, size = grab()  # WGC: latest frame; unchanged screen repeats it (constant fps)
                    self.q.put_nowait((self.t(), buf, size))
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
        import numpy as np
        w, h = self.region["width"], self.region["height"]
        with av.open(str(self.out / "video.mp4"), "w") as c, \
                open(self.out / "frames.csv", "w", newline="") as f:
            try:
                st = c.add_stream("libx264", rate=self.fps, options={"preset": "ultrafast", "crf": "23"})
            except Exception:
                st = c.add_stream("mpeg4", rate=self.fps)
                st.bit_rate = 8_000_000
            st.width, st.height, st.pix_fmt = w, h, "yuv420p"
            wr = csv.writer(f)
            wr.writerow(["frame", "t"])
            while (item := self.q.get()) is not None:
                t, buf, (fw, fh) = item
                fr = av.VideoFrame.from_ndarray(np.frombuffer(buf, np.uint8).reshape(fh, fw, 4), format="bgra")
                if (fw, fh) != (w, h):  # window resized: scale to the fixed video size
                    fr = fr.reformat(width=w, height=h)
                fr.pts = self.frames
                c.mux(st.encode(fr))
                wr.writerow([self.frames, f"{t:.4f}"])
                self.frames += 1
            c.mux(st.encode())

    def _audio(self):
        try:
            import sounddevice as sd
            sr = SR
            try:
                sd.check_input_settings(device=self.mic, samplerate=SR, channels=1, dtype="int16")
            except Exception:
                sr = int(sd.query_devices(self.mic, "input")["default_samplerate"])

            def cb(data, frames, _ti, _status):
                if self.audio_t0 is None:
                    self.audio_t0 = round(self.t() - frames / sr, 4)
                self.aq.put(bytes(data))

            with wave.open(str(self.out / "audio.wav"), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                with sd.RawInputStream(samplerate=sr, channels=1, dtype="int16", device=self.mic, callback=cb):
                    while not self.stop_ev.is_set():
                        try:
                            wf.writeframes(self.aq.get(timeout=0.2))
                        except queue.Empty:
                            pass
                while not self.aq.empty():
                    wf.writeframes(self.aq.get())
        except Exception as e:
            self.error = f"audio: {e}"
            log(self.error)

    # input callbacks (pynput threads)
    def _ev(self, kind, **kw):
        self.inputs.append({"t": round(self.t(), 4), "kind": kind, **kw})

    def _press(self, key):
        n = key_name(key)
        if n != "f9" and n not in self.down:  # F9 = app hotkey; ignore OS auto-repeat
            self.down.add(n)
            self._ev("key_down", key=n)

    def _release(self, key):
        n = key_name(key)
        if n in self.down:
            self.down.discard(n)
            self._ev("key_up", key=n)

    def _move(self, x, y):
        # ponytail: cursor-based; games that lock the cursor hide raw deltas. Upgrade: Win32 Raw Input.
        t, lx, ly = self._last_move
        now = self.t()
        if now - t >= 1 / 60:
            self._last_move = (now, x, y)
            self._ev("move", x=x, y=y, dx=x - lx if t >= 0 else 0, dy=y - ly if t >= 0 else 0)

    def _click(self, x, y, button, pressed):
        self._ev("mouse_down" if pressed else "mouse_up", x=x, y=y, button=button.name)

    def _scroll(self, x, y, dx, dy):
        self._ev("scroll", x=x, y=y, dx=dx, dy=dy)


# ---------------------------------------------------------------- post-processing

def transcribe(wav):
    if not wav.exists() or wav.stat().st_size <= 44:
        return []
    import ctranslate2
    from faster_whisper import WhisperModel
    plans = ([("cuda", "int8_float16")] if ctranslate2.get_cuda_device_count() else []) + [("cpu", "int8")]
    for dev, ct in plans:
        try:
            log(f"Whisper {WHISPER_MODEL} on {dev} (first run downloads the model)...")
            m = WhisperModel(WHISPER_MODEL, device=dev, compute_type=ct)
            segs, _ = m.transcribe(str(wav), language="en", vad_filter=True, word_timestamps=True,
                                   beam_size=5, condition_on_previous_text=False)
            return [{"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(),
                     "words": [[w.word.strip(), round(w.start, 3), round(w.end, 3)] for w in s.words or []]}
                    for s in segs]
        except Exception as e:
            if dev == "cpu":
                raise
            log(f"GPU whisper failed ({e}); falling back to CPU")


def extract_keyframes(d, items):
    import av
    need = {}
    for it in items:
        it["keyframes"] = []
        for i in it.pop("frames"):
            need[i] = f"keyframes/{i:06d}.jpg"
            it["keyframes"].append(need[i])
    if not need:
        return
    (d / "keyframes").mkdir(exist_ok=True)
    last = max(need)
    with av.open(str(d / "video.mp4")) as c:
        for i, fr in enumerate(c.decode(video=0)):
            if i in need:
                im = fr.to_image()
                im.thumbnail((KEYFRAME_SIDE, KEYFRAME_SIDE))
                im.save(d / need[i], quality=90)
            if i >= last:
                break


def process_session(d):
    d = Path(d)
    meta = json.loads((d / "meta.json").read_text())
    with open(d / "frames.csv", newline="") as f:
        ft = [float(r["t"]) for r in csv.DictReader(f)]
    inp = d / "inputs.jsonl"
    inputs = [json.loads(line) for line in open(inp, encoding="utf-8")] if inp.exists() else []
    off = meta.get("audio_t0") or 0.0
    segs = transcribe(d / "audio.wav")
    for s in segs:  # audio time -> session time
        s["start"], s["end"] = round(s["start"] + off, 3), round(s["end"] + off, 3)
        s["words"] = [[w, round(a + off, 3), round(b + off, 3)] for w, a, b in s["words"]]
    with open(d / "transcript.jsonl", "w", encoding="utf-8") as f:
        f.writelines(json.dumps(s) + "\n" for s in segs)
    items = build_dataset(merge_segments(segs), ft, inputs, meta["region"])
    log(f"{len(segs)} speech segments -> {len(items)} labelled thoughts; extracting keyframes...")
    extract_keyframes(d, items)
    with open(d / "segments.jsonl", "w", encoding="utf-8") as f:
        f.writelines(json.dumps(it) + "\n" for it in items)
    with open(d / "sft.jsonl", "w", encoding="utf-8") as f:
        f.writelines(json.dumps(sft_record(it)) + "\n" for it in items)
    meta.update(whisper_model=WHISPER_MODEL, thoughts=len(items), lead_s=LEAD_S, prompt=PROMPT)
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    log(f"done: {d}")
    return items


# ---------------------------------------------------------------- UI

class App:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.root = root = tk.Tk()
        root.title("Mito Recorder")
        root.resizable(False, False)
        self.fps, self.src = tk.IntVar(value=30), tk.StringVar(value="screen")
        self.out = tk.StringVar(value=str(Path.home() / "MitoData"))
        self.name = tk.StringVar(value=self._new_name())
        self.status = tk.StringVar(value="Ready. F9 = start/stop. Think aloud in English.")
        self.rec, self.busy, self.toggle_req, self.msg = None, False, False, None
        f = ttk.Frame(root, padding=12)
        f.grid()
        row = itertools.count()

        def line(label, *widgets):
            r = next(row)
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3)
            for c, w in enumerate(widgets, 1):
                w.grid(row=r, column=c, sticky="we", padx=3)

        line("FPS", ttk.Spinbox(f, from_=1, to=60, textvariable=self.fps, width=6))
        srcf = ttk.Frame(f)
        for txt, val in (("Screen", "screen"), ("Window", "window")):
            ttk.Radiobutton(srcf, text=txt, value=val, variable=self.src, command=self.refresh).pack(side="left")
        line("Record", srcf)
        self.target = ttk.Combobox(f, state="readonly", width=55)
        line("Target", self.target, ttk.Button(f, text="Refresh", command=self.refresh))
        self.mic = ttk.Combobox(f, state="readonly", width=55)
        line("Microphone", self.mic)
        line("Save to", ttk.Entry(f, textvariable=self.out, width=58), ttk.Button(f, text="Browse", command=self.browse))
        line("Name", ttk.Entry(f, textvariable=self.name, width=58))
        self.btn = ttk.Button(f, text="Start (F9)", command=self.toggle)
        self.btn.grid(row=next(row), column=0, columnspan=3, sticky="we", pady=8)
        ttk.Label(f, textvariable=self.status, wraplength=520).grid(row=next(row), column=0, columnspan=3, sticky="w")
        self.refresh()
        try:
            self.mics = list_mics()
            self.mic["values"] = [n for n, _ in self.mics]
            if self.mics:
                self.mic.current(0)
        except Exception as e:
            self.mics = []
            self.status.set(f"No microphone backend: {e}")
        try:
            from pynput import keyboard
            keyboard.Listener(on_press=lambda k: k == keyboard.Key.f9 and setattr(self, "toggle_req", True),
                              daemon=True).start()
        except Exception as e:
            log(f"hotkey unavailable: {e}")
        root.after(200, self.tick)

    @staticmethod
    def _new_name():
        return time.strftime("session_%Y%m%d_%H%M%S")

    def browse(self):
        from tkinter import filedialog
        p = filedialog.askdirectory(initialdir=self.out.get())
        if p:
            self.out.set(p)

    def refresh(self):
        self.targets = list_monitors() if self.src.get() == "screen" else list_windows()
        self.target["values"] = [n for n, _ in self.targets]
        if self.targets:
            self.target.current(0)
        else:
            self.target.set("")

    def toggle(self):
        from tkinter import messagebox
        if self.busy:
            return
        if self.rec:
            rec, self.rec = self.rec, None
            meta = rec.stop()
            self.btn.config(text="Processing...", state="disabled")
            self.busy = True
            self.msg = f"Saved {meta['frames']} frames ({meta['fps_actual']} fps). Transcribing..."
            threading.Thread(target=self._process, args=(rec.out,), daemon=True).start()
            return
        i = self.target.current()
        if i < 0:
            messagebox.showerror("Mito Recorder", "Pick a screen or window first.")
            return
        out = Path(self.out.get()) / self.name.get().strip()
        if out.exists() and any(out.iterdir()):
            messagebox.showerror("Mito Recorder", f"{out} already exists. Choose another name.")
            return
        tgt = self.targets[i][1]
        if self.src.get() == "screen":
            region_fn, target = (lambda: tgt[1]), ("monitor", tgt[0])
        else:
            region_fn, target = (lambda: window_rect(tgt)), ("window", tgt)
        mic = self.mics[self.mic.current()][1] if self.mics and self.mic.current() >= 0 else None
        try:
            fps = max(1, min(60, int(self.fps.get())))
            self.rec = Recorder(out, fps, region_fn, target=target, mic=mic, audio=bool(self.mics)).start()
        except Exception as e:
            messagebox.showerror("Mito Recorder", str(e))
            return
        self.btn.config(text="Stop (F9)")

    def _process(self, out):
        try:
            items = process_session(out)
            self.msg = f"Done: {len(items)} labelled thoughts in {out}"
        except Exception as e:
            log(f"processing failed: {e!r}")
            self.msg = f"Processing failed: {e}. Retry later: MitoRecorder.exe --process \"{out}\""
        self.busy = False

    def tick(self):
        if self.toggle_req:
            self.toggle_req = False
            self.toggle()
        if self.msg:
            self.status.set(self.msg)
            self.msg = None
        if self.rec:
            r, t = self.rec, self.rec.t()
            self.status.set(f"REC {int(t // 60):02d}:{int(t % 60):02d}  frames {r.frames}  "
                            f"({r.frames / max(t, 1e-6):.1f} fps)  dropped {r.dropped}" +
                            (f"  ! {r.error}" if r.error else ""))
        elif not self.busy and str(self.btn["state"]) == "disabled":
            self.btn.config(text="Start (F9)", state="normal")
            self.name.set(self._new_name())
        self.root.after(200, self.tick)


# ---------------------------------------------------------------- selftest / main

def selftest(a):
    out = Path(a.selftest)
    if out.exists():
        shutil.rmtree(out)
    log(f"platform={sys.platform} windows={len(list_windows())}")
    idx, mon = list_monitors()[0][1]
    rec = Recorder(out, 10, lambda: mon, target=("monitor", idx), audio=False, inputs=False).start()
    time.sleep(a.seconds)
    meta = rec.stop()
    log(f"recorded {meta}")
    assert meta["frames"] > 0 and not meta["error"], meta
    if a.audio:
        shutil.copy(a.audio, out / "audio.wav")
        m = json.loads((out / "meta.json").read_text())
        m["audio_t0"] = 0.0
        (out / "meta.json").write_text(json.dumps(m))
    items = process_session(out)
    if a.expect:
        text = " ".join(it["thought"] for it in items).lower()
        log(f"transcript: {text}")
        missing = [w for w in a.expect.split(",") if w not in text]
        assert not missing, f"missing words {missing}"
        recs = [json.loads(line) for line in open(out / "sft.jsonl")]
        assert recs and all((out / p).exists() for r in recs for p in r["images"]), "sft images missing"
    log("SELFTEST OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--process", metavar="DIR")
    ap.add_argument("--selftest", metavar="DIR")
    ap.add_argument("--audio")
    ap.add_argument("--expect")
    ap.add_argument("--seconds", type=float, default=3)
    a = ap.parse_args()
    try:
        if a.process:
            process_session(a.process)
        elif a.selftest:
            selftest(a)
        else:
            App().root.mainloop()
    except Exception as e:
        log(f"FAILED: {e!r}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
