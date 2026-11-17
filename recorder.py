"""Mito Recorder: record gameplay as training data for a game-playing AI (VLA).

Each run becomes one zip in the save folder, named <Game>_<quality>_<duration>.zip (e.g. Minecraft_light_1h_2m_7s.zip), holding
  video.mp4        H.264 (x264 veryfast, CRF 20), scaled down to at most 720p, constant FPS (24, 30 or 36; default 30)
  frames.csv       frame, t (grab time), src_t (source frame arrival), fresh (0 = repeat of the previous frame)
  profile.json     light/good profile, 720p, constant 30 fps, real vs repeated frames, duration
  inputs.jsonl     keyboard + mouse from Windows Raw Input, logged only while the game window is focused,
                   plus focus / cursor-lock / window-move events
  game_audio.ogg   the game's own sound (per-process loopback; all PC sound if Windows refuses), Opus 96 kbps,
                   on the session clock
  meta.json        game, sizes, timing, system (pointer speed, refresh rate), user (DPI, sensitivity),
                   quality numbers + warnings
Everything shares one clock: session time 0 = Start. Thoughts / dataset labelling happen later, elsewhere.

CLI:  recorder.py                  -> UI
      recorder.py --selftest DIR   -> record the screen a few seconds with injected input, zip, verify
      recorder.py --updatetest DIR -> (exe only) update a copy of itself from the latest GitHub release, verify
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
import struct
import subprocess
import sys
import threading
import time
import types
import webbrowser
import zipfile
import zlib
from fractions import Fraction
from pathlib import Path

import numpy as np

VERSION = "2.9"
REPO = "Kem0wow/mito-recorder"  # public; each v* tag gets a release with MitoRecorder.exe (CI)
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
DRIVE_FOLDER_ID = "1g-eHWyhihvk86EOgC-639riVl9WZGBXo"
UPLOAD_URL = f"https://drive.google.com/drive/folders/{DRIVE_FOLDER_ID}?usp=sharing"
try:  # drive_key.py is not in git: CI writes it from a secret before building the exe
    from drive_key import DRIVE_API_KEY
except ImportError:
    DRIVE_API_KEY = ""
STR = {
"en": {
 "tab_record": "Record", "tab_instr": "Instructions", "tab_data": "Data", "tab_update": "Update",
 "tab_update_new": "Update (new!)", "this_version": "This version: v{v}", "check_update": "Check for updates",
 "update_now": "Update now", "upd_checking": "Checking GitHub...", "upd_latest": "You have the latest version.",
 "upd_new": "New version v{v} is available. Press Update now: the app downloads it, checks it and restarts.",
 "upd_downloading": "Downloading v{v}...", "upd_restarting": "Updated. Restarting...",
 "upd_recording": "Stop the recording first.", "upd_notexe": "Only the .exe can update itself.",
 "upd_err": "Update check failed: {err}", "upd_notes": "What's new:",
 "game_window": "Game window", "refresh": "Refresh", "game_name": "Game name", "fps": "FPS",
 "fps_hint": "36 needs a stronger PC; if 'Low FPS' shows, pick 24 or 30",
 "dpi": "Mouse DPI (optional)", "sens": "Game sensitivity (optional)", "quality": "Video quality",
 "save_to": "Save to", "browse": "Browse", "start": "Start (F9)", "stop": "Stop (F9)", "saving": "Saving...",
 "idle": "Pick the game window, then press Start (or F9 in game).",
 "upload": "Upload recordings (Google Drive)", "open_folder": "Open my recordings folder",
 "fix_header": "PROBLEMS AND FIXES (click the warning on the Record tab to jump here)",
 "update_data": "Update data", "data_reading": "Reading the Drive folder...",
 "data_nokey": "Data needs a Google API key: set DRIVE_API_KEY in recorder.py (see README).",
 "data_summary": "{n} recording(s), {g} game(s).", "data_none": "None", "col_game": "Game", "col_total": "Recorded",
 "col_runs": "Recordings", "hms": "{h} h {m} min {s} s",
 "no_internet": "no internet connection",
 "drive_http": "Drive refused the request (HTTP {code}). Check DRIVE_API_KEY and that the folder is shared.",
 "data_err": "Could not read the Drive folder: {err}",
 "rec_line": "REC {mm}:{ss}  {name}  {fps} fps  dropped {d}",
 "not_focused": "  (game not focused: inputs paused)", "err": "  ! {err}",
 "finishing": "Finishing video and zipping...",
 "saved": "Saved {name}: {minutes:.1f} min, {fps} fps, {gb:.2f} GB.",
 "quality_ok": "Quality OK.", "warnings": "WARNINGS: {w}",
 "next_upload": "Next: upload the zip to the Drive folder (button above).",
 "save_failed": "Save failed: {err}. Raw files are kept in {out}",
 "pick_first": "Pick the game window first.", "banner_tail": "(click for the fix)",
 "short_black": "Black video", "short_fps": "Low FPS", "short_dropped": "Dropped frames",
 "short_focus": "Game not focused", "short_sound": "No game sound", "short_input": "No input",
 "short_disk": "Low disk",
 "fix_black": "BLACK VIDEO: the game is in exclusive fullscreen. Switch it to windowed or borderless, then record again.",
 "fix_fps": "LOW FPS: lower the game's graphics or resolution and close other programs.",
 "fix_dropped": "FRAMES DROPPED: the PC is overloaded. Close heavy programs and lower the game's graphics.",
 "fix_focus": "GAME NOT FOCUSED: click the game window. Alt-tab stops recording inputs.",
 "fix_sound": "NO GAME SOUND: check the game is not muted and its sound is on. Voice chat in other apps is not recorded.",
 "fix_input": "NO KEYBOARD OR MOUSE: play normally. Inputs only count while the game window is focused.",
 "fix_disk": "LOW DISK SPACE: free space on the drive or choose another save folder.",
 "consent": ("Records only the chosen game window, that game's own sound, and your keyboard and mouse while the "
             "game is focused. The data is used to train a game-playing AI."),
 "howto": ("HOW TO RECORD\n"
           "1. Start your game in windowed or borderless mode (not exclusive fullscreen).\n"
           "2. Pick the game window above and check the game name.\n"
           "3. Press Start, or F9 while in the game. Play. Press F9 again to stop. The zip is saved for you.\n"
           "4. Click \"Upload recordings\" and drop the .zip files (do not unzip them) into the Drive folder."),
 "tips": ("TIPS FOR GOOD DATA\n"
          "- Play seriously and normally, like you want to win or finish a level. Do not idle, pause or AFK.\n"
          "- Sessions of 10-60 minutes are ideal. Different games and different situations help more than one long run.\n"
          "- Keep the game window focused. When you alt-tab, nothing is recorded.\n"
          "- Only the game's own sound is recorded. Voice chat and music in other programs are not. If the result "
          "says game-only sound is unavailable, all PC sound was recorded: mute voice chat and music first.\n"
          "- Close heavy programs. If the result says low fps, lower the game's graphics or resolution.\n"
          "- Use your normal mouse settings. If you know your mouse DPI and the game's sensitivity, enter them.\n"
          "- After you stop, read the result line. \"Quality OK\" is good. If it shows a warning (black video, "
          "no sound, low fps), fix it and record again.")
},
"tr": {
 "tab_record": "Kayıt", "tab_instr": "Talimatlar", "tab_data": "Veri", "tab_update": "Güncelleme",
 "tab_update_new": "Güncelleme (yeni!)", "this_version": "Bu sürüm: v{v}", "check_update": "Güncellemeleri kontrol et",
 "update_now": "Şimdi güncelle", "upd_checking": "GitHub kontrol ediliyor...", "upd_latest": "En son sürümü kullanıyorsun.",
 "upd_new": "Yeni sürüm v{v} hazır. Şimdi güncelle'ye bas: uygulama indirir, kontrol eder ve yeniden açılır.",
 "upd_downloading": "v{v} indiriliyor...", "upd_restarting": "Güncellendi. Yeniden açılıyor...",
 "upd_recording": "Önce kaydı durdur.", "upd_notexe": "Sadece .exe kendini güncelleyebilir.",
 "upd_err": "Güncelleme kontrolü başarısız: {err}", "upd_notes": "Yenilikler:",
 "game_window": "Oyun penceresi", "refresh": "Yenile", "game_name": "Oyun adı", "fps": "FPS",
 "fps_hint": "36 daha güçlü PC ister; 'Düşük FPS' çıkarsa 24 veya 30 seç",
 "dpi": "Mouse DPI (isteğe bağlı)", "sens": "Oyun hassasiyeti (isteğe bağlı)", "quality": "Video kalitesi",
 "save_to": "Kayıt klasörü", "browse": "Gözat", "start": "Başlat (F9)", "stop": "Durdur (F9)",
 "saving": "Kaydediliyor...",
 "idle": "Oyun penceresini seç, sonra Başlat'a bas (ya da oyunda F9).",
 "upload": "Kayıtları yükle (Google Drive)", "open_folder": "Kayıt klasörümü aç",
 "fix_header": "SORUNLAR VE ÇÖZÜMLER (Kayıt sekmesindeki uyarıya tıklarsan buraya gelirsin)",
 "update_data": "Veriyi güncelle", "data_reading": "Drive klasörü okunuyor...",
 "data_nokey": "Veri için Google API anahtarı gerekli: recorder.py içinde DRIVE_API_KEY ayarla (README'ye bak).",
 "data_summary": "{n} kayıt, {g} oyun.", "data_none": "Yok", "col_game": "Oyun", "col_total": "Toplam süre",
 "col_runs": "Kayıt sayısı", "hms": "{h} sa {m} dk {s} sn",
 "no_internet": "internet bağlantısı yok",
 "drive_http": "Drive isteği reddetti (HTTP {code}). DRIVE_API_KEY'i ve klasör paylaşımını kontrol et.",
 "data_err": "Drive klasörü okunamadı: {err}",
 "rec_line": "KAYIT {mm}:{ss}  {name}  {fps} fps  kare kaybı {d}",
 "not_focused": "  (oyun odakta değil: girdiler duraklatıldı)", "err": "  ! {err}",
 "finishing": "Video bitiriliyor ve zipleniyor...",
 "saved": "{name} kaydedildi: {minutes:.1f} dk, {fps} fps, {gb:.2f} GB.",
 "quality_ok": "Kalite tamam.", "warnings": "UYARILAR: {w}",
 "next_upload": "Sonraki adım: zip'i Drive klasörüne yükle (yukarıdaki buton).",
 "save_failed": "Kaydetme başarısız: {err}. Ham dosyalar burada: {out}",
 "pick_first": "Önce oyun penceresini seç.", "banner_tail": "(düzeltmek için tıkla)",
 "short_black": "Siyah video", "short_fps": "Düşük FPS", "short_dropped": "Kare kaybı",
 "short_focus": "Oyun odakta değil", "short_sound": "Oyun sesi yok", "short_input": "Girdi yok",
 "short_disk": "Düşük disk",
 "fix_black": "SİYAH VİDEO: oyun tam ekran modda. Pencere ya da kenarsız pencere moduna geçir, sonra tekrar kaydet.",
 "fix_fps": "DÜŞÜK FPS: oyunun grafiklerini ya da çözünürlüğünü düşür, diğer programları kapat.",
 "fix_dropped": "KARE KAYBI: PC aşırı yüklü. Ağır programları kapat, oyunun grafiklerini düşür.",
 "fix_focus": "OYUN ODAKTA DEĞİL: oyun penceresine tıkla. Alt-tab girdi kaydını durdurur.",
 "fix_sound": "OYUN SESİ YOK: oyunun sessize alınmadığından ve sesinin açık olduğundan emin ol. Diğer uygulamalardaki sesli sohbet kaydedilmez.",
 "fix_input": "KLAVYE VEYA FARE YOK: normal oyna. Girdiler sadece oyun odaktayken sayılır.",
 "fix_disk": "DÜŞÜK DİSK ALANI: sürücüde yer aç ya da başka bir kayıt klasörü seç.",
 "consent": ("Yalnızca seçilen oyun penceresini, o oyunun kendi sesini ve oyun odaktayken klavye ve farenizi kaydeder. "
             "Veri, oyun oynayan bir yapay zekâyı eğitmek için kullanılır."),
 "howto": ("NASIL KAYIT YAPILIR\n"
           "1. Oyununu pencere veya kenarsız pencere modunda aç (tam ekran değil).\n"
           "2. Yukarıdan oyun penceresini seç ve oyun adını kontrol et.\n"
           "3. Başlat'a bas ya da oyunda F9. Oyna. Durdurmak için tekrar F9. Zip otomatik kaydedilir.\n"
           "4. \"Kayıtları yükle\" butonuna tıkla ve .zip dosyalarını (açmadan) Drive klasörüne bırak."),
 "tips": ("VERİ İÇİN İPUÇLARI\n"
          "- Ciddi ve normal oyna, bir seviyeyi bitirmek ister gibi. Boşta bekleme, duraklatma, AFK yapma.\n"
          "- 10-60 dakikalık oturumlar idealdir. Farklı oyunlar ve farklı durumlar, tek uzun kayıttan daha faydalı.\n"
          "- Oyun penceresini odakta tut. Alt-tab yaparsan hiçbir şey kaydedilmez.\n"
          "- Sadece oyunun kendi sesi kaydedilir. Diğer programlardaki sesli sohbet ve müzik kaydedilmez. Oyun sesi "
          "kullanılamazsa tüm PC sesi kaydedilir ve sonuçta yazar: önce sesli sohbeti ve müziği kapat.\n"
          "- Ağır programları kapat. Sonuçta düşük fps yazarsa oyunun grafiklerini ya da çözünürlüğünü düşür.\n"
          "- Normal fare ayarlarını kullan. Fare DPI'ını ve oyun hassasiyetini biliyorsan gir.\n"
          "- Durdurunca sonuç satırını oku. \"Kalite tamam\" iyi. Uyarı varsa (siyah video, ses yok, düşük fps) "
          "düzelt ve tekrar kaydet.")
}
}
FIX_KEYS = ("black", "fps", "dropped", "focus", "sound", "input", "disk")


def tr(key, lang="en", **kw):
    s = STR.get(lang, STR["en"]).get(key) or STR["en"][key]
    return s.format(**kw) if kw else s


IS_WIN = sys.platform == "win32"
FPS_CHOICES = (24, 30, 36)  # Record-tab combobox; the pipeline samples by session time t, never by frame index
FPS_DEFAULT = 30
FPS = FPS_DEFAULT  # selftest / headless default
MIN_FREE = 3e9   # refuse to start with less free disk than this
LOW_FREE = 5e8   # stop (and save) a running session below this
MAX_H = 720
# Encode profiles (picked on the Record tab). Good = fine detail for labels, heavier on the CPU.
X264_PROFILES = {"Good quality (heavy CPU)": {"preset": "veryfast", "crf": "20"},
                 "Light (less CPU, bigger files)": {"preset": "ultrafast", "crf": "20"}}  # same CRF as Good: quality kept for training, CPU saved by the preset
X264_DEFAULT = "Good quality (heavy CPU)"


def quality_tag(x264):
    """x264 options -> 'light' or 'good', for the zip name (training keeps light runs out of focus/OCR labels)."""
    return "light" if x264 == X264_PROFILES["Light (less CPU, bigger files)"] else "good"
AUDIO_BPS = 96_000
LOG = Path.home() / "MitoRecorder.log"
if sys.stdout is None or sys.stderr is None:  # --windowed exe: no console, send prints to a log file
    sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)

if IS_WIN:  # physical pixels, so window rects, capture and cursor positions agree
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()
    ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p


def resource(name):  # next to recorder.py, or unpacked inside the exe (PyInstaller)
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / name


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def set_priority(level):
    """Priority of the calling thread (Windows): +1 above normal for input/capture/audio (their timestamps must not
    starve while x264 loads the CPU), -1 below normal for scaling/encoding (never fight the game for CPU)."""
    if not IS_WIN:
        return
    try:
        k = ctypes.windll.kernel32
        k.GetCurrentThread.restype = ctypes.c_void_p
        k.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        k.SetThreadPriority(k.GetCurrentThread(), level)
    except Exception:
        pass


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


def quality(dur, fps, fresh, lags, bright, events, audio_s, silent, dropped, times=None):
    """Health numbers + human warnings for one session.
    fresh: 0/1 per frame (1 = a new source frame); lags: s between source frame arrival and our grab (fresh frames);
    bright: sampled mean pixel values; events: input dicts; audio_s/silent: file seconds, share of silent chunks;
    times: session time of every really captured frame (for the interval jitter)."""
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
    if times and len(times) > 2:  # |capture interval - 1/fps|: how far the grabs wobble around the 30 fps grid
        dev = sorted(abs(b - a - 1 / fps) * 1000 for a, b in zip(times, times[1:]))
        q["jitter_ms"] = {"p50": round(dev[len(dev) // 2], 2), "p95": round(dev[int(0.95 * (len(dev) - 1))], 2),
                          "max": round(dev[-1], 2)}
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
    # flagged for a human look, never deleted: bad video timing, many drops, black picture or a very short session
    q["needs_review"] = bool(
        (dur and q["fps"] < 0.9 * fps) or dropped > 0.01 * max(n, 1) or (q["black_share"] or 0) > 0.5
        or (q.get("jitter_ms") and q["jitter_ms"]["p95"] > 200 / fps) or 0 < dur < 5)
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


def duration_tag(seconds):
    """1h 26m 38s -> '1h_26m_38s'; 26m 38s -> '26m_38s'; 42 s -> '42s'."""
    s = int(round(seconds))
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return "_".join(([f"{h}h"] if h else []) + ([f"{m}m"] if h or m else []) + [f"{s}s"])


def free_zip_name(root, game, tag):
    """<game>_<tag>, or <game>_<tag>_2, _3 ... if that zip already exists in root."""
    name, k = f"{game}_{tag}", 1
    while (Path(root) / f"{name}.zip").exists():
        k += 1
        name = f"{game}_{tag}_{k}"
    return name


ZIP_RE = re.compile(r"^(?P<game>.+?)_(?:(?:good|light)_)?(?P<dur>(?:\d+h_)?(?:\d+m_)?\d+s)(?:_\d+)?\.zip$")  # lazy: game may contain _


def parse_duration(tag):
    """'1h_26m_38s' -> 5198 seconds (inverse of duration_tag)."""
    h, m, s = (int(x or 0) for x in re.fullmatch(r"(?:(\d+)h_)?(?:(\d+)m_)?(\d+)s", tag).groups())
    return h * 3600 + m * 60 + s


def summarize(names):
    """Zip names -> (rows, unparsed). rows: [(game, seconds, runs)] most played first; unparsed: old-style names."""
    total, runs, shown, other = {}, {}, {}, []
    for n in names:
        if not n.lower().endswith(".zip"):
            continue
        m = ZIP_RE.match(n)
        if not m:
            other.append(n)
            continue
        key = m["game"].lower()
        shown.setdefault(key, m["game"])
        total[key] = total.get(key, 0) + parse_duration(m["dur"])
        runs[key] = runs.get(key, 0) + 1
    rows = sorted(((shown[k], total[k], runs[k]) for k in total), key=lambda r: -r[1])
    return rows, other


def format_hms(sec, lang="en"):
    h, m, s_ = sec // 3600, sec % 3600 // 60, sec % 60
    return tr("hms", lang, h=h, m=m, s=s_)


def drive_zip_names(api_key, folder_id=DRIVE_FOLDER_ID):
    """Names of all files in the shared Drive folder (Drive API v3, paged)."""
    import urllib.parse
    import urllib.request
    names, token = [], None
    while True:
        q = {"q": f"'{folder_id}' in parents and trashed = false", "fields": "nextPageToken,files(name)",
             "pageSize": 1000, "key": api_key}
        if token:
            q["pageToken"] = token
        url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(q)
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.load(r)
        names += [f["name"] for f in data.get("files", [])]
        token = data.get("nextPageToken")
        if not token:
            return names


# ---------------------------------------------------------------- self-update from GitHub releases

def version_tuple(v):
    """'v2.10' -> (2, 10): numeric compare, so 2.10 > 2.9."""
    return tuple(int(x) for x in re.findall(r"\d+", v))


def is_newer(tag, current=VERSION):
    return version_tuple(tag) > version_tuple(current)


def _gh_request(url):
    import urllib.request
    h = {"User-Agent": "MitoRecorder", "Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):  # CI only: avoids the shared runners' anonymous rate limit
        h["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return urllib.request.Request(url, headers=h)


def latest_release(url=RELEASES_API):
    """Newest release: {tag, notes, url, sha256} of its MitoRecorder.exe. GitHub computes the sha256 itself."""
    import urllib.request
    with urllib.request.urlopen(_gh_request(url), timeout=20) as r:
        d = json.load(r)
    a = next((a for a in d.get("assets", []) if a["name"] == "MitoRecorder.exe"), None)
    if not a:
        raise RuntimeError(f"release {d.get('tag_name')} has no MitoRecorder.exe")
    digest = a.get("digest") or ""
    if not digest.startswith("sha256:"):  # never run a download we cannot check
        raise RuntimeError(f"release {d.get('tag_name')} has no sha256 for MitoRecorder.exe")
    return {"tag": d["tag_name"], "notes": (d.get("body") or "").strip(), "url": a["browser_download_url"],
            "sha256": digest.split(":", 1)[1].lower()}


def download_verified(url, sha256, dest):
    """Download to dest (via dest.part); refuse it unless its sha256 matches."""
    import hashlib
    import urllib.request
    dest, h = Path(dest), hashlib.sha256()
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(_gh_request(url), timeout=60) as r, open(tmp, "wb") as f:
        for chunk in iter(lambda: r.read(1 << 20), b""):
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest() != sha256.lower():
        tmp.unlink()
        raise RuntimeError("downloaded file failed the sha256 check")
    tmp.replace(dest)
    return dest


def swap_exe(exe, new):
    """exe -> exe.old, new -> exe. Windows lets a running exe be renamed (not deleted); the new one deletes .old."""
    exe, new = Path(exe), Path(new)
    old = exe.with_suffix(".old")
    if old.exists():
        old.unlink()
    exe.replace(old)
    try:
        new.replace(exe)
    except OSError:
        old.replace(exe)  # put the working exe back
        raise
    return old


def fresh_env(**extra):
    """Environment for starting this exe again as its own top-level process. A onefile PyInstaller exe that starts
    the same exe path inherits the parent's temp dir (_MEI...), which is deleted when the parent exits: the new
    process then dies with "No module named '_tkinter'". PYINSTALLER_RESET_ENVIRONMENT makes it extract its own."""
    env = {k: v for k, v in os.environ.items() if k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env.update(extra)
    return env


def apply_update(info, exe=None, relaunch=()):
    """Download + verify + swap the running exe, start the new one. Returns the new process."""
    exe = Path(exe or sys.executable)
    new = download_verified(info["url"], info["sha256"], exe.with_name(exe.stem + ".new.exe"))
    swap_exe(exe, new)
    log(f"updated {exe.name} to {info['tag']}")
    return subprocess.Popen([str(exe), *relaunch], env=fresh_env())


def cleanup_old_exe(wait_s=30.0):
    """After an update only the new exe stays: delete <exe>.old (the old process may need a moment to exit) and a
    half-finished <exe>.new.exe download."""
    if not getattr(sys, "frozen", False):
        return
    exe = Path(sys.executable)
    for junk in (exe.with_name(exe.stem + ".new.exe"), exe.with_name(exe.stem + ".new.exe.part")):
        try:
            junk.unlink()
        except OSError:
            pass
    old = exe.with_suffix(".old")
    end = time.monotonic() + wait_s
    while old.exists():
        try:
            old.unlink()
            log(f"removed {old.name}")
        except OSError:
            if time.monotonic() > end:
                return
            time.sleep(0.25)


def live_issues(m):
    """Problems to show while recording, most severe first. m: live numbers (see App.monitor).
    Returns [(severity 'high' | 'mid', key)]; keys are in FIXES."""
    out = []
    if m["black"]:
        out.append(("high", "black"))
    if m["fps"] < 0.6 * m["target"]:
        out.append(("high", "fps"))
    elif m["fps"] < 0.9 * m["target"]:
        out.append(("mid", "fps"))
    if m["dropped_recent"] > 0:
        out.append(("mid", "dropped"))
    if m["unfocused_s"] > 10:
        out.append(("high", "focus"))
    if m["silent_s"] > 60:
        out.append(("mid", "sound"))
    if m["idle_s"] > 60:
        out.append(("mid", "input"))
    if m["disk_low"]:
        out.append(("high", "disk"))
    return sorted(out, key=lambda x: x[0] != "high")


def fetch_error_text(e, lang="en"):
    """Status text for a failed Drive read."""
    import socket
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        return tr("drive_http", lang, code=e.code)
    if isinstance(e, (urllib.error.URLError, socket.timeout, ConnectionError)):
        return tr("no_internet", lang)
    return tr("data_err", lang, err=f"{type(e).__name__}: {e}")


def zip_session(d, name=None):
    """<dir>/ -> <name or dir name>.zip (written as .part, then renamed), folder removed. Returns the zip path."""
    d = Path(d)
    stem = name or d.name
    z = d.parent / f"{stem}.zip"
    tmp = d.parent / f"{stem}.zip.part"
    # Big already-compressed files (video/audio) are stored, copied in 8 MB reads (zipfile's own write() uses 8 KB
    # reads, which is slow for a multi-hundred-MB mp4). Small text files are deflated at level 1: fast, still small.
    with zipfile.ZipFile(tmp, "w", compresslevel=1) as zf:
        for p in sorted(d.iterdir()):
            zi = zipfile.ZipInfo.from_file(p, p.name)
            zi.compress_type = zipfile.ZIP_STORED if p.suffix in STORED else zipfile.ZIP_DEFLATED
            with open(p, "rb", buffering=0) as src, zf.open(zi, "w", force_zip64=p.stat().st_size > 2**30) as dst:
                while chunk := src.read(8 << 20):
                    dst.write(chunk)
    tmp.replace(z)
    shutil.rmtree(d)
    return z


ZIP_HDR = 512  # bytes reserved at the start of <name>.zip.part for video.mp4's zip local header


class CrcFile:
    """Write-only file for PyAV: counts bytes and CRC32 while video.mp4 streams straight into the zip."""

    def __init__(self, f):
        self.f, self.crc, self.n = f, 0, 0

    def write(self, b):
        self.crc = zlib.crc32(b, self.crc)
        self.n += len(b)
        return self.f.write(b)

    def flush(self):
        self.f.flush()


def _part_crc(part):
    crc, n = 0, 0
    with open(part, "rb") as f:
        f.seek(ZIP_HDR)
        while chunk := f.read(8 << 20):
            crc, n = zlib.crc32(chunk, crc), n + len(chunk)
    return n, crc


def video_entry(size, crc):
    """(ZipInfo, local header bytes) of video.mp4 (at the zip root) stored at offset 0. The header is padded with an unknown
    extra field to exactly ZIP_HDR bytes; zip64 fields are added by zipfile when size >= 4 GiB."""
    zi = zipfile.ZipInfo("video.mp4", time.localtime()[:6])
    zi.compress_type, zi.file_size, zi.compress_size, zi.CRC = zipfile.ZIP_STORED, size, size, crc
    zi.header_offset, zi.external_attr = 0, 0o100644 << 16
    pad = ZIP_HDR - len(zi.FileHeader())
    if pad < 4:
        raise ValueError("zip entry name too long")
    zi.extra = struct.pack("<HH", 0xCAFE, pad - 4) + bytes(pad - 4)
    header = zi.FileHeader()
    assert len(header) == ZIP_HDR, (len(header), pad)
    zi.extra = b""  # the padding belongs to the local header only
    return zi, header


def finalize_zip(part, d, name=None, size=None, crc=None):
    """<name>.zip.part (512 B gap + video.mp4 bytes) + the small files in session dir d -> <name>.zip, folder removed.
    The video is NOT copied: its zip header is written into the gap and the rest is appended after it, so the
    disk never holds two copies and finishing takes about as long as writing the small files."""
    part, d = Path(part), Path(d)
    stem = name or d.name
    if size is None or crc is None:  # crash recovery: nobody counted while the video was written
        size, crc = _part_crc(part)
    zi, header = video_entry(size, crc)
    z = part.parent / f"{stem}.zip"
    with open(part, "r+b") as fp:
        if fp.seek(0, 2) != ZIP_HDR + size:
            raise OSError("video part has the wrong size")
        fp.seek(0)
        fp.write(header)
        fp.seek(ZIP_HDR + size)
        with zipfile.ZipFile(fp, "w", compresslevel=1) as zf:  # continues after the video bytes
            zf.filelist.append(zi)
            zf.NameToInfo[zi.filename] = zi
            for p in sorted(d.iterdir()) if d.is_dir() else []:
                if p.name != "video.mp4":
                    zi2 = zipfile.ZipInfo.from_file(p, p.name)
                    zi2.compress_type = zipfile.ZIP_STORED if p.suffix in STORED else zipfile.ZIP_DEFLATED
                    with open(p, "rb", buffering=0) as src, zf.open(zi2, "w") as dst:
                        while chunk := src.read(8 << 20):
                            dst.write(chunk)
    part.replace(z)
    shutil.rmtree(d, ignore_errors=True)
    return z


def recover_parts(root):
    """A crash or power loss leaves <game>_N.zip.part (a playable fragmented mp4 inside) + the <game>_N folder:
    turn each into <game>_N_recovered.zip. Never touches a part that is being written (call before recording)."""
    out = []
    root = Path(root)
    for part in sorted(root.glob("*.zip.part")) if root.is_dir() else []:
        try:
            if part.stat().st_size <= ZIP_HDR:
                part.unlink()
                continue
            stem = part.name[:-len(".zip.part")]
            out.append(finalize_zip(part, root / stem, free_zip_name(root, stem, "recovered")))
            log(f"recovered {out[-1].name}")
        except Exception as e:
            log(f"could not recover {part.name}: {e!r}")
    return out


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


# ---------------------------------------------------------------- game audio sources
# Each source pushes (session time, int16 array (n, 2)) into a sink from its own thread.

def _guid_buf(s):
    h = s.strip("{}").replace("-", "")
    return ctypes.create_string_buffer(struct.pack("<IHH", int(h[:8], 16), int(h[8:12], 16), int(h[12:16], 16))
                                       + bytes.fromhex(h[16:]), 16)


def _com(p, idx, *args):
    """Call method idx of COM interface pointer p. args: (ctypes type, value) pairs. Raises OSError on failure."""
    vt = ctypes.cast(p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *[t for t, _ in args])(vt[idx])
    hr = fn(p, *[v for _, v in args])
    if hr < 0:
        raise OSError(f"COM method {idx} failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
    return hr


def _completion_handler(done, box):
    """Minimal COM object for IActivateAudioInterfaceCompletionHandler: ActivateCompleted sets done, stores op."""
    def query(this, iid, ppv):
        ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))[0] = this
        return 0

    def completed(this, op):
        box.append(op)
        done.set()
        return 0

    ref = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(lambda this: 1)
    fns = [ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(query), ref, ref,
           ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(completed)]
    vt = (ctypes.c_void_p * 4)(*[ctypes.cast(f, ctypes.c_void_p) for f in fns])
    obj = (ctypes.c_void_p * 1)(ctypes.addressof(vt))
    return ctypes.addressof(obj), (fns, vt, obj)  # caller keeps the tuple alive


class ProcessLoopback:
    """Only the sound of one process and its child processes (Windows 10 2004+). Voice chat and music in other
    programs are not included. Raises OSError when Windows refuses."""
    rate, ch = 48000, 2
    FLAGS = 0x00020000 | 0x00040000 | 0x08000000 | 0x80000000  # LOOPBACK | EVENTCALLBACK | SRC_QUALITY | AUTOCONVERTPCM

    def __init__(self, pid):
        self.pid, self.name, self.err, self.ac, self.th = pid, f"game process tree (pid {pid})", None, None, None
        self.stop_ev = threading.Event()

    def start(self, sink, clock):
        params = (ctypes.c_uint32 * 3)(1, self.pid, 0)  # AUDIOCLIENT_ACTIVATION_PARAMS: process loopback, pid, tree

        class PROPVAR(ctypes.Structure):  # VT_BLOB
            _fields_ = [("vt", ctypes.c_uint16), ("res", ctypes.c_uint16 * 3), ("size", ctypes.c_uint32),
                        ("data", ctypes.c_void_p)]

        pv = PROPVAR(65, (ctypes.c_uint16 * 3)(), ctypes.sizeof(params), ctypes.addressof(params))
        iid_client = _guid_buf("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")    # IAudioClient
        iid_capture = _guid_buf("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")  # IAudioCaptureClient
        done, box = threading.Event(), []
        handler, self._keep = _completion_handler(done, box)
        act = ctypes.WinDLL("mmdevapi").ActivateAudioInterfaceAsync
        act.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_void_p)]
        act.restype = ctypes.c_long
        op = ctypes.c_void_p()
        hr = act("VAD\\Process_Loopback", ctypes.addressof(iid_client), ctypes.addressof(pv), handler,
                 ctypes.byref(op))
        if hr < 0:
            raise OSError(f"process loopback refused (HRESULT 0x{hr & 0xFFFFFFFF:08X}; needs Windows 10 2004+)")
        if not done.wait(5):
            raise OSError("process loopback: no answer from Windows")
        res, punk = ctypes.c_long(), ctypes.c_void_p()
        _com(box[0], 3, (ctypes.c_void_p, ctypes.addressof(res)), (ctypes.c_void_p, ctypes.addressof(punk)))
        if res.value < 0:
            raise OSError(f"process loopback refused for pid {self.pid} (HRESULT 0x{res.value & 0xFFFFFFFF:08X})")
        self.ac = punk.value
        wfx = ctypes.create_string_buffer(struct.pack("<HHIIHHH", 1, 2, 48000, 192000, 4, 16, 0), 18)  # PCM 48k stereo
        _com(self.ac, 3, (ctypes.c_uint32, 0), (ctypes.c_uint32, self.FLAGS), (ctypes.c_int64, 2_000_000),
             (ctypes.c_int64, 0), (ctypes.c_void_p, ctypes.addressof(wfx)), (ctypes.c_void_p, None))  # Initialize
        k32 = ctypes.windll.kernel32
        k32.CreateEventW.restype = ctypes.c_void_p
        self.ev = k32.CreateEventW(None, False, False, None)
        _com(self.ac, 13, (ctypes.c_void_p, self.ev))  # SetEventHandle
        cap = ctypes.c_void_p()
        _com(self.ac, 14, (ctypes.c_void_p, ctypes.addressof(iid_capture)), (ctypes.c_void_p, ctypes.addressof(cap)))
        self.cap, self.sink, self.clock, self.wfx = cap.value, sink, clock, wfx
        _com(self.ac, 10)  # Start
        self.th = threading.Thread(target=self._pump, daemon=True)
        self.th.start()
        return self

    def _pump(self):
        k32 = ctypes.windll.kernel32
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.WaitForSingleObject.restype = ctypes.c_uint32
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # this thread talks COM (multithreaded apartment)
        try:
            while not self.stop_ev.is_set():
                if k32.WaitForSingleObject(self.ev, 100) != 0:  # WAIT_OBJECT_0 = data ready
                    continue
                while not self.stop_ev.is_set():
                    n = ctypes.c_uint32()
                    _com(self.cap, 5, (ctypes.c_void_p, ctypes.addressof(n)))  # GetNextPacketSize
                    if n.value == 0:
                        break
                    data, frames, flags = ctypes.c_void_p(), ctypes.c_uint32(), ctypes.c_uint32()
                    dpos, qpc = ctypes.c_uint64(), ctypes.c_uint64()
                    _com(self.cap, 3, (ctypes.c_void_p, ctypes.addressof(data)), (ctypes.c_void_p, ctypes.addressof(frames)),
                         (ctypes.c_void_p, ctypes.addressof(flags)), (ctypes.c_void_p, ctypes.addressof(dpos)),
                         (ctypes.c_void_p, ctypes.addressof(qpc)))  # GetBuffer
                    nf = frames.value
                    a = None
                    if nf:
                        if flags.value & 2 or not data.value:  # AUDCLNT_BUFFERFLAGS_SILENT
                            a = np.zeros((nf, self.ch), np.int16)
                        else:
                            a = np.frombuffer(ctypes.string_at(data.value, nf * 4), np.int16).reshape(nf, self.ch)
                    _com(self.cap, 4, (ctypes.c_uint32, nf))  # ReleaseBuffer
                    if a is not None:
                        self.sink(self.clock(), a)
        except Exception as e:
            self.err = f"{type(e).__name__}: {e}"
            log(f"process loopback: {self.err}")

    def stop(self):
        self.stop_ev.set()
        if self.th:
            self.th.join(2)
        if self.ac:
            try:
                _com(self.ac, 11)  # Stop
            except OSError:
                pass
            self.ac = None


class PcLoopback:
    """Fallback when game-only sound is impossible: everything the PC plays (WASAPI loopback), voice chat included."""

    def __init__(self):
        import pyaudiowpatch as pa
        self.pa, self.p, self.s = pa, pa.PyAudio(), None
        try:
            dev = self.p.get_default_wasapi_loopback()
        except Exception:
            self.p.terminate()
            raise
        self.idx, self.rate, self.ch = dev["index"], int(dev["defaultSampleRate"]), int(dev["maxInputChannels"])
        self.name = f"all PC sound ({dev['name']})"

    def start(self, sink, clock):
        pa = self.pa

        def cb(data, n, _info, _status):
            sink(clock(), np.frombuffer(data, np.int16).reshape(n, self.ch))
            return None, pa.paContinue

        self.s = self.p.open(format=pa.paInt16, channels=self.ch, rate=self.rate, input=True,
                             input_device_index=self.idx, frames_per_buffer=self.rate // 50, stream_callback=cb)
        return self

    def stop(self):
        if self.p:
            if self.s:
                self.s.stop_stream()
                self.s.close()
            self.p.terminate()
            self.p = None


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


def window_pid(hwnd):
    from ctypes import byref, wintypes
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, byref(pid))
    return pid.value or None


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
        set_priority(1)
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
    def __init__(self, out, fps, region_fn, target=None, hwnd=None, game="", title="", audio=True, user=None, pid=None, x264=None,
                 zero_copy=False):
        """target: ("window", hwnd) | ("monitor", 1-based index) for Windows Graphics Capture; None = GDI/mss.
        hwnd: inputs are kept only while this window is in the foreground (None = always).
        pid: the game's process; its sound only (None = all PC sound).
        zero_copy: video.mp4 streams straight into <out>.zip.part (see finalize_zip) instead of a file in out."""
        self.out, self.fps, self.region_fn = Path(out), fps, region_fn
        self.target, self.hwnd, self.game, self.title, self.audio = target, hwnd, game, title, audio
        self.pid = pid
        self.x264 = x264 or X264_PROFILES[X264_DEFAULT]
        self.backend, self.error, self.audio_info, self.audio_error, self.audio_note = None, None, None, None, None
        self.stop_ev, self.lock = threading.Event(), threading.Lock()
        self.q = queue.Queue(maxsize=8)    # ~0.25 s of full-res frames (4K = 33 MB each); full => drop, never stall;
                                           # the encoder refills missed slots with repeats, so timing stays exact
        self.eq = queue.Queue(maxsize=4)   # scaled 720p frames waiting for x264 (keeps full-res RAM bounded)
        self.frames = self.dropped = 0  # frames = real captured frames; slots = frames in video.mp4 (incl. repeats)
        self.slots, self.video_t0, self.t_end = 0, 0.0, None
        self.part = self.out.parent / f"{self.out.name}.zip.part" if zero_copy else None
        self.video_bytes = self.video_crc = None
        self.real_t = []  # session time of every really captured frame (jitter)
        self.user, self.sysinfo, self.note = user or {}, system_info(), None
        self.down, self.focus, self.threads, self.fh = {}, None, [], None
        self.fresh, self.lags, self.bright, self.cstate, self.audio_stats = [], [], [], None, None
        self.last_sound = self.last_input = None  # session time of the last non-silent audio / last input

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
        if IS_WIN:  # default sleep granularity is 15.6 ms, which makes a 30 fps capture loop jitter
            ctypes.windll.winmm.timeBeginPeriod(1)
        workers = [self._capture, self._scale, self._encode] + ([self._game_audio] if self.audio and IS_WIN else [])
        self.threads = [threading.Thread(target=f, daemon=True) for f in workers]
        for th in self.threads:
            th.start()
        return self

    def stop(self):
        self.stop_ev.set()
        for th in self.threads:
            th.join()
        if IS_WIN:
            ctypes.windll.winmm.timeEndPeriod(1)
        dur = self.t()
        with self.lock:
            self.fh.close()
        p = self.out / "inputs.jsonl"
        events = sorted((json.loads(x) for x in open(p, encoding="utf-8")), key=lambda e: e["t"])
        p.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        q, warnings = quality(dur, self.fps, self.fresh, self.lags, self.bright, events,
                              *(self.audio_stats or (None, None)), self.dropped, self.real_t)
        if self.audio_error:
            warnings = [w for w in warnings if w != "no game audio recorded"] + [f"game audio failed: {self.audio_error}"]
        if self.audio_note:
            warnings.append(self.audio_note)
        if self.note:
            warnings.append(self.note)
        meta = {"recorder": VERSION, "game": self.game, "window_title": self.title, "started": self.started,
                "duration_s": round(dur, 3), "fps_requested": self.fps,
                "fps_actual": round(self.frames / dur, 2) if dur else 0,  # real captured frames per second
                "frames": self.slots, "real_frames": self.frames, "repeated_frames": self.slots - self.frames,
                "video_t0_s": round(self.video_t0, 4),
                "dropped": self.dropped, "capture": self.backend, "region": self.region,
                "video_size": list(self.vsize), "video": f"H.264 {self.x264}", "audio": self.audio_info,
                "audio_error": self.audio_error,
                "inputs": "Windows Raw Input, only while the game window is focused. t = session s. "
                          "x/y = absolute screen px (see region). move dx/dy = raw mouse counts summed over "
                          "~16 ms, stamped when the motion began. scroll 120 = one notch. vk/sc = Windows "
                          "virtual-key / scan code. Sorted by t. cursor events: visible + clip (clip set = cursor "
                          "locked to a region, i.e. mouse-look). region events: window moved/resized. focus: game "
                          "window gained/lost focus (keys still held are released with forced=true).",
                "error": self.error, "user": self.user, "system": self.sysinfo, "quality": q, "warnings": warnings}
        (self.out / "meta.json").write_text(json.dumps(meta, indent=1))
        tag = quality_tag(self.x264)
        profile = {"profile": tag, "cpu_load": "light" if tag == "light" else "heavy", "x264": self.x264,
                   "duration_s": meta["duration_s"], "resolution": f"{self.vsize[1]}p",
                   "video_size": list(self.vsize), "fps": self.fps, "fps_mode": "constant",
                   "video_frames": self.slots, "real_frames": self.frames, "repeated_frames": self.slots - self.frames,
                   "real_fps": meta["fps_actual"], "video_t0_s": meta["video_t0_s"],
                   "frame_time": "video time = session time: frame k is at k / fps seconds; repeated slots have fresh = 0 in frames.csv",
                   "capture": self.backend, "recorder": VERSION}
        (self.out / "profile.json").write_text(json.dumps(profile, indent=1))
        return meta

    def package(self, name):
        """Zip name -> path of the finished <name>.zip (zero-copy when the video was streamed into a .part)."""
        if self.part:
            return finalize_zip(self.part, self.out, name, self.video_bytes, self.video_crc)
        return zip_session(self.out, name)

    # input (Raw Input thread)
    def _log(self, t, ev):  # caller holds self.lock
        if not self.fh.closed:
            self.fh.write(json.dumps({"t": round(max(t, 0.0), 4), **ev}) + "\n")

    def on_raw(self, ev):
        if not self.focus:
            return
        pc = ev.pop("pc", None)
        t = pc - self.t0 if pc is not None else self.t()
        self.last_input = t
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
                                     minimum_update_interval=max(1, int(1000 / self.fps) - 4),  # <= ~34 copies/s, still >= fps
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
        set_priority(1)
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
            self.t_end = self.t()
            if close:
                close()
            self.q.put(None)

    def _scale(self):
        """Full-res BGRA -> 720p yuv420p in one swscale pass (AREA keeps small UI text readable). Own thread, so it
        runs beside x264 in _encode instead of after it."""
        import av
        set_priority(-1)
        w, h = self.vsize
        n = 0
        try:
            while (item := self.q.get()) is not None:
                t, img, src_t, fresh = item
                fr = av.VideoFrame.from_ndarray(img, format="bgra").reformat(
                    width=w, height=h, format="yuv420p", interpolation="AREA")
                if n % 30 == 0:
                    self.bright.append(float(img[::16, ::16, :3].mean()))
                n += 1
                self.eq.put((t, fr, src_t, fresh))
        finally:
            self.eq.put(None)

    def _encode(self):
        if not self.part:
            return self._encode_to(str(self.out / "video.mp4"))
        with open(self.part, "wb") as sink:  # zero-copy zip: gap for the zip header, then the mp4 bytes
            sink.write(bytes(ZIP_HDR))
            dest = CrcFile(sink)
            self._encode_to(dest)
            self.video_bytes, self.video_crc = dest.n, dest.crc

    def _encode_to(self, dest):
        import av
        set_priority(-1)
        w, h = self.vsize
        # fragmented mp4: a crash or power loss leaves a playable file
        with av.open(dest, "w", format="mp4",
                     options={"movflags": "frag_keyframe+empty_moov+default_base_moof"}) as c, \
                open(self.out / "frames.csv", "w", newline="") as f:
            try:  # GOP 2 s: cheap random access when sampling frames for training
                st = c.add_stream("libx264", rate=self.fps, options={**self.x264, "g": str(self.fps * 2)})
            except Exception:
                st = c.add_stream("mpeg4", rate=self.fps)
                st.bit_rate = 6_000_000
            st.width, st.height, st.pix_fmt = w, h, "yuv420p"
            wr = csv.writer(f)
            # t = session time we grabbed the frame; src_t = when the source frame arrived; fresh = 0 if repeated
            wr.writerow(["frame", "t", "src_t", "fresh"])
            # Constant frame rate on the session clock: video frame k sits at k / fps = session time k / fps (the
            # slots before the first captured frame repeat that frame, fresh = 0). If capture ran
            # slow (say 27 of 30 fps) the missing slots are filled with a repeat of the previous frame (fresh = 0),
            # so the video is never shorter than the session and stays in sync with inputs.jsonl and the audio.
            last = None  # (yuv frame, src_t) of the last real frame

            def put(fr, t, src_t, fresh):
                fr.pts = self.slots
                c.mux(st.encode(fr))
                wr.writerow([self.slots, f"{t:.4f}", f"{src_t:.4f}", fresh])
                if self.slots % 30 == 0:
                    f.flush()
                self.slots += 1

            while (item := self.eq.get()) is not None:
                t, fr, src_t, fresh = item
                if last is None:
                    last = (fr, src_t)
                target = round(t * self.fps)
                while self.slots < target:
                    put(last[0], self.slots / self.fps, last[1], 0)
                if self.slots > target:
                    continue  # two grabs rounded to one slot (timer jitter): keep the first
                put(fr, t, src_t, fresh)
                last = (fr, src_t)
                self.frames += 1
                self.real_t.append(t)
                self.fresh.append(fresh)
                if fresh:
                    self.lags.append(t - src_t)
            if last and self.t_end is not None:  # trailing slots up to the end of the session
                while self.slots < round(self.t_end * self.fps):
                    put(last[0], self.slots / self.fps, last[1], 0)
            c.mux(st.encode())

    def _game_audio(self):
        """The game's own sound. If Windows can't isolate it, all PC sound instead, and the result says so."""
        set_priority(1)
        # ActivateAudioInterfaceAsync fails with 0x800401F0 (CO_E_NOTINITIALIZED) on a thread without COM: that made
        # process loopback "refused" on every PC and silently recorded all PC sound.
        com_ok = ctypes.windll.ole32.CoInitializeEx(None, 0) in (0, 1)  # multithreaded apartment; 1 = already inited
        out, stats, src, q = None, [0, 0], None, queue.Queue()  # stats: chunks, silent chunks

        def drain(final=False):
            """Write queued chunks. Running: stop as soon as stop_ev is set (process loopback sends a silent packet
            every ~10 ms, so the queue never stays empty and "until empty" never ends). final: only what is queued
            right now, so a producer that is still running cannot keep this going."""
            left = q.qsize()
            while (left > 0) if final else not self.stop_ev.is_set():
                try:
                    t, a = q.get_nowait() if final else q.get(timeout=0.2)
                except queue.Empty:
                    return
                left -= 1
                stats[0] += 1
                silent = int(a.min() > -8 and a.max() < 8)
                stats[1] += silent
                if not silent:
                    self.last_sound = t
                out.write(t, a)

        try:
            if self.pid:
                try:
                    src = ProcessLoopback(self.pid).start(lambda t, a: q.put((t, a)), self.t)
                except Exception as e:
                    self.audio_note = f"game-only sound unavailable ({e}); recorded all PC sound instead"
                    src = None
            if src is None:
                src = PcLoopback().start(lambda t, a: q.put((t, a)), self.t)
            out = AudioOut(self.out / "game_audio", src.rate)
            self.audio_info = {"file": out.path.name, "source": src.name, "device_rate": src.rate,
                               "device_channels": src.ch, "codec": out.st.codec_context.name,
                               "bit_rate": AUDIO_BPS, "clock": "sample 0 = session t 0; silence padded"}
            while not self.stop_ev.is_set():
                drain()
            src.stop()
            drain(final=True)
            if getattr(src, "err", None):
                self.audio_error = src.err
        except Exception as e:
            self.audio_error = f"{type(e).__name__}: {e}"
            log(f"game audio: {self.audio_error}")
        finally:
            if src:
                src.stop()
            if out:
                out.pad_to(self.t())  # trailing silence: the file ends with the session
                out.close()
                self.audio_stats = (round(out.written / out.rate, 2), round(stats[1] / max(stats[0], 1), 3))
            if com_ok:
                ctypes.windll.ole32.CoUninitialize()


# ---------------------------------------------------------------- UI

class App:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.lang = tk, ttk, "en"
        self.root = root = tk.Tk()
        root.title("Mito Recorder")
        ttk.Style(root).theme_use("vista" if IS_WIN else "clam")  # native look on Windows
        root.resizable(False, False)
        try:  # window and taskbar icon
            if IS_WIN:
                root.iconbitmap(str(resource("icon.ico")))
            self._icon = tk.PhotoImage(file=str(resource("icon.png")))
            root.iconphoto(True, self._icon)
        except Exception as e:
            log(f"icon: {e}")
        self.game = tk.StringVar()
        self.dpi, self.sens = tk.StringVar(), tk.StringVar()
        self.quality = tk.StringVar(value=X264_DEFAULT)
        self.fps_var = tk.StringVar(value=str(FPS_DEFAULT))
        self.out = tk.StringVar(value=str(Path.home() / "MitoData"))
        self.status, self.data_status = tk.StringVar(value=self.t("idle")), tk.StringVar(value="")
        self.rec, self.raw, self.busy, self.toggle_req, self.msg, self.f9 = None, None, False, False, None, False
        self.issues, self.mon, self.unf_since, self.last_mon, self.blink_n = [], [], None, 0.0, 0
        self.marker_req = False  # unused since F8 markers were removed
        self.data_q, self.last_data, self.targets = queue.Queue(), None, []
        self.upd_q, self.upd_info, self.upd_status = queue.Queue(), None, tk.StringVar(value="")
        self.build()
        self.check_update()
        self.refresh()
        if IS_WIN:
            try:
                self.raw = RawInput(self._on_raw).start()
            except Exception as e:
                self.status.set(f"Keyboard/mouse capture unavailable: {e}")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(200, self.tick)

    def t(self, key, **kw):
        return tr(key, self.lang, **kw)

    def set_lang(self, code):
        if code == self.lang:
            return
        self.lang = code
        self.target_text = self.target.get()
        if not self.rec:
            self.status.set(self.t("idle"))
        self.build()

    def build(self):  # (re)creates every widget in the current language; state lives in self.*
        tk, ttk, root, t = self.tk, self.ttk, self.root, self.t
        self.target_text = getattr(self, "target_text", "")
        for w in root.winfo_children():
            w.destroy()
        bar = ttk.Frame(root)
        bar.pack(anchor="e", padx=10, pady=(6, 0))
        for code in ("en", "tr"):
            b = ttk.Button(bar, text=code.upper(), width=4, command=lambda c=code: self.set_lang(c))
            b.pack(side="left", padx=2)
            if code == self.lang:
                b.state(["disabled"])
        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True)
        f, ins, dat = ttk.Frame(nb, padding=16), ttk.Frame(nb, padding=16), ttk.Frame(nb, padding=16)
        upd = ttk.Frame(nb, padding=16)
        nb.add(f, text=t("tab_record"))
        nb.add(ins, text=t("tab_instr"))
        nb.add(dat, text=t("tab_data"))
        nb.add(upd, text=t("tab_update"))
        self.nb, self.ins, self.upd_tab = nb, ins, upd

        # Update tab
        ttk.Label(upd, text=t("this_version", v=VERSION)).pack(anchor="w")
        btns = ttk.Frame(upd)
        btns.pack(anchor="w", pady=8)
        ttk.Button(btns, text=t("check_update"), command=self.check_update).pack(side="left")
        self.upd_btn = ttk.Button(btns, text=t("update_now"), command=self.do_update, state="disabled")
        self.upd_btn.pack(side="left", padx=6)
        ttk.Label(upd, textvariable=self.upd_status, wraplength=560, justify="left").pack(anchor="w")
        self.upd_notes = ttk.Label(upd, text="", wraplength=560, justify="left")
        self.upd_notes.pack(anchor="w", pady=(10, 0))
        if self.upd_info:
            self.show_update(("ok", self.upd_info))

        # Data tab
        ttk.Button(dat, text=t("update_data"), command=self.update_data).pack(anchor="w")
        ttk.Label(dat, textvariable=self.data_status, wraplength=560, justify="left").pack(anchor="w", pady=(8, 10))
        self.tree = ttk.Treeview(dat, columns=("game", "total", "runs"), show="headings", height=14)
        for col, key, w in (("game", "col_game", 260), ("total", "col_total", 200), ("runs", "col_runs", 90)):
            self.tree.heading(col, text=t(key))
            self.tree.column(col, width=w, anchor="w" if col == "game" else "center")
        self.tree.pack(fill="both", expand=True)
        if self.last_data:
            self.show_data(*self.last_data)

        # Record tab
        row = itertools.count()

        def line(label, *widgets):
            r = next(row)
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3)
            for c, w in enumerate(widgets, 1):
                w.grid(row=r, column=c, sticky="we", padx=3)

        self.target = ttk.Combobox(f, state="readonly", width=55, values=[lbl for lbl, _, _ in self.targets])
        self.target.bind("<<ComboboxSelected>>", lambda _e: self._pick())
        if self.target_text:
            self.target.set(self.target_text)
        line(t("game_window"), self.target, ttk.Button(f, text=t("refresh"), command=self.refresh))
        line(t("game_name"), ttk.Entry(f, textvariable=self.game, width=58))
        line(t("dpi"), ttk.Entry(f, textvariable=self.dpi, width=10))
        line(t("sens"), ttk.Entry(f, textvariable=self.sens, width=10))
        line(t("quality"), ttk.Combobox(f, state="readonly", width=30, values=list(X264_PROFILES),
                                        textvariable=self.quality))
        ff = ttk.Frame(f)
        self.fps_box = ttk.Combobox(ff, state="readonly", width=6, values=[str(x) for x in FPS_CHOICES],
                                    textvariable=self.fps_var)
        self.fps_box.pack(side="left")
        ttk.Label(ff, text=t("fps_hint"), foreground="#666").pack(side="left", padx=8)
        line(t("fps"), ff)
        line(t("save_to"), ttk.Entry(f, textvariable=self.out, width=58),
             ttk.Button(f, text=t("browse"), command=self.browse))
        self.btn = ttk.Button(f, text=t("stop") if self.rec else t("start"), command=self.toggle)
        self.btn.grid(row=next(row), column=0, columnspan=3, sticky="we", pady=8)
        ttk.Label(f, textvariable=self.status, wraplength=520).grid(row=next(row), column=0, columnspan=3, sticky="w")
        self.banner = tk.Label(f, justify="left", wraplength=540, cursor="hand2")
        self.banner.grid(row=next(row), column=0, columnspan=3, sticky="we", pady=(6, 0))
        self.banner.grid_remove()
        self.bg0 = self.banner.cget("bg")
        self.banner.bind("<Button-1>", lambda _e: self.issues and self.show_fix(self.issues[0][1]))
        bf = ttk.Frame(f)
        bf.grid(row=next(row), column=0, columnspan=3, sticky="we", pady=(0, 6))
        ttk.Button(bf, text=t("upload"), command=lambda: webbrowser.open(UPLOAD_URL)
                   ).pack(side="left", expand=True, fill="x", padx=(0, 3))
        ttk.Button(bf, text=t("open_folder"), command=self.open_folder
                   ).pack(side="left", expand=True, fill="x", padx=(3, 0))
        ttk.Label(f, text=t("consent"), wraplength=520, justify="left", foreground="#666").grid(
            row=next(row), column=0, columnspan=3, sticky="w", pady=(6, 0))

        # Instructions tab
        for txt in (t("howto"), t("tips")):
            ttk.Label(ins, text=txt, wraplength=560, justify="left").pack(anchor="w", pady=(0, 14))
        ttk.Label(ins, text=t("fix_header")).pack(anchor="w")
        self.fix_labels = {}
        for key in FIX_KEYS:
            self.fix_labels[key] = tk.Label(ins, text=t("fix_" + key), wraplength=560, justify="left")
            self.fix_labels[key].pack(anchor="w", pady=(4, 0))
        self.show_banner()

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

    def update_data(self):
        if not DRIVE_API_KEY:
            self.data_status.set(self.t("data_nokey"))
            return
        self.data_status.set(self.t("data_reading"))
        threading.Thread(target=self._fetch_data, daemon=True).start()

    def _fetch_data(self):  # worker thread: hands the result to tick() through a queue
        try:
            self.data_q.put(("ok", summarize(drive_zip_names(DRIVE_API_KEY))))
        except Exception as e:
            self.data_q.put(("err", fetch_error_text(e, self.lang)))

    def show_data(self, kind, val):
        self.last_data = (kind, val)
        self.tree.delete(*self.tree.get_children())
        if kind == "err":
            self.data_status.set(val)
            return
        rows, _ = val  # old-style names (no duration) are left out
        for game, sec, runs in rows:
            self.tree.insert("", "end", values=(game, format_hms(sec, self.lang), runs))
        self.data_status.set(self.t("data_summary", n=sum(r[2] for r in rows), g=len(rows)) if rows else self.t("data_none"))

    def toggle(self):
        from tkinter import messagebox
        if self.busy:
            return
        if self.rec:
            rec, self.rec = self.rec, None
            self.busy = True
            self.btn.config(text=self.t("saving"), state="disabled")
            self.status.set(self.t("finishing"))
            threading.Thread(target=self._finish, args=(rec,), daemon=True).start()
            return
        i = self.target.current()
        if i < 0:
            messagebox.showerror("Mito Recorder", self.t("pick_first"))
            return
        _, hwnd, title = self.targets[i]
        game, root = safe_name(self.game.get()), Path(self.out.get())
        d = root / f"{game}_{next_index(root, game)}"
        try:
            self.rec = Recorder(d, int(self.fps_var.get()), lambda: window_rect(hwnd), target=("window", hwnd),
                                hwnd=hwnd, zero_copy=True, pid=window_pid(hwnd),
                                x264=X264_PROFILES[self.quality.get()],
                                game=game, title=title,
                                user={"mouse_dpi": self.dpi.get().strip(),
                                      "game_sensitivity": self.sens.get().strip()}).start()
        except Exception as e:
            messagebox.showerror("Mito Recorder", str(e))
            return
        self.btn.config(text=self.t("stop"))
        self.issues, self.mon, self.unf_since = [], [], None

    def _finish(self, rec):
        try:
            meta = rec.stop()
            z = rec.package(free_zip_name(rec.out.parent, rec.game, duration_tag(meta["duration_s"])))
            self.msg = (self.t("saved", name=z.name, minutes=meta["duration_s"] / 60, fps=meta["fps_actual"],
                               gb=z.stat().st_size / 1e9) + " " +
                        (self.t("warnings", w="; ".join(meta["warnings"])) if meta["warnings"] else self.t("quality_ok")) +
                        " " + self.t("next_upload"))
        except Exception as e:
            log(f"save failed: {e!r}")
            self.msg = self.t("save_failed", err=e, out=rec.out)
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

    def check_update(self):
        self.upd_status.set(self.t("upd_checking"))
        threading.Thread(target=lambda: self.upd_q.put(self._try(latest_release)), daemon=True).start()

    def do_update(self):
        if self.rec or self.busy:
            self.upd_status.set(self.t("upd_recording"))
            return
        if not getattr(sys, "frozen", False):
            self.upd_status.set(self.t("upd_notexe"))
            return
        info = self.upd_info
        self.upd_btn.state(["disabled"])
        self.upd_status.set(self.t("upd_downloading", v=info["tag"].lstrip("v")))
        threading.Thread(target=lambda: self.upd_q.put(self._try(apply_update, info, kind="done")),
                         daemon=True).start()

    @staticmethod
    def _try(fn, *args, kind="ok"):  # worker thread: result or error text, for tick()
        try:
            return kind, fn(*args)
        except Exception as e:
            log(f"update: {e!r}")
            return "err", f"{type(e).__name__}: {e}"

    def show_update(self, res):
        kind, val = res
        if kind == "err":
            self.upd_status.set(self.t("upd_err", err=val))
            self.upd_btn.state(["!disabled"] if self.upd_info else ["disabled"])
        elif kind == "done":  # new exe already started: hand over
            self.upd_status.set(self.t("upd_restarting"))
            self.root.after(300, self.close)
        elif is_newer(val["tag"]):
            self.upd_info = val
            self.upd_status.set(self.t("upd_new", v=val["tag"].lstrip("v")))
            self.upd_notes.config(text=(self.t("upd_notes") + "\n" + val["notes"]) if val["notes"] else "")
            self.upd_btn.state(["!disabled"])
            self.nb.tab(self.upd_tab, text=self.t("tab_update_new"))
        else:
            self.upd_status.set(self.t("upd_latest"))

    def tick(self):
        try:
            self.show_update(self.upd_q.get_nowait())
        except queue.Empty:
            pass
        if self.toggle_req:
            self.toggle_req = False
            self.toggle()
        if self.msg:
            self.status.set(self.msg)
            self.msg = None
        self.fps_box.config(state="disabled" if self.rec or self.busy else "readonly")
        try:
            self.show_data(*self.data_q.get_nowait())
        except queue.Empty:
            pass
        if self.rec and shutil.disk_usage(self.rec.out).free < LOW_FREE:
            self.rec.note = "stopped early: the disk was almost full"
            self.toggle()
        if self.rec:
            r, secs = self.rec, self.rec.t()
            line = self.t("rec_line", mm=f"{int(secs // 60):02d}", ss=f"{int(secs % 60):02d}", name=r.out.name,
                          fps=f"{r.frames / max(secs, 1e-6):.1f}", d=r.dropped)
            if not r.focus:
                line += self.t("not_focused")
            if r.error:
                line += self.t("err", err=r.error)
            self.status.set(line)
        elif not self.busy and str(self.btn["state"]) == "disabled":
            self.btn.config(text=self.t("start"), state="normal")
        now = time.perf_counter()
        if self.rec and now - self.last_mon >= 1:
            self.last_mon = now
            self.monitor(now)
        self.blink_n += 1
        self.show_banner()
        self.root.after(200, self.tick)

    def monitor(self, now):  # once a second while recording: problems for the warning banner
        r = self.rec
        self.mon = (self.mon + [(now, r.frames, r.dropped)])[-6:]
        (t0, f0, d0), (t1, f1, d1) = self.mon[0], self.mon[-1]
        if r.focus:
            self.unf_since = None
        elif self.unf_since is None:
            self.unf_since = now
        secs = r.t()
        m = {"fps": (f1 - f0) / max(t1 - t0, 1e-6), "target": r.fps,
             "black": bool(r.bright) and r.bright[-1] < 3, "dropped_recent": d1 - d0,
             "unfocused_s": 0 if self.unf_since is None else now - self.unf_since,
             "silent_s": secs - (r.last_sound if r.last_sound is not None else 0) if r.audio and IS_WIN else 0,
             "idle_s": secs - (r.last_input if r.last_input is not None else 0) if r.focus else 0,
             "disk_low": shutil.disk_usage(r.out).free < 2 * LOW_FREE}
        self.issues = live_issues(m)

    def show_banner(self):
        if not self.issues:
            self.banner.grid_remove()
            return
        self.banner.grid()
        color = "#c62828" if self.issues[0][0] == "high" else "#ef6c00"
        on = (self.blink_n // 3) % 2 == 0  # ~0.6 s on/off
        text = " · ".join(self.t("short_" + k) for _, k in self.issues) + "  " + self.t("banner_tail")
        self.banner.config(text=text, bg=color if on else self.bg0, fg="white" if on else color)

    def show_fix(self, key):
        self.nb.select(self.ins)
        w = self.fix_labels[key]
        bg0 = w.cget("bg")
        w.config(bg="#fff176")  # flash the matching block for 2 s
        self.root.after(2000, lambda: w.config(bg=bg0))


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
    rec = Recorder(d, a.fps, lambda: mon, target=("monitor", idx), game=game, title=game, zero_copy=True).start()
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
    assert meta["fps_requested"] == a.fps and abs(meta["frames"] - round(meta["duration_s"] * a.fps)) <= 2, meta  # constant fps
    assert meta["video_size"][1] <= MAX_H, meta["video_size"]
    import av
    z = rec.package(d.name)  # zero-copy zip: the video is read back out of the finished zip
    with zipfile.ZipFile(z) as zf:
        assert zf.testzip() is None
        zf.extract("video.mp4", root / "x")
    with av.open(str(root / "x" / "video.mp4")) as c:  # the fragmented mp4 must decode completely
        n = sum(1 for _ in c.decode(video=0))
    assert abs(n - meta["frames"]) <= 1, (n, meta["frames"])
    d.mkdir(exist_ok=True)  # frames.csv & co. were packed into the zip; unpack them for the checks below
    with zipfile.ZipFile(z) as zf:
        for nm in zf.namelist():
            if not nm.endswith("video.mp4"):
                (d / Path(nm).name).write_bytes(zf.read(nm))
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
    names = zipfile.ZipFile(z).namelist()
    need = ["video.mp4", "frames.csv", "inputs.jsonl", "meta.json", "profile.json"]
    assert all(n in names for n in need), names
    log(f"zip {z.name} {z.stat().st_size / 1e6:.2f} MB for {meta['duration_s']} s at {meta['video_size']}: {names}")
    log("SELFTEST OK")


def uitest():
    app = App()
    app.root.update()
    log(f"UI OK windows={len(app.targets)} raw_input={'on' if app.raw else 'off'}")
    app.close()


def updatetest(d):
    """CI, built exe: put a copy of this exe in d, let the copy update itself from the latest release as if it
    were v0.0 (download, sha256 check, rename-while-running, restart with --uitest), then check the result."""
    import hashlib
    assert getattr(sys, "frozen", False), "--updatetest needs the built exe"
    d = Path(d)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    exe = d / "MitoRecorder.exe"
    shutil.copy2(sys.executable, exe)
    info = latest_release()
    log(f"latest release {info['tag']} sha256 {info['sha256'][:12]}...")
    rc = subprocess.run([str(exe), "--update-now", "--as-version", "0.0"]).returncode
    assert rc == 0, f"update + restarted --uitest exited {rc}"
    got = hashlib.sha256(exe.read_bytes()).hexdigest()
    assert got == info["sha256"], f"exe after update is not the release ({got[:12]})"
    assert not (d / "MitoRecorder.new.exe").exists(), "leftover download"
    rc = subprocess.run([str(exe), "--uitest"]).returncode  # old process gone now: .old must be cleaned up
    assert rc == 0, f"updated exe --uitest exited {rc}"
    if version_tuple(info["tag"]) >= (2, 4):  # releases before 2.4 have no cleanup
        assert not exe.with_suffix(".old").exists(), "MitoRecorder.old was not removed"
    log("UPDATETEST OK")


def update_now(as_version):
    """Child of --updatetest: update this exe if the release is newer than as_version, run the new one."""
    info = latest_release()
    assert is_newer(info["tag"], as_version), info["tag"]
    raise SystemExit(apply_update(info, relaunch=["--uitest"]).wait())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", metavar="DIR")
    ap.add_argument("--uitest", action="store_true")
    ap.add_argument("--updatetest", metavar="DIR")
    ap.add_argument("--update-now", action="store_true")
    ap.add_argument("--as-version", default=VERSION)
    ap.add_argument("--seconds", type=float, default=3)
    ap.add_argument("--fps", type=int, default=FPS_DEFAULT, choices=FPS_CHOICES)
    a = ap.parse_args()
    cleanup_old_exe()
    try:
        if a.updatetest:
            updatetest(a.updatetest)
        elif a.update_now:
            update_now(a.as_version)
        elif a.uitest:
            uitest()
        elif a.selftest:
            selftest(a)
        else:
            app = App()
            threading.Thread(target=recover_parts, args=(app.out.get(),), daemon=True).start()
            app.root.mainloop()
    except ModuleNotFoundError as e:
        if getattr(sys, "frozen", False) and not os.environ.get("MITO_FRESH"):
            # started by an older version's updater, which handed over its own (soon deleted) temp dir: run again clean
            log(f"bundle of the previous process is gone ({e}); restarting clean")
            subprocess.Popen([sys.executable, *sys.argv[1:]], env=fresh_env(MITO_FRESH="1"))
            raise SystemExit(0)
        log(f"FAILED: {e!r}")
        raise SystemExit(1)
    except Exception as e:
        log(f"FAILED: {e!r}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
