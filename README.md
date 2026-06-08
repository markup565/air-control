# Air Control 🖐️

A holographic gesture interface. Control windows and sci-fi HUD panels **with hand
movements in front of your webcam** — no mouse, no touchscreen.

Hand tracking is powered by
[MediaPipe HandLandmarker](https://ai.google.dev/edge/mediapipe/solutions/vision/hand_landmarker),
rendering by OpenCV. The architecture is tuned for high FPS: camera capture and neural-network
inference run on separate threads, so the video and panels are drawn at full camera speed.

> Platform: **Windows** (dragging real OS windows uses the Win32 API via `pywin32`). The
> HUD-panel mode is essentially cross-platform, but the project was tested on Windows.

## Features

- **Frame grab.** Spread your thumb and index finger so the rectangle between them lands on a
  window's borders (IoU metric) — the window is "grabbed in a frame". Move your hand and the
  window follows. Pinch your fingers or move the frame off the window to release.
- **Two modes:**
  - `PANELS` (default) — holographic HUD panels (power core, diagnostics, targeting) that you
    drag with your hand.
  - `--os` — drag **real Windows windows** (here the grab is the classic pinch: bring the thumb
    and index finger together).
- Asynchronous tracking (MediaPipe LIVE_STREAM), position smoothing (EMA) and grab/release
  hysteresis, so nothing jitters and nothing is dropped if the hand disappears for a few frames.

## Installation

Requires Python 3.10+.

```bash
git clone https://github.com/markup565/air-control.git
cd air-control
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

The model file `hand_landmarker.task` (~7.8 MB) is already included in the repo — no separate
download needed. The model is © Google, licensed under Apache-2.0.

## Usage

```bash
python air_control.py                 # HUD panels, auto-detect a working camera
python air_control.py --list          # list available cameras
python air_control.py --cam 0         # explicitly select camera #0
python air_control.py --os            # drag real Windows windows
python air_control.py --det-width 480 # detector frame width (smaller = faster)
```

### Gestures

| Mode | Grab | Release |
|------|------|---------|
| Panels (default) | frame the window with thumb + index finger | pinch fingers / move the frame off the window |
| `--os` (Windows windows) | pinch (bring thumb + index together) | spread fingers apart |

### Keys

| Key | Action |
|-----|--------|
| `Q` / `Esc` | quit (closing the window also works) |
| `F` | pause tracking |
| `M` | toggle mirror |
| `,` / `.` | grab threshold (frame in panel mode, pinch in `--os`) |
| `[` / `]` | cursor sensitivity (`--os` only) |

## Configuration

The key parameters are constants at the top of [`air_control.py`](air_control.py):

- `ANIM_SPEED` — sci-fi animation tempo (`1.0` = fast, `0.55` = current, lower = slower).
- `FRAME_GRAB_ON` / `FRAME_GRAB_OFF` — IoU thresholds for frame grab / release.
- `PINCH_ON` / `PINCH_OFF` — pinch thresholds for `--os` mode.
- `CAP_W`, `CAP_H`, `DET_WIDTH` — camera and detector frame resolution (speed/accuracy balance).

## License

MIT — see [LICENSE](LICENSE). The `hand_landmarker.task` model is distributed separately under
the Apache-2.0 license (© Google).
