"""Ties the capture threads together and muxes the results with ffmpeg."""

from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import tempfile

from . import audio as audio_mod
from . import ffmpeg_utils, video
from .clock import Clock
from .config import QUALITY_CRF, Settings


def _safe(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned or "Recording"


class Target:
    """One capture area and the files it produces."""

    def __init__(self, key, label, region, temp_path, final_path):
        self.key = key
        self.label = label
        self.region = region
        self.temp_path = temp_path
        self.final_path = final_path
        self.recorder = None


class RecordingSession:
    """Start / pause / stop a recording, then finalise it into mp4 + mp3."""

    def __init__(self, settings: Settings, log=None):
        self.settings = settings
        self.log = log or (lambda msg: None)
        self.clock = Clock()
        self.targets = []
        self.audio_streams = []      # [(gain, AudioRecorder), ...]
        self.temp_dir = None
        self.outputs = []
        self.errors = []
        self._stem = ""

    # -- helpers ----------------------------------------------------------
    def _on_thread_error(self, label, exc):
        self.errors.append("%s: %s" % (label, exc))
        self.log("ERROR [%s] %s" % (label, exc))

    def _resolve_targets(self):
        settings = self.settings
        if not settings.save_mp4:
            return []

        monitors = video.list_monitors()
        chosen = []
        for token in settings.sources:
            if token == "all":
                region = video.virtual_desktop()
                chosen.append(("all", "AllScreens", region))
            elif token.startswith("monitor:"):
                index = int(token.split(":", 1)[1])
                if 1 <= index <= len(monitors):
                    chosen.append((token, "Monitor%d" % index, monitors[index - 1][1]))
                else:
                    self.log("Monitor %d is not connected - skipped." % index)
            elif token == "region":
                x, y, w, h = settings.region
                if w > 1 and h > 1:
                    chosen.append(
                        ("region", "Region",
                         {"left": int(x), "top": int(y), "width": int(w), "height": int(h)})
                    )
                else:
                    self.log("The custom region is empty - skipped.")
        return chosen

    # -- lifecycle --------------------------------------------------------
    def start(self):
        settings = self.settings
        if not settings.save_mp4 and not settings.save_mp3:
            raise RuntimeError("Nothing to record: enable MP4 video, MP3 audio, or both.")

        ffmpeg_utils.ffmpeg_path()  # fail fast with a clear message
        os.makedirs(settings.output_dir, exist_ok=True)
        self.temp_dir = tempfile.mkdtemp(prefix="screenrec_")
        stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._stem = "%s_%s" % (_safe(settings.file_prefix), stamp)

        chosen = self._resolve_targets()
        if settings.save_mp4 and not chosen:
            raise RuntimeError("Select at least one screen (or a custom region) to record.")

        suffix_needed = len(chosen) > 1
        for key, name, region in chosen:
            stem = self._stem + ("_" + name if suffix_needed else "")
            self.targets.append(
                Target(
                    key, name, region,
                    os.path.join(self.temp_dir, "%s.mp4" % name),
                    os.path.join(settings.output_dir, "%s.mp4" % stem),
                )
            )

        self._start_audio()
        self.clock.start()

        crf = QUALITY_CRF.get(settings.quality, 20)
        for target in self.targets:
            target.recorder = video.VideoRecorder(
                region=target.region,
                fps=settings.fps,
                out_path=target.temp_path,
                clock=self.clock,
                label=target.label,
                crf=crf,
                show_cursor=settings.show_cursor,
                on_error=self._on_thread_error,
            )
            target.recorder.start()
            self.log("Recording %s -> %s" % (target.label, os.path.basename(target.final_path)))

        if not self.targets and not self.audio_streams:
            raise RuntimeError("Nothing could be recorded - no screen and no audio source.")

    def _start_audio(self):
        settings = self.settings
        wanted = []
        if settings.record_system:
            wanted.append(("system", True, settings.system_device, settings.system_gain))
        if settings.record_mic:
            wanted.append(("mic", False, settings.mic_device, settings.mic_gain))

        for label, loopback, device_name, gain in wanted:
            try:
                devices = (audio_mod.list_output_devices() if loopback
                           else audio_mod.list_input_devices())
                index = audio_mod.find_device(device_name, devices)
                if index is None:
                    index = (audio_mod.default_output_index() if loopback
                             else audio_mod.default_input_index())
                if index is None:
                    raise audio_mod.AudioUnavailable("no %s device found" % label)

                stream = audio_mod.make_recorder(
                    label,
                    index,
                    os.path.join(self.temp_dir, "%s.wav" % label),
                    self.clock,
                    on_error=self._on_thread_error,
                )
                stream.start()
                self.audio_streams.append((gain, stream))
                self.log("Recording %s audio from %s"
                         % (label, audio_mod.device_label(index, devices)))
            except Exception as exc:
                self.errors.append("%s audio: %s" % (label, exc))
                self.log("WARNING: could not record %s audio - %s" % (label, exc))

        if self.settings.save_mp3 and not self.audio_streams:
            self.log("WARNING: no audio source is active, so no MP3 will be produced.")

    def pause(self):
        self.clock.pause()

    def resume(self):
        self.clock.resume()

    @property
    def elapsed(self) -> float:
        return self.clock.elapsed

    def levels(self):
        """Live input levels as {'system': 0.0-1.0, 'mic': 0.0-1.0}."""
        return {stream.label: stream.level for _, stream in self.audio_streams}

    def stop(self):
        """Stop every capture thread. Call finalize() afterwards."""
        self.clock.resume()
        for target in self.targets:
            if target.recorder:
                target.recorder.stop()
        for target in self.targets:
            if target.recorder:
                target.recorder.join(timeout=180)
        for _, stream in self.audio_streams:
            stream.stop()

    # -- muxing -----------------------------------------------------------
    def _usable_audio(self):
        usable = []
        for gain, stream in self.audio_streams:
            try:
                if stream.frames_written > 0 and os.path.getsize(stream.path) > 1024:
                    usable.append((gain, stream))
                else:
                    self.log("WARNING: %s audio captured nothing (silent or unsupported device)."
                             % stream.label)
            except OSError:
                pass
        return usable

    @staticmethod
    def _audio_filter(streams, first_input=0, out="aout"):
        """volume + resample each source, then mix them into one track."""
        parts, labels = [], []
        for offset, (gain, _stream) in enumerate(streams):
            label = "a%d" % offset
            parts.append(
                "[%d:a]aresample=48000,volume=%.3f[%s]" % (first_input + offset, gain, label)
            )
            labels.append("[%s]" % label)
        if len(streams) > 1:
            parts.append(
                "%samix=inputs=%d:duration=longest:normalize=0[%s]"
                % ("".join(labels), len(streams), out)
            )
        else:
            parts[-1] = parts[-1].replace("[a0]", "[%s]" % out)
        return ";".join(parts)

    def _run(self, args, what):
        result = ffmpeg_utils.run(args)
        if result.returncode != 0:
            tail = "\n".join((result.stdout or "").strip().splitlines()[-8:])
            raise RuntimeError("ffmpeg failed while %s:\n%s" % (what, tail))

    def finalize(self, progress=None):
        """Mux audio into every video and write the MP3. Returns output paths."""
        report = progress or self.log
        streams = self._usable_audio()
        ffmpeg = ffmpeg_utils.ffmpeg_path()

        for target in self.targets:
            if not os.path.exists(target.temp_path) or os.path.getsize(target.temp_path) < 1024:
                self.log("WARNING: %s produced no video." % target.label)
                continue
            report("Writing %s ..." % os.path.basename(target.final_path))
            if streams:
                args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", target.temp_path]
                for _, stream in streams:
                    args += ["-i", stream.path]
                args += [
                    "-filter_complex", self._audio_filter(streams, first_input=1),
                    "-map", "0:v", "-map", "[aout]",
                    "-c:v", "copy",
                    "-c:a", ffmpeg_utils.pick_aac_encoder(), "-b:a", "192k",
                    "-movflags", "+faststart",
                    target.final_path,
                ]
                self._run(args, "adding audio to %s" % target.label)
            else:
                shutil.move(target.temp_path, target.final_path)
            self.outputs.append(target.final_path)

        if self.settings.save_mp3 and streams:
            mp3_path = os.path.join(self.settings.output_dir, "%s.mp3" % self._stem)
            report("Writing %s ..." % os.path.basename(mp3_path))
            encoder = ffmpeg_utils.pick_mp3_encoder()
            args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
            for _, stream in streams:
                args += ["-i", stream.path]
            args += [
                "-filter_complex", self._audio_filter(streams, first_input=0),
                "-map", "[aout]", "-vn", "-c:a", encoder,
            ]
            args += ["-q:a", "2"] if encoder == "libmp3lame" else ["-b:a", "192k"]
            args += [mp3_path]
            self._run(args, "writing the MP3")
            self.outputs.append(mp3_path)

        self.cleanup()
        return self.outputs

    def cleanup(self):
        if self.temp_dir and os.path.isdir(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir = None
