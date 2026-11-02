# Mito Recorder

Records gameplay with your spoken thoughts (in English) and keyboard/mouse input, then builds a dataset for fine-tuning a vision-language model on how you reason.

1. Run `MitoRecorder.exe`. Choose the FPS, Screen or Window, the target, the microphone, the save folder and a name.
2. Press **Start**, or **F9** while in the game. Play and think aloud. Press **F9** again to stop.
3. After you stop, faster-whisper (`distil-large-v3`, English) transcribes the audio and the dataset is written. The model downloads about 1.5 GB on first use. Whisper runs on the GPU if CUDA 12 is installed, otherwise on the CPU.

Output in `<folder>/<name>/`:

- **Raw recording:** `video.mp4`, `frames.csv` (frame and time), `audio.wav` and `inputs.jsonl`.
- **Transcript:** `transcript.jsonl` (Whisper segments with word timestamps).
- **Labelled thoughts:** `segments.jsonl`. Each line is one thought with its frame range, its actions and its keyframes.
- **Training file:** `sft.jsonl` in LLaMA-Factory / Qwen-VL `messages` + `images` format. Image paths are relative to the session folder.

Reprocess a session: `MitoRecorder.exe --process <session dir>`. The log is written to `%USERPROFILE%\MitoRecorder.log`.

Tips:
- Run games in borderless windowed mode, because exclusive fullscreen can record black frames.
- Mouse deltas come from the cursor, so games that lock the cursor (FPS games) record no aim motion.
