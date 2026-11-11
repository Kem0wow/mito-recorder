# Mito Recorder

Records gameplay as training data for a game-playing AI. Nothing is uploaded: each run is saved as one zip on your PC.

1. Run `MitoRecorder.exe`, pick the **game window** (run the game in windowed or borderless mode), check the game name.
2. **Start** (or **F9** while in the game). Play. **F9** again to stop. The zip is written automatically.
3. Upload the zips from `C:\Users\<you>\MitoData\` (one `<Game>_<N>.zip` per run, do not unzip) to the shared Drive folder: https://drive.google.com/drive/folders/1g-eHWyhihvk86EOgC-639riVl9WZGBXo?usp=sharing (the app has an Upload button).

Recorded: the game window (720p max, 30 fps), all sound your PC plays (mute voice chat and music apps), and keyboard + mouse **only while the game window is focused** (F9 is never logged).

Inside each zip: `video.mp4` (fragmented, so a crash keeps it playable), `frames.csv` (with source-frame arrival time and a fresh/repeat flag), `inputs.jsonl` (Windows Raw Input, so mouse motion works in FPS games), `game_audio.ogg` (Opus, 96 kbps), `meta.json` (system info, your optional mouse DPI / sensitivity, a quality report with warnings). All share one clock; t = 0 is Start.

Log: `%USERPROFILE%\MitoRecorder.log`. Dev: `pytest test_recorder.py`; CI builds the exe and self-tests it on Windows.
