"""Headless diagnostics:  ScreenRecorder.exe --selftest

Records a few seconds from every configured source into a temporary folder and
writes a report, so a machine (or a frozen build) can be checked without
touching the UI.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback

REPORT_PATH = os.path.join(tempfile.gettempdir(), "ScreenRecorder_selftest.txt")


def main(seconds: float = 3.0) -> int:
    lines = []

    def say(message=""):
        lines.append(str(message))
        try:
            print(message)
        except Exception:
            pass

    failed = False
    try:
        from . import __version__
        from . import audio, ffmpeg_utils, video
        from .config import Settings
        from .recorder import RecordingSession

        video.enable_dpi_awareness()
        say("Screen Recorder %s self-test" % __version__)
        say("frozen: %s" % bool(getattr(sys, "frozen", False)))
        say("python: %s" % sys.version.split()[0])
        say("")

        say("ffmpeg: %s" % ffmpeg_utils.ffmpeg_path())
        say("  video encoder: %s" % ffmpeg_utils.pick_video_encoder())
        say("  audio encoders: %s / %s"
            % (ffmpeg_utils.pick_aac_encoder(), ffmpeg_utils.pick_mp3_encoder()))

        monitors = video.list_monitors()
        say("screens: %d" % len(monitors))
        for label, _region in monitors:
            say("  %s" % label)

        say("microphones: %d" % len(audio.list_input_devices()))
        for _index, label in audio.list_input_devices():
            say("  %s" % label)
        say("playback devices (system sound): %d" % len(audio.list_output_devices()))
        for _index, label in audio.list_output_devices():
            say("  %s" % label)
        say("")

        temp_out = tempfile.mkdtemp(prefix="screenrec_selftest_")
        settings = Settings()
        settings.output_dir = temp_out
        settings.file_prefix = "SelfTest"
        settings.sources = ["monitor:%d" % i for i in range(1, len(monitors) + 1)]
        settings.fps = 10
        settings.quality = "Low"

        say("recording %.0fs from %s ..." % (seconds, settings.sources))
        session = RecordingSession(settings, log=say)
        session.start()
        time.sleep(seconds)
        session.stop()
        outputs = session.finalize(progress=say)

        say("")
        for path in outputs:
            size = os.path.getsize(path)
            say("wrote %s (%d bytes)" % (os.path.basename(path), size))
            if size < 2048:
                failed = True
                say("  WARNING: that file looks empty")
        if not outputs:
            failed = True
            say("FAILED: nothing was produced")
        for warning in session.errors:
            say("warning: %s" % warning)

        import shutil

        shutil.rmtree(temp_out, ignore_errors=True)
        say("")
        say("RESULT: %s" % ("FAILED" if failed else "OK"))
    except Exception:
        failed = True
        lines.append("FAILED with an exception:")
        lines.append(traceback.format_exc())

    try:
        with open(REPORT_PATH, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass
    return 1 if failed else 0
