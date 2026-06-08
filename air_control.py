"""
Air Control — a holographic gesture interface (fast version).

Architecture tuned for high FPS:
  * camera capture runs on a separate thread (never blocks the loop);
  * asynchronous hand tracking (MediaPipe LIVE_STREAM) — the network runs in the
    background while video and panels are drawn at full camera speed;
  * the detector is fed a downscaled copy of the frame (faster inference);
  * panel transparency is blended only over the panel ROI, not the whole frame.

FRAME gesture (panel mode): spread your thumb and index finger so they bracket a window
along its diagonal and the "frame" between them lands on the window's borders = GRAB.
Move your hand and the window follows. Pinch your fingers or move the frame off = release.
In --os mode (real Windows windows) the classic pinch is used instead.

Modes:
  PANELS (default) — holographic panels on the canvas, dragged with a gesture.
  --os  — drag real Windows windows.

Keys: Q/Esc quit · F pause · M mirror · , . grab threshold (pinch in --os) · [ ] sens. (--os)

Run:
    python air_control.py                  # panels, auto-detect a working camera
    python air_control.py --cam 0          # explicitly select camera #0
    python air_control.py --os
    python air_control.py --det-width 480  # detector frame width (smaller = faster)
"""

import argparse
import math
import os
import sys
import threading
import time

import cv2
import numpy as np

import mediapipe as mp
from mediapipe.tasks.python.core.base_options import BaseOptions
from mediapipe.tasks.python import vision

import win32api
import win32con
import win32gui


# ----------------------------- SETTINGS -------------------------------------

DEFAULT_CAM = -1              # -1 = auto-detect a working camera; otherwise an explicit index
MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hand_landmarker.task")

CAP_W, CAP_H = 1280, 720      # capture resolution (the picture)
DET_WIDTH = 560               # frame width fed to the detector (speed/accuracy)

ANIM_SPEED = 0.55             # sci-fi animation tempo (1.0 = original; lower = slower)

PINCH_ON = 0.045              # pinch ON threshold (fingers together) — --os only
PINCH_OFF = 0.090             # release threshold (wide hysteresis band) — --os only

FRAME_GRAB_ON = 0.32          # IoU "the 2-finger frame landed on a window" -> grab (panel mode)
FRAME_GRAB_OFF = 0.12         # IoU below this for N frames in a row -> release the window
POS_ALPHA = 0.40              # cursor position smoothing (lower = smoother, more lag)
PINCH_ALPHA = 0.50            # pinch value smoothing
RELEASE_FRAMES = 4            # frames in a row of "released" before letting go
LOST_GRACE = 6                # frames we tolerate the hand missing before dropping the object
SMOOTHING = 0.35              # (for the --os on-screen cursor mode)
ACTIVE_MARGIN = 0.13
GAIN = 1.0

SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
]

CYAN = (255, 215, 60)
ORANGE = (40, 170, 255)
WHITE = (245, 245, 245)
DARKFILL = (45, 28, 8)


# ----------------------------- CAMERA THREAD --------------------------------

class CameraThread:
    """Reads the camera in the background, always returns the freshest frame (no loop blocking)."""

    def __init__(self, idx):
        self.cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if self.cap.isOpened():
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAP_W)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAP_H)
            self.cap.set(cv2.CAP_PROP_FPS, 30)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._frame = None
        self._lock = threading.Lock()
        self._running = True
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def opened(self):
        return self.cap.isOpened()

    def _loop(self):
        while self._running:
            ok, f = self.cap.read()
            if ok:
                with self._lock:
                    self._frame = f
            else:
                time.sleep(0.005)

    def read(self):
        with self._lock:
            return None if self._frame is None else self._frame

    def release(self):
        self._running = False
        time.sleep(0.05)
        self.cap.release()


# ----------------------------- HOLO PANELS ----------------------------------

class Panel:
    def __init__(self, x, y, w, h, title, kind):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.title, self.kind = title, kind

    def contains(self, px, py):
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h


