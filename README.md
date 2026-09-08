# Screen Recorder — meeting recorder for Windows

Records your screens **and both sides of the audio** (what you say + what you
hear), then writes an **MP4** per screen and a single **MP3** of the whole
conversation. Ships as a plain Python app and as a standalone `.exe`.

![status](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)

## What it does

| | |
|---|---|
| **Video** | H.264 MP4, one file per selected screen, at 5–60 fps |
| **Audio in** | Your microphone (WASAPI/MME via PortAudio) |
| **Audio out** | Everything Windows plays — the other participants — via WASAPI loopback |
| **Audio file** | One MP3 of the mixed conversation, alongside the video |
| **Multi-monitor** | Tick several screens and each is recorded to **its own file**, simultaneously |
| **Also** | Custom drag-selected region, pause/resume, live level meters, mouse cursor drawn in, per-source volume |

Both audio sources are recorded to separate tracks and mixed only at the end,
so a dead microphone never costs you the meeting audio, and you can rebalance
mic vs. system volume before you start.

## Quick start (Python)

```bash
pip install -r requirements.txt
python run.py
```

Python 3.9+ (tested on 3.14). Everything, including `ffmpeg`, comes from pip —
nothing to install by hand.

## Build the .exe

```bash
build.bat
```

The result is `dist\ScreenRecorder.exe` — a single file (~80 MB) with ffmpeg
and PortAudio inside. It needs no Python on the target machine. Copy it
anywhere and double-click it.

## Using it for a meeting

1. **Screens** — tick each monitor you want. Every ticked item becomes its own
   MP4 (`Meeting_2026-09-08_14-30-05_Monitor1.mp4`,
   `..._Monitor2.mp4`). Tick *All screens together* instead if you want one
   wide file, or *Custom region* to drag out an area.
2. **Audio** — leave both *System sound* and *Microphone* ticked. Pick the
   playback device your meeting app actually uses (usually your headset) as
   the system source.
3. Press **Test levels** and talk for a moment; both bars should move. This is
   worth doing once per headset — it is the only way to know the meeting audio
   is really being picked up.
4. **Start recording**. The window minimises itself; the timer keeps running in
   the taskbar. **Pause** freezes both picture and sound and leaves no gap in
   the file.
5. **Stop**, then wait a few seconds while the files are written. They land in
   the output folder, which **Open** shows you.

## Output

```
Meeting_2026-09-08_14-30-05_Monitor1.mp4    video + mixed audio
Meeting_2026-09-08_14-30-05_Monitor2.mp4    video + mixed audio
Meeting_2026-09-08_14-30-05.mp3             audio only
```

With a single screen selected, the `_Monitor1` suffix is dropped. Every MP4
carries the full mixed audio, so each file stands on its own.

## Notes and limits

- **System sound is captured per playback device.** Pick the device the meeting
  is actually playing through. If you switch headsets mid-meeting, the loopback
  keeps following the device you selected, not the new default.
- **Recording several 4K screens at once is CPU-heavy.** At 3840×2160 the
  encoder does real work; if the CPU cannot keep up, frames are repeated rather
  than the recording drifting out of sync. Drop to 15–20 fps or *Medium*
  quality for long multi-screen meetings.
- **Disk use**: roughly 10–25 MB/minute per 1080p screen at High quality, more
  for 4K.
- Files are assembled in a temp folder and muxed on **Stop**, so leave the app
  open for the few seconds it takes to finish.
- **Recording other people**: many places require everyone's consent before a
  meeting is recorded. Tell the room.

## How it fits together

```
screen_recorder/
  ui.py            Tkinter window, device pickers, level meters, region picker
  recorder.py      Session: starts the capture threads, muxes the results
  video.py         mss screen grab -> raw BGRA -> ffmpeg (one thread per screen)
  audio.py         mic (sounddevice) + system loopback (soundcard) -> WAV
  clock.py         Shared pausable clock that keeps every stream aligned
  ffmpeg_utils.py  Finds the bundled ffmpeg, probes which encoders it has
  config.py        Settings, saved to %APPDATA%\ScreenRecorder\settings.json
```

Video is piped uncompressed into ffmpeg and encoded live, so nothing large is
ever staged on disk. The shared clock is what keeps picture and sound together:
video frames are emitted against it (duplicating a frame if the CPU stalls) and
both audio streams stop feeding while it is paused.

## Troubleshooting

Run the built-in diagnostic first — it lists every screen and audio device it
can see, records three seconds from all of them, and reports what worked:

```bash
ScreenRecorder.exe --selftest
```

(or `python run.py --selftest`). It writes the same report to
`%TEMP%\ScreenRecorder_selftest.txt`.

| Symptom | Cause |
|---|---|
| *"system audio unavailable"* | `pip install soundcard`; loopback needs Windows 10/11 |
| System bar stays flat | Wrong playback device selected, or nothing is playing on it |
| Mic bar stays flat | Windows mic privacy setting, or the headset is muted at the hardware level |
| *"ffmpeg was not found"* | `pip install imageio-ffmpeg`, or drop `ffmpeg.exe` next to the app |
| Video looks stretched on a scaled display | Fixed by the app's per-monitor DPI awareness; report it if you still see it |
