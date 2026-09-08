"""Locating and driving the bundled ffmpeg binary."""

from __future__ import annotations

import functools
import os
import subprocess
import shutil
import sys

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _no_window_kwargs() -> dict:
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": si, "creationflags": CREATE_NO_WINDOW}


def _candidates():
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    env = os.environ.get("FFMPEG_BINARY")
    if env:
        yield env
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        yield os.path.join(meipass, exe)
    yield os.path.join(os.path.dirname(os.path.abspath(sys.executable)), exe)
    yield os.path.join(os.path.dirname(os.path.abspath(__file__)), exe)
    try:
        import imageio_ffmpeg

        yield imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        yield found


class FFmpegNotFound(RuntimeError):
    pass


@functools.lru_cache(maxsize=1)
def ffmpeg_path() -> str:
    for cand in _candidates():
        if cand and os.path.isfile(cand):
            return cand
    raise FFmpegNotFound(
        "ffmpeg was not found. Install it with:  pip install imageio-ffmpeg\n"
        "or put ffmpeg.exe next to the application."
    )


def run(args, timeout=None) -> subprocess.CompletedProcess:
    """Run ffmpeg and capture its output (no console window)."""
    return subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        timeout=timeout,
        **_no_window_kwargs(),
    )


def popen(args, **kwargs) -> subprocess.Popen:
    kwargs.setdefault("stdin", subprocess.PIPE)
    kwargs.setdefault("stdout", subprocess.DEVNULL)
    kwargs.setdefault("stderr", subprocess.PIPE)
    return subprocess.Popen(args, **kwargs, **_no_window_kwargs())


@functools.lru_cache(maxsize=1)
def _encoders() -> str:
    try:
        return run([ffmpeg_path(), "-hide_banner", "-encoders"], timeout=30).stdout or ""
    except Exception:
        return ""


def has_encoder(name: str) -> bool:
    text = _encoders()
    return any(line.split()[1:2] == [name] for line in text.splitlines() if line.strip())


def pick_video_encoder() -> str:
    for name in ("libx264", "libopenh264", "h264_mf", "mpeg4"):
        if has_encoder(name):
            return name
    return "mpeg4"


def pick_mp3_encoder() -> str:
    for name in ("libmp3lame", "libshine", "mp3_mf"):
        if has_encoder(name):
            return name
    return "libmp3lame"


def pick_aac_encoder() -> str:
    for name in ("aac", "libfdk_aac", "aac_mf"):
        if has_encoder(name):
            return name
    return "aac"


def encoder_quality_args(encoder: str, crf: int) -> list:
    """Quality flags appropriate to whichever H.264 encoder we ended up with."""
    if encoder in ("libx264", "libopenh264"):
        if encoder == "libx264":
            return ["-preset", "veryfast", "-crf", str(crf)]
        return ["-b:v", "6M"]
    if encoder == "h264_mf":
        return ["-b:v", "6M"]
    return ["-qscale:v", "4"]
