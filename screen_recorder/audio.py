"""Audio capture for meetings.

Two independent sources are recorded to separate WAV files and mixed later:

* the microphone (what you say)      -> sounddevice / PortAudio
* the system output (what you hear)  -> soundcard / WASAPI loopback

They are kept separate on disk so the mix (and the per-source volume) can be
decided at the very end, and so one failing source never kills the other.
"""

from __future__ import annotations

import ctypes
import queue
import threading
import wave

import numpy as np

try:
    import sounddevice as sd
except Exception:  # pragma: no cover - reported in the UI
    sd = None

try:
    import soundcard as sc
except Exception:  # pragma: no cover - reported in the UI
    sc = None


class AudioUnavailable(RuntimeError):
    pass


# COM is per thread. soundcard only initialises it on the thread that imports
# it, and PortAudio's WASAPI backend needs it on whichever thread opens a
# stream - so every thread that touches audio has to initialise it itself.
# Threads here are ours and short-lived, so the apartment is left in place and
# released by Windows when the thread ends.
_RPC_E_CHANGED_MODE = 0x80010106
_com_state = threading.local()


def _ensure_com() -> None:
    if getattr(_com_state, "ready", False):
        return
    try:
        hresult = ctypes.windll.ole32.CoInitializeEx(None, 0)  # multithreaded
        # S_OK / S_FALSE mean it is usable; RPC_E_CHANGED_MODE means this
        # thread already has a (single-threaded) apartment, also usable.
        _com_state.ready = hresult in (0, 1) or (hresult & 0xFFFFFFFF) == _RPC_E_CHANGED_MODE
    except Exception:
        _com_state.ready = False


def mic_supported() -> bool:
    return sd is not None


def loopback_supported() -> bool:
    return sc is not None


# --------------------------------------------------------------------------
# device discovery
# --------------------------------------------------------------------------
# Windows exposes the same microphone through several host APIs; show each
# physical device once, through the best API available for it.
_HOST_PRIORITY = ("WASAPI", "MME", "DIRECTSOUND", "WDM-KS")


def _host_rank(api_name: str) -> int:
    upper = api_name.upper()
    for rank, key in enumerate(_HOST_PRIORITY):
        if key in upper:
            return rank
    return len(_HOST_PRIORITY)


def list_input_devices():
    """Microphones as [(index, label), ...], one entry per physical device."""
    if sd is None:
        return []
    _ensure_com()
    best = {}
    try:
        apis = sd.query_hostapis()
        for index, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] <= 0:
                continue
            name = " ".join(dev["name"].split())  # some drivers embed newlines
            api = apis[dev["hostapi"]]["name"]
            # MME truncates names to 31 characters, so compare on a short stem.
            key = name.lower()[:28]
            rank = _host_rank(api)
            if key not in best or rank < best[key][0]:
                best[key] = (rank, index, "%s  [%s]" % (name, api))
    except Exception:
        return []
    ordered = sorted(best.values(), key=lambda item: (item[0], item[2].lower()))
    return [(index, label) for _rank, index, label in ordered]


def list_output_devices():
    """Playback devices usable for loopback as [(id, label), ...].

    Recording one of these captures exactly what Windows plays through it,
    which is how the other meeting participants end up in the recording.
    """
    if sc is None:
        return []
    _ensure_com()
    try:
        return [(spk.id, " ".join(spk.name.split())) for spk in sc.all_speakers()]
    except Exception:
        return []


def default_input_index():
    """Windows' default microphone, mapped onto the de-duplicated list."""
    if sd is None:
        return None
    _ensure_com()
    devices = list_input_devices()
    if not devices:
        return None
    try:
        value = sd.default.device[0]
        if value is not None and value >= 0:
            name = " ".join(sd.query_devices(value)["name"].split())
            match = find_device(name, devices)
            if match is not None:
                return match
    except Exception:
        pass
    return devices[0][0]


def default_output_index():
    if sc is None:
        return None
    _ensure_com()
    try:
        return sc.default_speaker().id
    except Exception:
        return None


def find_device(name, devices):
    """Resolve a saved device label back to its id, tolerating small renames."""
    if not name:
        return None
    for device_id, label in devices:
        if label == name:
            return device_id
    stem = name.split("  [")[0]
    for device_id, label in devices:
        if stem and stem in label:
            return device_id
    return None


def device_label(device_id, devices):
    for candidate, label in devices:
        if candidate == device_id:
            return label
    return str(device_id)


