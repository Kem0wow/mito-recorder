from recorder import build_dataset, frame_range, merge_segments, pick_frames, sft_record, summarize


def test_merge_segments():
    segs = [{"start": 0.0, "end": 1.0, "text": "I see a door."},
            {"start": 1.3, "end": 2.0, "text": "Open it."},
            {"start": 5.0, "end": 6.0, "text": "Key next."},
            {"start": 6.1, "end": 6.2, "text": ""}]
    out = merge_segments(segs)
    assert [o["text"] for o in out] == ["I see a door. Open it.", "Key next."]
    assert out[0]["end"] == 2.0
    long = [{"start": i * 5.0, "end": i * 5.0 + 4.9, "text": "x"} for i in range(5)]
    assert all(o["end"] - o["start"] <= 15 for o in merge_segments(long))


def test_frame_range_edges():
    ft = [0.0, 0.1, 0.2, 0.3]
    assert frame_range(ft, 0.05, 0.25) == (1, 2)
    assert frame_range(ft, 0.11, 0.12) == (2, 2)   # no frame inside: next frame
    assert frame_range(ft, 5.0, 6.0) == (3, 3)     # after video end: last frame
    assert frame_range([], 0, 1) is None


def test_pick_frames():
    assert pick_frames(3, 4) == [3, 4]
    assert pick_frames(0, 30) == [0, 10, 20, 30]


def test_summarize_collapses():
    acts = [{"kind": "key_down", "key": "w"}, {"kind": "key_up", "key": "w"},
            {"kind": "key_down", "key": "w"}, {"kind": "mouse_down", "button": "left", "x": 500, "y": 250},
            {"kind": "scroll", "dx": 0, "dy": -1}]
    assert summarize(acts) == "press w x2, left click at (500,250), scroll down"
    assert summarize([{"kind": "move", "x": 1, "y": 1}]) == "move mouse"
    assert summarize([]) == "no input"


def test_build_dataset():
    ft = [i / 10 for i in range(100)]  # 10 s at 10 fps
    inputs = [{"t": 2.5, "kind": "mouse_down", "button": "left", "x": 200, "y": 150},
              {"t": 9.5, "kind": "key_down", "key": "e"}]
    region = {"left": 100, "top": 100, "width": 200, "height": 100}
    items = build_dataset([{"start": 2.0, "end": 3.0, "text": "Click the door."}], ft, inputs, region)
    it = items[0]
    assert (it["frame_start"], it["frame_end"]) == (10, 30)  # 1 s lead
    assert it["actions"] == [{"t": 2.5, "kind": "mouse_down", "button": "left", "x": 500, "y": 500}]
    assert it["action_summary"] == "left click at (500,500)"
    assert it["frames"] == [10, 17, 23, 30]
    it["keyframes"] = ["keyframes/a.jpg"]
    rec = sft_record(it)
    assert rec["messages"][0]["content"].startswith("<image>") and rec["images"] == ["keyframes/a.jpg"]
    assert "Click the door." in rec["messages"][1]["content"]
