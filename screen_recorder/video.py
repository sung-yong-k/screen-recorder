"""Screen capture -> raw frames -> ffmpeg -> H.264 mp4.

One VideoRecorder instance owns one capture area (a monitor, the whole
desktop, or a custom region) and one output file, so several of them can run
side by side to record every screen separately.
"""

from __future__ import annotations

import ctypes
import threading
import time

import numpy as np

from . import ffmpeg_utils

# A small arrow drawn into the frame, because mss cannot capture the real
# cursor.  'X' = black outline, 'O' = white fill.
_ARROW = [
    "X         ",
    "XX        ",
    "XOX       ",
    "XOOX      ",
    "XOOOX     ",
    "XOOOOX    ",
    "XOOOOOX   ",
    "XOOOOOOX  ",
    "XOOOOOOOX ",
    "XOOOOOOOOX",
    "XOOOOOXXXX",
    "XOOXOOX   ",
    "XOX XOOX  ",
    "XX   XOOX ",
    "X    XOOX ",
    "      XOOX",
    "       XXX",
]
_ARROW_H = len(_ARROW)
_ARROW_W = max(len(row) for row in _ARROW)
_ARROW_OUTLINE = np.array(
    [[c < len(row) and row[c] == "X" for c in range(_ARROW_W)] for row in _ARROW],
    dtype=bool,
)
_ARROW_FILL = np.array(
    [[c < len(row) and row[c] == "O" for c in range(_ARROW_W)] for row in _ARROW],
    dtype=bool,
)


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def cursor_position():
    try:
        point = _Point()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return point.x, point.y
    except Exception:
        pass
    return None


def enable_dpi_awareness() -> None:
    """Capture (and draw the UI) in real pixels on scaled displays."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def list_monitors():
    """Physical monitors as [(label, region_dict), ...], in mss order."""
    import mss

    monitors = []
    with mss.mss() as sct:
        for index, mon in enumerate(sct.monitors[1:], start=1):
            label = "Monitor %d  (%dx%d at %d,%d)" % (
                index, mon["width"], mon["height"], mon["left"], mon["top"],
            )
            monitors.append((label, dict(mon)))
    return monitors


def virtual_desktop():
    """The bounding box of every monitor together."""
    import mss

    with mss.mss() as sct:
        return dict(sct.monitors[0])


class VideoRecorder(threading.Thread):
    """Grabs one screen area at a fixed frame rate and pipes it into ffmpeg."""

    def __init__(self, region, fps, out_path, clock, label="video",
                 crf=20, show_cursor=True, on_error=None):
        super().__init__(name="capture-%s" % label, daemon=True)
        self.region = dict(region)
        self.fps = max(1, int(fps))
        self.out_path = out_path
        self.clock = clock
        self.label = label
        self.crf = crf
        self.show_cursor = show_cursor
        self.on_error = on_error
        self._stop = threading.Event()
        self._proc = None
        self._stderr_tail = []
        self.frames_written = 0
        self.error = None

    # -- lifecycle --------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # surfaced to the user through the UI log
            self.error = exc
            if self.on_error:
                self.on_error(self.label, exc)
        finally:
            self._close_proc()

    # -- internals --------------------------------------------------------
    def _start_ffmpeg(self, width, height):
        encoder = ffmpeg_utils.pick_video_encoder()
        args = [
            ffmpeg_utils.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgra",
            "-s", "%dx%d" % (width, height), "-r", str(self.fps),
            "-i", "-", "-an",
            "-c:v", encoder,
        ]
        args += ffmpeg_utils.encoder_quality_args(encoder, self.crf)
        args += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", self.out_path]
        self._proc = ffmpeg_utils.popen(args)
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self):
        try:
            for line in iter(self._proc.stderr.readline, b""):
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self._stderr_tail.append(text)
                    del self._stderr_tail[:-15]
        except Exception:
            pass

    def _close_proc(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=120)
        except Exception:
            proc.kill()

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    def _draw_cursor(self, frame, left, top, width, height):
        pos = cursor_position()
        if not pos:
            return
        x, y = pos[0] - left, pos[1] - top
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(width, x + _ARROW_W), min(height, y + _ARROW_H)
        if x1 <= x0 or y1 <= y0:
            return
        outline = _ARROW_OUTLINE[y0 - y:y1 - y, x0 - x:x1 - x]
        fill = _ARROW_FILL[y0 - y:y1 - y, x0 - x:x1 - x]
        view = frame[y0:y1, x0:x1]
        view[outline] = (0, 0, 0, 255)
        view[fill] = (255, 255, 255, 255)

    def _run(self):
        import mss

        with mss.mss() as sct:
            shot = sct.grab(self.region)
            width, height = shot.width & ~1, shot.height & ~1  # h.264 wants even sides
            if width < 2 or height < 2:
                raise RuntimeError(
                    "Capture area is too small (%dx%d)." % (shot.width, shot.height)
                )
            left, top = self.region["left"], self.region["top"]
            needs_crop = (width != shot.width) or (height != shot.height)
            self._start_ffmpeg(width, height)
            stdin = self._proc.stdin
            frame_time = 1.0 / self.fps

            while not self._stop.is_set():
                if self.clock.paused:
                    time.sleep(0.05)
                    continue

                shot = sct.grab(self.region)
                if needs_crop or self.show_cursor:
                    frame = np.frombuffer(shot.raw, dtype=np.uint8)
                    frame = frame.reshape(shot.height, shot.width, 4)
                    frame = np.ascontiguousarray(frame[:height, :width])
                    if self.show_cursor:
                        self._draw_cursor(frame, left, top, width, height)
                    payload = frame.tobytes()
                else:
                    payload = bytes(shot.raw)

                try:
                    stdin.write(payload)
                    self.frames_written += 1
                    # Keep the stream's clock honest: if we fell behind (a busy
                    # CPU, a slow grab), repeat the frame to fill the gap.
                    target = int(self.clock.elapsed * self.fps)
                    repeats = 0
                    while self.frames_written < target and repeats < self.fps * 2:
                        stdin.write(payload)
                        self.frames_written += 1
                        repeats += 1
                except (BrokenPipeError, OSError, ValueError):
                    raise RuntimeError(
                        "The video encoder stopped unexpectedly.\n%s" % self.stderr_tail
                    )

                sleep_for = (self.frames_written * frame_time) - self.clock.elapsed
                if sleep_for > 0:
                    time.sleep(min(sleep_for, frame_time))
