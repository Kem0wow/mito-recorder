import zipfile

import numpy as np
import pytest

from recorder import AudioOut, audio_pad, duration_tag, free_zip_name, next_index, out_size, quality, safe_name, \
    vk_name, zip_session


def test_zip_name(tmp_path):
    assert duration_tag(26 * 60 + 38) == "26m_38s" and duration_tag(3600 + 26 * 60 + 38) == "1h_26m_38s"
    assert duration_tag(42) == "42s" and duration_tag(3600 * 2 + 5) == "2h_0m_5s"
    assert free_zip_name(tmp_path, "minecraft", "1h_26m_38s") == "minecraft_1h_26m_38s"
    (tmp_path / "minecraft_1h_26m_38s.zip").write_bytes(b"")
    assert free_zip_name(tmp_path, "minecraft", "1h_26m_38s") == "minecraft_1h_26m_38s_2"
    from recorder import X264_PROFILES, quality_tag
    assert quality_tag(X264_PROFILES["Light (less CPU, bigger files)"]) == "light"
    assert quality_tag(X264_PROFILES["Good quality (heavy CPU)"]) == "good"


def test_safe_name():
    assert safe_name('Minecraft 1.20.1 - Singleplayer') == "Minecraft 1.20.1 - Singleplayer"
    assert safe_name('a<b>:c"/d\\e|f?g*\n') == "abcdefg"
    assert safe_name("  ...  ") == "game"
    assert len(safe_name("x" * 200)) == 60


def test_next_index(tmp_path):
    assert next_index(tmp_path / "missing", "Doom") == 1
    for n in ("Doom_1.zip", "Doom_3", "doom_7.zip.part", "Doom 2_9.zip", "Doom_x.zip"):
        (tmp_path / n).mkdir()
    assert next_index(tmp_path, "Doom") == 4  # .part and other games ignored
    assert next_index(tmp_path, "Doom 2") == 10
    assert next_index(tmp_path, "[weird] (game)") == 1  # regex/glob chars in names are safe


def test_out_size():
    assert out_size(1920, 1080) == (1280, 720)
    assert out_size(2560, 1080) == (1706, 720)
    assert out_size(1281, 721) == (1278, 720)  # aspect kept, made even
    assert out_size(801, 601) == (800, 600)  # small stays native, made even


def test_audio_pad():
    assert audio_pad(0, 0.5, 4800, 48000) == 19200       # first chunk at 0.5 s: 0.4 s of silence before it
    assert audio_pad(24000, 0.6, 4800, 48000) == 0       # on time
    assert audio_pad(24000, 0.62, 4800, 48000) == 0      # jitter under 30 ms ignored
    assert audio_pad(24000, 5.1, 4800, 48000) == 216000  # PC was silent 4.5 s


def test_vk_name():
    assert [vk_name(v) for v in (0x57, 0x31, 0x20, 0x70, 0x60, 0xA0, 0xFE)] == \
        ["w", "1", "space", "f1", "num0", "shift", "vk254"]


def test_zip_session(tmp_path):
    d = tmp_path / "Game 1.5_2"
    d.mkdir()
    (d / "video.mp4").write_bytes(b"\0" * 1000)
    (d / "inputs.jsonl").write_text('{"t": 0}\n' * 100)
    z = zip_session(d)
    assert z.name == "Game 1.5_2.zip" and not d.exists()
    with zipfile.ZipFile(z) as zf:
        info = {i.filename: i.compress_type for i in zf.infolist()}
    assert info == {"Game 1.5_2/inputs.jsonl": zipfile.ZIP_DEFLATED, "Game 1.5_2/video.mp4": zipfile.ZIP_STORED}


def test_audio_out_keeps_session_clock(tmp_path):
    av = pytest.importorskip("av")
    rate = 44100  # device rate differs from the 48 kHz file on purpose
    out = AudioOut(tmp_path / "game_audio", rate)
    tone = (np.sin(np.arange(4410) / 10) * 8000).astype(np.int16)
    out.write(0.5, np.stack([tone] * 2, axis=1))           # first sound at 0.4-0.5 s
    out.write(2.0, np.stack([tone] * 6, axis=1))           # 1.4 s silence, 6-channel device
    out.close()
    with av.open(str(out.path)) as c:
        n = sum(f.samples for f in c.decode(audio=0))
    assert abs(n / 48000 - 2.0) < 0.05, n / 48000


def test_audio_pad_to_end(tmp_path):
    av = pytest.importorskip("av")
    out = AudioOut(tmp_path / "a", 48000)
    out.pad_to(3.0)  # nothing was ever played: the file still spans the session
    out.close()
    with av.open(str(out.path)) as c:
        n = sum(f.samples for f in c.decode(audio=0))
    assert abs(n / 48000 - 3.0) < 0.05


def test_quality():
    ev = [{"t": 0.0, "kind": "focus", "on": True}, {"t": 1.0, "kind": "key_down"}]
    q, w = quality(60, 30, [1] * 1800, [0.01] * 1800, [40.0] * 60, ev, 60.0, 0.3, 0)
    assert w == [] and q["focused_s"] == 60 and q["capture_lag_ms_median"] == 10.0
    bad = [{"t": 0.0, "kind": "focus", "on": True}, {"t": 5.0, "kind": "focus", "on": False}]
    q, w = quality(60, 30, [1] * 1000 + [0] * 100, [0.01], [0.5] * 10, bad, None, None, 50)
    text = " | ".join(w)
    for s in ("mostly black", "only 18.33 fps", "50 frames dropped", "no game audio", "focused less than half",
              "no keyboard or mouse"):
        assert s in text, (s, w)
    _, w = quality(20, 30, [1] * 600, [0.01], [40.0], ev, 20.0, 1.0, 0)
    assert any("silent" in x for x in w)


