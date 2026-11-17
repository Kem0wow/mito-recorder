# Mito Recorder

Records gameplay as training data for a game-playing AI. Nothing is uploaded: each run is saved as one zip on your PC.

1. Run `MitoRecorder.exe`, pick the **game window** (run the game in windowed or borderless mode), check the game name. Instructions and tips are on the **Instructions** tab.
2. **Start** (or **F9** while in the game). Play. **F9** again to stop. The zip is written automatically.
3. Upload the zips from `C:\Users\<you>\MitoData\` (one `<Game>_<quality>_<duration>.zip` per run, e.g. `Minecraft_light_1h_2m_7s.zip`, do not unzip) to the shared Drive folder: https://drive.google.com/drive/folders/1g-eHWyhihvk86EOgC-639riVl9WZGBXo?usp=sharing (the app has an Upload button).

4. Updates: the **Update** tab checks GitHub at start-up and shows "(new!)" when a newer release exists. **Update now** downloads it, checks its sha256 against the one GitHub publishes, swaps the exe and restarts. No need to download a new exe by hand.

Releases: bump `VERSION` in `recorder.py`, commit, then push an annotated tag `v<VERSION>` (its message becomes the release notes). CI builds and tests the exe, runs a self-update test against the current latest release, and publishes the release.

Recorded: the game window (720p max, 30 fps; video quality on the Record tab: Good = the standard, Light = same quality target for weak PCs: less CPU, bigger files), the **game's own sound** (only that program's audio, so voice chat and music apps are left out; if Windows cannot isolate it, all PC sound is recorded and the result says so), and keyboard + mouse **only while the game window is focused** (F9 is never logged).

Inside each zip: `video.mp4` (fragmented, so a crash keeps it playable), `frames.csv` (with source-frame arrival time and a fresh/repeat flag; if capture ran below 30 fps the missing slots are repeats, so the video always runs at exactly 30 fps on the session clock), `inputs.jsonl` (Windows Raw Input, so mouse motion works in FPS games), `game_audio.ogg` (Opus, 96 kbps), `profile.json` (light/good profile, 720p, constant 30 fps, real vs repeated frames, duration), `meta.json` (system info, your optional mouse DPI / sensitivity, a quality report with warnings). All share one clock; t = 0 is Start.

While recording, a red or orange warning blinks when something is wrong (black video, low FPS, dropped frames, game not focused, no sound, no input, low disk). Click it to jump to the fix on the Instructions tab.

Data tab: **Update data** lists every recording in the Drive folder, totals the time per game and sorts the games by time. It needs `DRIVE_API_KEY` in `recorder.py` (a Google Cloud API key restricted to the Drive API; the folder must be shared as "anyone with the link").

Log: `%USERPROFILE%\MitoRecorder.log`. Dev: `pytest test_recorder.py`; CI builds the exe and self-tests it on Windows.
