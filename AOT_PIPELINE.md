# AOT pipeline: automatic action labels for recordings

Goal: players only play and upload. A pipeline writes the training labels (what happens, what the player was trying to do) for every recording, in the style of SIMA 2: short imperative task text for short time spans, with a confidence score and a way to check it.

Players never label anything. Nothing in the recorder asks them to.

## 1. Input

One zip per session, from `mito-recorder` (`<Game>_<duration>.zip`):

| File | Use in the pipeline |
|---|---|
| `video.mp4` | 720p max, 30 fps, H.264 veryfast CRF 20, GOP 2 s (cheap random access) |
| `frames.csv` | frame index, session time `t`, source time `src_t`, `fresh` (0 = repeated frame, ignore for labels) |
| `inputs.jsonl` | keyboard, mouse buttons, scroll, raw mouse motion (`move`), cursor lock (`cursor`), `focus`, `region`. Sorted by `t`. Session clock. |
| `game_audio.ogg` | game sound only (per-process loopback). Used for event hints (hit, menu sound), not for labels by itself |
| `meta.json` | game name, duration, fps, quality warnings, system and user info (mouse DPI, sensitivity) |

Everything shares one clock: `t = 0` is Start.

## 2. Game profile (once per game)

The pipeline first learns the game, so the labeler is not guessing from pixels alone.

- Key: the game name from `meta.json` (`game`). Only the game name is sent to web search. Never player name, email, or file paths.
- Agent (Gemini via `agy`, or Claude Haiku as a fallback) searches the web and writes `profiles/<game>.md`:
  - genre, camera (first/third person, top-down), HUD elements and where they sit
  - default controls (move, jump, interact, attack, menu, inventory)
  - common goals and loops ("clear a dungeon", "reach the next village")
  - menu and loading screens, and what they look like
- Profile is reviewed by a human once. Errors fixed by hand. Reused for every session of that game.

## 3. Segmentation

- Cut each session into **spans** of 8-15 s.
- Prefer boundaries at input pauses (no key or mouse for > 1 s) so one span is one action.
- Drop spans that are fully in a menu or loading screen (profile tells what those look like), or mark them `type: "menu"`.
- Drop spans with `focus` off (no inputs recorded there, the screen is not the player's action).
- Each span keeps `start`, `end` in session seconds.

## 4. Visual input (keep quality, save tokens)

For each span:

- Sample frames at **1 fps**, decoded from `video.mp4` (keyframe-aligned seek, then exact frame by `frames.csv`).
- Build **one montage image per span**: up to 16 frames in a 4x4 grid. Each tile 320x180 (so the montage is 1280x720). Frame order left-to-right, top-to-bottom, with the second on each tile.
- Full resolution is not needed for the action. Small text (menus, HUD) is still readable at 320x180 for most games. If a game needs more detail, use a 2x2 grid at 640x360 for that span.
- Keep the original video. Montages are temporary.

## 5. Input summary (text, per span)

Built from `inputs.jsonl` with no model:

- keys pressed in the span, with hold time and order: `W held 2.1 s, space tap at 3.4 s`
- mouse: clicks (button, count, position in screen fraction), scroll, total raw motion `dx, dy`
- cursor lock on/off (mouse-look games)
- silence / no-input stretches

This summary is the strongest signal for *what* happened. The montage shows *where* and *what changed on screen*.

## 6. Labeling prompt

Model: `gemini-3.8-flash` (default `low`, use `medium` on disagreement) via `agy`, or Claude Haiku. Input per call: game profile, the span's montage image, the input summary, and the output schema. One call per span.

Rules the prompt must enforce:
- Describe the **player's action**, in plain imperative form, as an instruction a player could follow. Example: "Open the inventory and equip the sword." Not "The player is walking."
- One task per span. If two things happen, split the span.
- Do not guess what is off-screen. If nothing is clear, `task: null` and `confidence: "low"`.
- No pixel-level claims the montage cannot show (exact numbers, tiny text) unless the profile says the HUD shows them.

## 7. Output: `labels.jsonl` (one line per span)

```json
{"session": "minecraft_1h_26m_38s", "start": 42.0, "end": 53.5, "type": "action",
 "task": "Walk forward and attack the zombie in front of you.",
 "evidence": {"keys": ["W held 3.2 s", "left click x4"], "screen": "zombie in center, health bar drops"},
 "confidence": "high", "labeler": "gemini-3.8-flash-low", "profile": "minecraft@v1"}
```

Fields:
- `type`: `action` | `menu` | `loading` | `unclear`
- `task`: imperative text or `null`
- `evidence`: short facts the label is based on (lets a human check fast)
- `confidence`: `high` | `medium` | `low` (from the model, adjusted by checks below)
- `labeler`, `profile`: which model and game profile version made the label

Session-level file: `labels.jsonl` next to the session's `meta.json`. Never overwrite a label. Re-labeling writes a new line with a new `labeler` value.

## 8. Checks before a label is used

1. **Format check:** valid JSON, `start < end`, spans do not overlap, all inside the session duration.
2. **Confidence rule:** `low` and `unclear` are not used for training. `medium` only with a second labeler agreeing.
3. **Agreement check:** label every span twice (two models or two prompts). If the imperative verbs or the main objects differ, mark `medium`.
4. **Human sample:** 5% of spans, random, per game. If more than 1 in 10 wrong, fix the profile or prompt and relabel that game.
5. **Input sanity:** a span labeled "attack" with zero clicks gets downgraded to `medium`.

Accepted labels go to `dataset/<game>/` as one line per span, merged across sessions, with the session id.

## 9. Costs and limits (measure in the pilot, do not guess)

Pilot: 1 hour of recording, one game. Record:
- seconds per span, per model
- tokens per span (input and output)
- share of spans at each confidence level
- share of human-checked spans that were wrong

Estimate: one 15 s span with a 1280x720 montage is about 1.2k image tokens plus text. Per recorded hour that is about 240 spans, roughly 300k input tokens. Check it against the pilot.

## 10. What the recorder must provide (done in `recorder.py`)

- video quality for labels: H.264 veryfast CRF 20, 720p
- one clock for video, inputs, audio, frames
- game-only sound, so labels are not polluted by voice chat
- the game name (from the window title) in `meta.json`
- live warnings so bad recordings are fixed before upload
- no labeling UI for players (F8 markers were removed)

## Open items

- Automated labeler script (`label_pipeline/`), not written yet.
- Pilot run and cost numbers (section 9).
- Choice of model for the final dataset: Gemini Flash via `agy` (default) vs Claude Haiku.
- Human checker tool (a small review UI showing montage and label).
