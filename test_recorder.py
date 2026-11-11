import zipfile

import numpy as np
import pytest

from recorder import AudioOut, audio_pad, next_index, out_size, safe_name, vk_name, zip_session


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