def test_summarize():
    from recorder import format_hms, summarize
    names = ["minecraft_1h_26m_38s.zip", "Minecraft_30m_22s.zip", "Doom_2_42s.zip", "doom 2_1m_18s_2.zip",
             "Minecraft_3.zip", "notes.txt", "Minecraft_light_1h_2m_7s.zip", "doom 2_good_10s.zip"]
    rows, other = summarize(names)
    # minecraft: 5198 + 1822 s in 2 runs; "doom 2" and "Doom_2" are different game names
    assert rows == [("minecraft", 10747, 3), ("doom 2", 88, 2), ("Doom_2", 42, 1)], rows
    assert other == ["Minecraft_3.zip"]
    assert format_hms(7020) == "1 h 57 min 0 s" and format_hms(42) == "0 h 0 min 42 s"


def test_fetch_error_text():
    import socket
    import urllib.error
    from recorder import fetch_error_text
    assert fetch_error_text(urllib.error.URLError("[Errno -3] Temporary failure in name resolution")) == \
        "no internet connection"
    assert fetch_error_text(socket.timeout("timed out")) == "no internet connection"
    assert fetch_error_text(urllib.error.HTTPError("u", 403, "forbidden", None, None)).startswith("Drive refused")


def test_live_issues():
    from recorder import live_issues
    ok = {"fps": 30, "target": 30, "black": False, "dropped_recent": 0, "unfocused_s": 0, "silent_s": 5,
          "idle_s": 5, "disk_low": False}
    assert live_issues(ok) == []
    bad = dict(ok, fps=12, black=True, unfocused_s=20)
    assert live_issues(bad) == [("high", "black"), ("high", "fps"), ("high", "focus")]
    assert live_issues(dict(ok, fps=25)) == [("mid", "fps")] and live_issues(dict(ok, silent_s=90)) == [("mid", "sound")]


def test_self_update_pieces(tmp_path):
    import hashlib
    import json
    from recorder import download_verified, is_newer, latest_release, swap_exe
    assert is_newer("v2.10", "2.9") and is_newer("v2.4", "2.3") and not is_newer("v2.3", "2.3")
    assert not is_newer("v2.2", "2.3") and is_newer("v2.3", "0.0")
    exe_new = tmp_path / "rel.exe"
    exe_new.write_bytes(b"new build")
    sha = hashlib.sha256(b"new build").hexdigest()
    api = tmp_path / "latest.json"
    asset = {"name": "MitoRecorder.exe", "browser_download_url": exe_new.as_uri(), "digest": f"sha256:{sha}"}
    api.write_text(json.dumps({"tag_name": "v9.1", "body": "notes", "assets": [asset]}))
    info = latest_release(api.as_uri())
    assert info == {"tag": "v9.1", "notes": "notes", "url": exe_new.as_uri(), "sha256": sha}
    api.write_text(json.dumps({"tag_name": "v9.1", "assets": [dict(asset, digest=None)]}))
    with pytest.raises(RuntimeError, match="sha256"):  # no checksum: refuse
        latest_release(api.as_uri())
    with pytest.raises(RuntimeError, match="sha256"):  # wrong checksum: refuse, nothing left behind
        download_verified(info["url"], "0" * 64, tmp_path / "x.new.exe")
    assert not list(tmp_path.glob("x.new.exe*"))
    exe = tmp_path / "MitoRecorder.exe"
    exe.write_bytes(b"old build")
    new = download_verified(info["url"], sha, tmp_path / "MitoRecorder.new.exe")
    old = swap_exe(exe, new)
    assert exe.read_bytes() == b"new build" and old.read_bytes() == b"old build" and not new.exists()


def test_encoder_keeps_constant_30fps_when_capture_is_slow(tmp_path):
    """27 fps capture with a 1 s stall still gives a 30 fps video as long as the session (repeats fill the gaps)."""
    import csv
    import threading
    av = pytest.importorskip("av")
    from recorder import Recorder
    rec = Recorder(tmp_path, 30, lambda: None)
    rec.vsize, rec.t_end = (320, 180), 12.0
    ts = [i / 27 for i in range(270) if not 4 < i / 27 < 5]

    def feed():
        for i, t in enumerate(ts):
            img = np.full((180, 320, 3), i % 255, np.uint8)
            rec.eq.put((2.0 + t, av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuv420p"), 2.0 + t, 1))
        rec.eq.put(None)

    threading.Thread(target=feed, daemon=True).start()
    rec._encode()
    rows = list(csv.DictReader(open(tmp_path / "frames.csv")))
    assert rec.slots == len(rows) == 360                     # 12 s session * 30 fps, video time = session time
    assert rows[0]["fresh"] == "0" and float(rows[0]["t"]) == 0.0   # slots before the first capture repeat it
    assert rec.frames == len(ts) and rec.slots - rec.frames == 360 - len(ts)
    assert sum(r["fresh"] == "0" for r in rows) >= rec.slots - rec.frames
    with av.open(str(tmp_path / "video.mp4")) as c:
        assert sum(1 for _ in c.decode(video=0)) == 360