# --------------------------------------------------------------------------
# recorders
# --------------------------------------------------------------------------
class _BaseRecorder:
    """Shared WAV writing, level metering and pause handling."""

    def __init__(self, device, path, clock, label="audio", on_error=None):
        self.device = device
        self.path = path
        self.clock = clock
        self.label = label
        self.on_error = on_error
        self.error = None
        self.frames_written = 0
        self.level = 0.0            # peak of the latest block, 0.0 - 1.0
        self.samplerate = 48000
        self.channels = 2
        self._queue = queue.Queue(maxsize=512)
        self._stop = threading.Event()
        self._writer = None
        self._wave = None

    # -- wav plumbing -----------------------------------------------------
    def _open_wave(self):
        self._wave = wave.open(self.path, "wb")
        self._wave.setnchannels(self.channels)
        self._wave.setsampwidth(2)
        self._wave.setframerate(self.samplerate)
        self._writer = threading.Thread(
            target=self._write_loop, name="wav-%s" % self.label, daemon=True)
        self._writer.start()

    def _submit(self, block_int16, peak):
        self.level = peak
        try:
            self._queue.put_nowait(block_int16.tobytes())
        except queue.Full:
            pass  # a stalled disk should drop audio, not block the device

    def _write_loop(self):
        while True:
            try:
                chunk = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue
            if chunk is None:
                break
            try:
                self._wave.writeframes(chunk)
                self.frames_written += len(chunk) // (2 * self.channels)
            except Exception as exc:
                self._fail(exc)
                break

    def _fail(self, exc):
        self.error = exc
        if self.on_error:
            self.on_error(self.label, exc)

    def _close_wave(self):
        if self._writer is not None:
            self._writer.join(timeout=10)
            self._writer = None
        try:
            if self._wave is not None:
                self._wave.close()
        except Exception:
            pass
        self._wave = None
        self.level = 0.0

    @property
    def duration(self) -> float:
        return self.frames_written / float(self.samplerate or 48000)


class MicRecorder(_BaseRecorder):
    """Microphone / line-in via PortAudio."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stream = None

    def start(self):
        if sd is None:
            raise AudioUnavailable(
                "sounddevice is not installed. Run:  pip install sounddevice")
        _ensure_com()  # PortAudio's WASAPI backend needs it on this thread
        info = sd.query_devices(self.device)
        self.channels = max(1, min(2, int(info["max_input_channels"])))
        self.samplerate = int(info.get("default_samplerate") or 48000)
        self._open_wave()
        self._stream = sd.InputStream(
            device=self.device,
            channels=self.channels,
            samplerate=self.samplerate,
            dtype="int16",
            blocksize=1024,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        if self.clock.paused or not indata.size:
            return
        self._submit(indata.copy(), float(np.abs(indata).max()) / 32768.0)

    def stop(self):
        self._stop.set()
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        self._close_wave()


class SystemRecorder(_BaseRecorder):
    """Everything Windows plays on a device, via WASAPI loopback."""

    BLOCK = 1024

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._thread = None
        self._ready = threading.Event()
        self._start_error = None

    def start(self):
        if sc is None:
            raise AudioUnavailable(
                "soundcard is not installed, so system sound cannot be recorded.\n"
                "Run:  pip install soundcard")
        _ensure_com()
        self.samplerate = 48000
        self.channels = 2
        self._open_wave()
        self._thread = threading.Thread(
            target=self._run, name="loopback-%s" % self.label, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            self._stop.set()
            raise AudioUnavailable("the playback device did not start within 10 seconds")
        if self._start_error:
            self._stop.set()
            self._close_wave()
            raise self._start_error

    def _open_loopback(self):
        try:
            mic = sc.get_microphone(self.device, include_loopback=True)
        except Exception:
            mic = None
        if mic is None:
            mic = sc.get_microphone(sc.default_speaker().id, include_loopback=True)
        return mic

    def _run(self):
        _ensure_com()
        try:
            mic = self._open_loopback()
            with mic.recorder(samplerate=self.samplerate,
                              channels=self.channels,
                              blocksize=self.BLOCK) as rec:
                self._ready.set()
                while not self._stop.is_set():
                    block = rec.record(numframes=self.BLOCK)
                    if self.clock.paused or block is None or not len(block):
                        continue
                    peak = float(np.abs(block).max())
                    clipped = np.clip(block, -1.0, 1.0)
                    self._submit((clipped * 32767.0).astype(np.int16), peak)
        except Exception as exc:
            self._start_error = exc if not self._ready.is_set() else None
            if self._ready.is_set():
                self._fail(exc)
            self._ready.set()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._close_wave()


def make_recorder(kind, device, path, clock, on_error=None):
    """kind is 'mic' or 'system'."""
    cls = SystemRecorder if kind == "system" else MicRecorder
    return cls(device, path, clock, label=kind, on_error=on_error)