def _content_reactor(img, x, y, w, h, t, col):
    cx, cy = x + w // 2, y + h // 2
    base = min(w, h) // 2 - 12
    pulse = int(6 * math.sin(t * 0.12))
    cv2.circle(img, (cx, cy), base + pulse, col, 2)
    cv2.circle(img, (cx, cy), int(base * 0.62), col, 1)
    cv2.circle(img, (cx, cy), int(base * 0.30) + pulse // 2, ORANGE, -1)
    for i in range(12):
        a = t * 0.05 + i * (math.pi / 6)
        p1 = (int(cx + base * 0.7 * math.cos(a)), int(cy + base * 0.7 * math.sin(a)))
        p2 = (int(cx + base * 0.92 * math.cos(a)), int(cy + base * 0.92 * math.sin(a)))
        cv2.line(img, p1, p2, col, 1)
    cv2.putText(img, "CORE: 100%", (x + 8, y + h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.4, col, 1, cv2.LINE_AA)


def _content_diag(img, x, y, w, h, t, col):
    prev = None
    for i in range(0, w - 12, 2):
        px = x + 6 + i
        py = int(y + h * 0.45 + (h * 0.22) * math.sin(i * 0.18 + t * 0.2))
        if prev:
            cv2.line(img, prev, (px, py), col, 1)
        prev = (px, py)
    bx = x + 8
    for k in range(5):
        val = int((h * 0.3) * (0.5 + 0.5 * math.sin(t * 0.1 + k)))
        cv2.rectangle(img, (bx, y + h - 12), (bx + 10, y + h - 12 - val), col, -1)
        bx += 16
    for i, ln in enumerate(["CPU  42%", "MEM  61%", "NET  OK", "SYS  ONLINE"]):
        cv2.putText(img, ln, (x + w - 110, y + 18 + i * 16), cv2.FONT_HERSHEY_SIMPLEX, 0.4, col, 1, cv2.LINE_AA)


def _content_target(img, x, y, w, h, t, col):
    cx, cy = x + w // 2, y + h // 2
    r = min(w, h) // 2 - 10
    cv2.circle(img, (cx, cy), r, col, 1)
    cv2.line(img, (cx - r - 6, cy), (cx + r + 6, cy), col, 1)
    cv2.line(img, (cx, cy - r - 6), (cx, cy + r + 6), col, 1)
    sweep = t * 0.06
    cv2.line(img, (cx, cy), (int(cx + r * math.cos(sweep)), int(cy + r * math.sin(sweep))), ORANGE, 2)
    cv2.putText(img, "LOCK", (cx - 22, cy + r + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, ORANGE, 1, cv2.LINE_AA)


CONTENT = {"reactor": _content_reactor, "diag": _content_diag, "target": _content_target}


def draw_panel(frame, p, t, active):
    fh, fw = frame.shape[:2]
    x = max(0, min(p.x, fw - 1)); y = max(0, min(p.y, fh - 1))
    w = min(p.w, fw - x); h = min(p.h, fh - y)
    col = ORANGE if active else CYAN

    # transparent fill — only over the panel ROI (fast)
    roi = frame[y:y + h, x:x + w]
    overlay = roi.copy()
    overlay[:] = DARKFILL
    dim = (col[0] // 4, col[1] // 4, col[2] // 4)
    for gx in range(0, w, 22):
        cv2.line(overlay, (gx, 0), (gx, h), dim, 1)
    for gy in range(0, h, 22):
        cv2.line(overlay, (0, gy), (w, gy), dim, 1)
    cv2.addWeighted(overlay, 0.40, roi, 0.60, 0, roi)

    # border + corner brackets (drawn in absolute panel coordinates)
    px, py, pw, ph = p.x, p.y, p.w, p.h
    cv2.rectangle(frame, (px, py), (px + pw, py + ph), col, 1)
    L = 20
    for (bx, by, dx, dy) in [(px, py, 1, 1), (px + pw, py, -1, 1), (px, py + ph, 1, -1), (px + pw, py + ph, -1, -1)]:
        cv2.line(frame, (bx, by), (bx + dx * L, by), col, 2)
        cv2.line(frame, (bx, by), (bx, by + dy * L), col, 2)
    cv2.rectangle(frame, (px, py), (px + pw, py + 22), col, -1)
    cv2.putText(frame, p.title, (px + 10, py + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (15, 15, 15), 1, cv2.LINE_AA)
    CONTENT[p.kind](frame, px + 4, py + 26, pw - 8, ph - 30, t, col)


def default_panels(fw, fh):
    return [
        Panel(int(fw * 0.06), int(fh * 0.20), 200, 200, "POWER CORE", "reactor"),
        Panel(int(fw * 0.40), int(fh * 0.12), 250, 160, "SYSTEM DIAGNOSTICS", "diag"),
        Panel(int(fw * 0.64), int(fh * 0.50), 190, 190, "TARGETING", "target"),
    ]


def iou(a, b):
    """IoU of two rectangles given as (x0, y0, x1, y1). 1.0 = perfect overlap."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    if inter == 0:
        return 0.0
    area_a = max(0, ax1 - ax0) * max(0, ay1 - ay0)
    area_b = max(0, bx1 - bx0) * max(0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


# ----------------------------- WINAPI UTILS ---------------------------------

def screen_size():
    w = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN) or win32api.GetSystemMetrics(0)
    h = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN) or win32api.GetSystemMetrics(1)
    return w, h


def virtual_origin():
    return (win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN),
            win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN))


def top_level_window_at(x, y):
    try:
        hwnd = win32gui.WindowFromPoint((int(x), int(y)))
    except Exception:  # noqa: BLE001
        return None
    if not hwnd:
        return None
    root = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
    if not root or win32gui.GetClassName(root) in SKIP_CLASSES or not win32gui.IsWindowVisible(root):
        return None
    return root


def restore_if_maximized(hwnd):
    if win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMAXIMIZED:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)


def move_window_topleft(hwnd, left, top):
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, int(left), int(top), 0, 0,
                          win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | win32con.SWP_ASYNCWINDOWPOS)


def list_cameras(max_idx=6):
    print("Scanning cameras (DirectShow):")
    for i in range(max_idx):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        ok = cap.isOpened()
        info = f"{int(cap.get(3))}x{int(cap.get(4))}" if ok else ""
        cap.release()
        print(f"  [{i}] {'OK ' + info if ok else '-'}")


def auto_detect_camera(max_idx=8):
    """Find the first camera that actually delivers a frame. Returns the index or -1.

    We check not only isOpened() but an actual frame read — this skips devices held by
    another process and "ghost" devices that open fine but produce no picture."""
    for i in range(max_idx):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        got = False
        if cap.isOpened():
            for _ in range(5):            # a couple of attempts to warm the camera up
                got, _frame = cap.read()
                if got and _frame is not None:
                    break
                time.sleep(0.05)
        cap.release()
        if got:
            print(f"[i] Auto-selected camera: #{i}")
            return i
    return -1


# ----------------------------- SHARED TRACKING STATE ------------------------

class Tracker:
    """Holds the latest result from the asynchronous LIVE_STREAM callback."""
    def __init__(self):
        self.lock = threading.Lock()
        self.landmarks = None   # list of 21 normalized (x, y)
        self.norm_pinch = None

    def update(self, result, output_image, timestamp_ms):
        if result.hand_landmarks:
            lms = result.hand_landmarks[0]
            thumb, index, wrist, mid = lms[4], lms[8], lms[0], lms[9]
            palm = math.hypot(wrist.x - mid.x, wrist.y - mid.y) + 1e-6
            npz = math.hypot(thumb.x - index.x, thumb.y - index.y) / palm
            pts = [(p.x, p.y) for p in lms]
            with self.lock:
                self.landmarks = pts
                self.norm_pinch = npz
        else:
            with self.lock:
                self.landmarks = None
                self.norm_pinch = None

    def get(self):
        with self.lock:
            return self.landmarks, self.norm_pinch


# ----------------------------- MAIN LOOP ------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=DEFAULT_CAM)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--os", action="store_true")
    ap.add_argument("--det-width", type=int, default=DET_WIDTH)
    args = ap.parse_args()

    if args.list:
        list_cameras()
        return
    if not os.path.exists(MODEL_PATH):
        print(f"[!] Model file not found: {MODEL_PATH}")
        sys.exit(1)

    cam_idx = args.cam
    if cam_idx < 0:
        print("[i] No camera specified — searching for an available one...")
        cam_idx = auto_detect_camera()
        if cam_idx < 0:
            print("[!] No working camera found. Connect a camera "
                  "or set an index manually: --cam N (list them with --list).")
            sys.exit(1)

    cam = CameraThread(cam_idx)
    if not cam.opened():
        print(f"[!] Camera #{cam_idx} failed to open. Run --list and pick --cam N.")
        sys.exit(1)

    sw, sh = screen_size()
    ox, oy = virtual_origin()
    tracker = Tracker()

    options = vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=MODEL_PATH),
        running_mode=vision.RunningMode.LIVE_STREAM,
        num_hands=1,
        min_hand_detection_confidence=0.5,
        min_hand_presence_confidence=0.5,
        min_tracking_confidence=0.5,
        result_callback=tracker.update,
    )
    landmarker = vision.HandLandmarker.create_from_options(options)

    panels = None
    grabbed_panel = None
    panel_offset = (0, 0)
    pinch = False
    grabbed_hwnd = None
    grab_offset = (0, 0)
    sx, sy = None, None
    cur_x = cur_y = None          # smoothed cursor position (frame coordinates)
    pinch_val = 1.0               # smoothed pinch value (large = open)
    release_count = 0             # "released" frame counter for debouncing
    lost_count = 0                # frames-without-hand counter
    gain = GAIN
    pinch_on = PINCH_ON
    frame_grab_on = FRAME_GRAB_ON
    mirror = True
    frozen = False
    last_ts = -1
    anim = 0

    win_name = "Air Control HUD  |  Q-exit  F-pause  M-mirror  , .-pinch"
    cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(win_name, cv2.WND_PROP_TOPMOST, 1)

    print("Running. Frame a panel with your thumb and index finger and drag it.")
    fps_t = time.perf_counter()
    fps = 0.0

    # wait for the first frame
    while cam.read() is None:
        time.sleep(0.01)

    while True:
        frame = cam.read()
        if frame is None:
            time.sleep(0.005)
            continue
        frame = frame.copy()
        if mirror:
            frame = cv2.flip(frame, 1)
        fh, fw = frame.shape[:2]
        anim += ANIM_SPEED

        if panels is None and not args.os:
            panels = default_panels(fw, fh)

        # --- asynchronous feed to the detector (non-blocking) ---
        if not frozen:
            ts = int(time.perf_counter() * 1000)
            if ts <= last_ts:
                ts = last_ts + 1
            last_ts = ts
            scale = args.det_width / fw
            small = cv2.resize(frame, (args.det_width, int(fh * scale)))
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            try:
                landmarker.detect_async(mp_img, ts)
            except Exception:  # noqa: BLE001
                pass

        landmarks, norm_pinch = tracker.get()
        cursor_fx = cursor_fy = None

        if landmarks is not None and not frozen:
            lost_count = 0
            pts = [(int(x * fw), int(y * fh)) for (x, y) in landmarks]
            for a, b in HAND_CONNECTIONS:
                cv2.line(frame, pts[a], pts[b], (0, 200, 255), 2)
            for (ppx, ppy) in pts:
                cv2.circle(frame, (ppx, ppy), 4, WHITE, -1)

            # raw cursor target + EMA smoothing (damps jitter and smooths inference steps)
            raw_x = (landmarks[4][0] + landmarks[8][0]) / 2.0 * fw
            raw_y = (landmarks[4][1] + landmarks[8][1]) / 2.0 * fh
            if cur_x is None:
                cur_x, cur_y = raw_x, raw_y
            else:
                cur_x += (raw_x - cur_x) * POS_ALPHA
                cur_y += (raw_y - cur_y) * POS_ALPHA
            cursor_fx, cursor_fy = cur_x, cur_y

            # smoothed pinch value
            if norm_pinch is not None:
                pinch_val += (norm_pinch - pinch_val) * PINCH_ALPHA

            # --- grab ---
            if args.os:
                # real Windows windows: classic pinch (bring fingers together)
                if not pinch:
                    if pinch_val < pinch_on:
                        pinch = True
                        release_count = 0

                        def remap(v):
                            lo, hi = ACTIVE_MARGIN, 1.0 - ACTIVE_MARGIN
                            v = (v - lo) / (hi - lo)
                            return min(max(0.5 + (v - 0.5) * gain, 0.0), 1.0)
                        gx = ox + remap(cur_x / fw) * sw
                        gy = oy + remap(cur_y / fh) * sh
                        sx, sy = gx, gy
                        grabbed_hwnd = top_level_window_at(sx, sy)
                        if grabbed_hwnd:
                            restore_if_maximized(grabbed_hwnd)
                            l, t0, r, b = win32gui.GetWindowRect(grabbed_hwnd)
                            grab_offset = (l - sx, t0 - sy)
                else:
                    if pinch_val > PINCH_OFF:
                        release_count += 1
                        if release_count >= RELEASE_FRAMES:
                            pinch = False
                            grabbed_hwnd = None
                    else:
                        release_count = 0
            else:
                # panel mode: frame grab. The thumb (4) and index (8) tips define a
                # rectangle; when it lands on a window's borders (IoU >= threshold) we
                # take it. Pinch / move the frame off (low IoU) to release.
                tx, ty = pts[4]
                ix, iy = pts[8]
                grip = (min(tx, ix), min(ty, iy), max(tx, ix), max(ty, iy))
                cv2.rectangle(frame, (grip[0], grip[1]), (grip[2], grip[3]),
                              ORANGE if pinch else CYAN, 1)
                if not pinch:
                    best_p, best_iou = None, 0.0
                    for p in reversed(panels):
                        ov = iou(grip, (p.x, p.y, p.x + p.w, p.y + p.h))
                        if ov > best_iou:
                            best_iou, best_p = ov, p
                    if best_p is not None and best_iou >= frame_grab_on:
                        pinch = True
                        release_count = 0
                        grabbed_panel = best_p
                        panel_offset = (best_p.x - cur_x, best_p.y - cur_y)
                        panels.remove(best_p)
                        panels.append(best_p)
                elif grabbed_panel is not None:
                    held = iou(grip, (grabbed_panel.x, grabbed_panel.y,
                                      grabbed_panel.x + grabbed_panel.w,
                                      grabbed_panel.y + grabbed_panel.h))
                    if held < FRAME_GRAB_OFF:
                        release_count += 1
                        if release_count >= RELEASE_FRAMES:
                            pinch = False
                            grabbed_panel = None
                    else:
                        release_count = 0

            # dragging
            if pinch and args.os and grabbed_hwnd:
                def remap2(v):
                    lo, hi = ACTIVE_MARGIN, 1.0 - ACTIVE_MARGIN
                    v = (v - lo) / (hi - lo)
                    return min(max(0.5 + (v - 0.5) * gain, 0.0), 1.0)
                tgx = ox + remap2(cur_x / fw) * sw
                tgy = oy + remap2(cur_y / fh) * sh
                sx += (tgx - sx) * (1.0 - SMOOTHING)
                sy += (tgy - sy) * (1.0 - SMOOTHING)
                try:
                    move_window_topleft(grabbed_hwnd, sx + grab_offset[0], sy + grab_offset[1])
                except Exception:  # noqa: BLE001
                    grabbed_hwnd = None
            elif pinch and not args.os and grabbed_panel:
                nx = cur_x + panel_offset[0]
                ny = cur_y + panel_offset[1]
                grabbed_panel.x = int(min(max(nx, 0), fw - grabbed_panel.w))
                grabbed_panel.y = int(min(max(ny, 0), fh - grabbed_panel.h))
        elif landmarks is None and not frozen:
            # hand lost — keep a grace period, don't drop the object immediately
            lost_count += 1
            if lost_count > LOST_GRACE:
                pinch = False
                grabbed_hwnd = None
                grabbed_panel = None

        if not args.os and panels:
            for p in panels:
                draw_panel(frame, p, anim, active=(p is grabbed_panel))

        if cursor_fx is not None:
            cc = ORANGE if pinch else CYAN
            r = 16 if not pinch else 10
            ix, iy = int(cursor_fx), int(cursor_fy)
            cv2.circle(frame, (ix, iy), r, cc, 2)
            cv2.line(frame, (ix - r - 6, iy), (ix + r + 6, iy), cc, 1)
            cv2.line(frame, (ix, iy - r - 6), (ix, iy + r + 6), cc, 1)

        now = time.perf_counter()
        fps = 0.9 * fps + 0.1 * (1.0 / max(now - fps_t, 1e-3))
        fps_t = now
        col = ORANGE if pinch else CYAN
        cv2.putText(frame, "GRAB" if pinch else "open", (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.2, col, 3)
        mode = "OS-WINDOWS" if args.os else "HUD PANELS"
        cv2.putText(frame, f"{mode}  fps {fps:4.1f}", (20, fh - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 2)
        if frozen:
            cv2.putText(frame, "PAUSED (F)", (fw // 2 - 90, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.0, ORANGE, 3)

        cv2.imshow(win_name, frame)
        key = cv2.waitKey(1) & 0xFF
        # quit via the window close button: VISIBLE drops below 1 once it's closed
        try:
            if cv2.getWindowProperty(win_name, cv2.WND_PROP_VISIBLE) < 1:
                break
        except cv2.error:
            break
        if key in (ord("q"), 27):
            break
        elif key == ord("f"):
            frozen = not frozen
            pinch, grabbed_hwnd, grabbed_panel = False, None, None
        elif key == ord("["):
            gain = max(0.4, gain - 0.1)
        elif key == ord("]"):
            gain = min(3.0, gain + 0.1)
        elif key == ord(","):
            if args.os:
                pinch_on = max(0.02, pinch_on - 0.005)
            else:
                frame_grab_on = max(0.10, frame_grab_on - 0.02)
        elif key == ord("."):
            if args.os:
                pinch_on = min(0.12, pinch_on + 0.005)
            else:
                frame_grab_on = min(0.70, frame_grab_on + 0.02)
        elif key == ord("m"):
            mirror = not mirror

    cam.release()
    cv2.destroyAllWindows()
    landmarker.close()


if __name__ == "__main__":
    main()
