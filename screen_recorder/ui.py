"""Tkinter front end for the meeting recorder."""

from __future__ import annotations

import os
import queue
import threading
import tempfile
import shutil

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, __version__
from . import audio as audio_mod
from . import ffmpeg_utils, video
from .clock import Clock
from .config import QUALITY_CRF, Settings
from .recorder import RecordingSession

IDLE, RECORDING, PAUSED, FINISHING = "idle", "recording", "paused", "finishing"


def human_time(seconds: float) -> str:
    seconds = int(seconds)
    return "%02d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


class RegionPicker:
    """Drag a rectangle across the desktop to pick a capture area."""

    def __init__(self, parent):
        self.parent = parent
        self.result = None

    def pick(self):
        desktop = video.virtual_desktop()
        top = tk.Toplevel(self.parent)
        top.overrideredirect(True)
        top.attributes("-alpha", 0.3)
        top.attributes("-topmost", True)
        top.configure(bg="#1e6fd9")
        top.geometry("%dx%d+%d+%d" % (
            desktop["width"], desktop["height"], desktop["left"], desktop["top"]))
        canvas = tk.Canvas(top, cursor="cross", bg="#1e6fd9", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_text(
            desktop["width"] // 2, 40,
            text="Drag to select the area to record  -  Esc to cancel",
            fill="white", font=("Segoe UI", 18, "bold"),
        )

        state = {"x": 0, "y": 0, "rect": None}

        def on_press(event):
            state["x"], state["y"] = event.x, event.y
            if state["rect"]:
                canvas.delete(state["rect"])
            state["rect"] = canvas.create_rectangle(
                event.x, event.y, event.x, event.y, outline="white", width=2)

        def on_drag(event):
            if state["rect"]:
                canvas.coords(state["rect"], state["x"], state["y"], event.x, event.y)

        def on_release(event):
            x0, x1 = sorted((state["x"], event.x))
            y0, y1 = sorted((state["y"], event.y))
            if x1 - x0 > 8 and y1 - y0 > 8:
                self.result = [desktop["left"] + x0, desktop["top"] + y0, x1 - x0, y1 - y0]
            top.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        top.bind("<Escape>", lambda _e: top.destroy())
        top.focus_force()
        self.parent.wait_window(top)
        return self.result


class RecorderApp(ttk.Frame):
    def __init__(self, root: tk.Tk):
        super().__init__(root, padding=10)
        self.root = root
        self.settings = Settings.load()
        self.state_name = IDLE
        self.session = None
        self.ui_queue = queue.Queue()
        self.monitor_vars = []      # [(token, BooleanVar, label)]
        self._preview = None

        self.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(4, weight=1)

        self._build_output()
        self._build_screens()
        self._build_audio()
        self._build_controls()
        self._build_log()

        self.refresh_screens()
        self.refresh_audio_devices()
        self._apply_settings_to_widgets()
        self._poll()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            ffmpeg_utils.ffmpeg_path()
        except ffmpeg_utils.FFmpegNotFound as exc:
            self.log("ERROR: %s" % exc)
            self.start_btn.state(["disabled"])

    # -- widgets ----------------------------------------------------------
    def _build_output(self):
        frame = ttk.LabelFrame(self, text="Output", padding=8)
        frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Folder").grid(row=0, column=0, sticky="w")
        self.folder_var = tk.StringVar(value=self.settings.output_dir)
        ttk.Entry(frame, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frame, text="Browse...", command=self.choose_folder).grid(row=0, column=2)
        ttk.Button(frame, text="Open", command=self.open_folder).grid(row=0, column=3, padx=(4, 0))

        ttk.Label(frame, text="Name").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.prefix_var = tk.StringVar(value=self.settings.file_prefix)
        ttk.Entry(frame, textvariable=self.prefix_var, width=24).grid(
            row=1, column=1, sticky="w", padx=6, pady=(6, 0))

        formats = ttk.Frame(frame)
        formats.grid(row=1, column=2, columnspan=2, sticky="e", pady=(6, 0))
        self.mp4_var = tk.BooleanVar(value=self.settings.save_mp4)
        self.mp3_var = tk.BooleanVar(value=self.settings.save_mp3)
        ttk.Checkbutton(formats, text="MP4 video", variable=self.mp4_var).pack(side="left")
        ttk.Checkbutton(formats, text="MP3 audio", variable=self.mp3_var).pack(side="left", padx=(10, 0))

    def _build_screens(self):
        frame = ttk.LabelFrame(self, text="Screens  (each ticked item becomes its own file)", padding=8)
        frame.grid(row=1, column=0, sticky="nsew", padx=(0, 4))
        frame.columnconfigure(0, weight=1)

        self.screens_inner = ttk.Frame(frame)
        self.screens_inner.grid(row=0, column=0, sticky="ew")
        self.screens_inner.columnconfigure(0, weight=1)

        self.region_var = tk.BooleanVar(value="region" in self.settings.sources)
        region_row = ttk.Frame(frame)
        region_row.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Checkbutton(region_row, text="Custom region", variable=self.region_var).pack(side="left")
        ttk.Button(region_row, text="Select...", width=9, command=self.pick_region).pack(side="left", padx=6)
        self.region_label = ttk.Label(region_row, text="")
        self.region_label.pack(side="left")
        self._update_region_label()

        options = ttk.Frame(frame)
        options.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(options, text="FPS").pack(side="left")
        self.fps_var = tk.IntVar(value=self.settings.fps)
        ttk.Spinbox(options, from_=5, to=60, width=4, textvariable=self.fps_var).pack(side="left", padx=(4, 12))
        ttk.Label(options, text="Quality").pack(side="left")
        self.quality_var = tk.StringVar(value=self.settings.quality)
        ttk.Combobox(options, textvariable=self.quality_var, width=8, state="readonly",
                     values=list(QUALITY_CRF)).pack(side="left", padx=4)
        self.cursor_var = tk.BooleanVar(value=self.settings.show_cursor)
        ttk.Checkbutton(options, text="Cursor", variable=self.cursor_var).pack(side="left", padx=(10, 0))

        ttk.Button(frame, text="Refresh screens", command=self.refresh_screens).grid(
            row=3, column=0, sticky="w", pady=(8, 0))

    def _build_audio(self):
        frame = ttk.LabelFrame(self, text="Audio", padding=8)
        frame.grid(row=1, column=1, sticky="nsew", padx=(4, 0))
        frame.columnconfigure(0, weight=1)

        self.system_var = tk.BooleanVar(value=self.settings.record_system)
        ttk.Checkbutton(frame, text="System sound  (what you hear - the meeting)",
                        variable=self.system_var).grid(row=0, column=0, sticky="w")
        self.system_device_var = tk.StringVar()
        self.system_combo = ttk.Combobox(frame, textvariable=self.system_device_var, state="readonly")
        self.system_combo.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.system_meter = ttk.Progressbar(frame, maximum=100, length=120)
        self.system_meter.grid(row=2, column=0, sticky="ew", pady=(2, 8))

        self.mic_var = tk.BooleanVar(value=self.settings.record_mic)
        ttk.Checkbutton(frame, text="Microphone  (what you say)",
                        variable=self.mic_var).grid(row=3, column=0, sticky="w")
        self.mic_device_var = tk.StringVar()
        self.mic_combo = ttk.Combobox(frame, textvariable=self.mic_device_var, state="readonly")
        self.mic_combo.grid(row=4, column=0, sticky="ew", pady=(2, 0))
        self.mic_meter = ttk.Progressbar(frame, maximum=100, length=120)
        self.mic_meter.grid(row=5, column=0, sticky="ew", pady=(2, 8))

        gains = ttk.Frame(frame)
        gains.grid(row=6, column=0, sticky="w")
        ttk.Label(gains, text="Volume  system").pack(side="left")
        self.system_gain_var = tk.DoubleVar(value=self.settings.system_gain)
        ttk.Spinbox(gains, from_=0.1, to=4.0, increment=0.1, width=5,
                    textvariable=self.system_gain_var).pack(side="left", padx=(4, 10))
        ttk.Label(gains, text="mic").pack(side="left")
        self.mic_gain_var = tk.DoubleVar(value=self.settings.mic_gain)
        ttk.Spinbox(gains, from_=0.1, to=4.0, increment=0.1, width=5,
                    textvariable=self.mic_gain_var).pack(side="left", padx=4)

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, sticky="w", pady=(8, 0))
        ttk.Button(buttons, text="Refresh devices", command=self.refresh_audio_devices).pack(side="left")
        self.test_btn = ttk.Button(buttons, text="Test levels", command=self.toggle_level_test)
        self.test_btn.pack(side="left", padx=6)

    def _build_controls(self):
        frame = ttk.Frame(self)
        frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=10)
        frame.columnconfigure(3, weight=1)

        self.start_btn = ttk.Button(frame, text="Start recording", command=self.on_start)
        self.start_btn.grid(row=0, column=0)
        self.pause_btn = ttk.Button(frame, text="Pause", command=self.on_pause, state="disabled")
        self.pause_btn.grid(row=0, column=1, padx=6)
        self.stop_btn = ttk.Button(frame, text="Stop", command=self.on_stop, state="disabled")
        self.stop_btn.grid(row=0, column=2)

        self.timer_label = ttk.Label(frame, text="00:00:00", font=("Segoe UI", 18, "bold"))
        self.timer_label.grid(row=0, column=3, sticky="e")

        self.minimize_var = tk.BooleanVar(value=self.settings.minimize_on_start)
        ttk.Checkbutton(self, text="Minimise this window while recording",
                        variable=self.minimize_var).grid(row=3, column=0, columnspan=2, sticky="w")

    def _build_log(self):
        frame = ttk.LabelFrame(self, text="Status", padding=6)
        frame.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(frame, height=8, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(frame, command=self.log_text.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=bar.set)

    # -- helpers ----------------------------------------------------------
    def log(self, message: str):
        self.ui_queue.put(lambda: self._append_log(message))

    def _append_log(self, message: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def refresh_screens(self):
        for child in self.screens_inner.winfo_children():
            child.destroy()
        previous = {token for token, var, _ in self.monitor_vars if var.get()} or set(self.settings.sources)
        self.monitor_vars = []

        try:
            monitors = video.list_monitors()
        except Exception as exc:
            self.log("ERROR: could not enumerate screens - %s" % exc)
            monitors = []

        for index, (label, _region) in enumerate(monitors, start=1):
            token = "monitor:%d" % index
            var = tk.BooleanVar(value=token in previous)
            ttk.Checkbutton(self.screens_inner, text=label, variable=var).grid(
                row=index - 1, column=0, sticky="w")
            self.monitor_vars.append((token, var, label))

        if len(monitors) > 1:
            var = tk.BooleanVar(value="all" in previous)
            ttk.Checkbutton(self.screens_inner,
                            text="All screens together (one wide file)", variable=var).grid(
                row=len(monitors), column=0, sticky="w")
            self.monitor_vars.append(("all", var, "All screens"))

        if monitors and not any(var.get() for _t, var, _l in self.monitor_vars):
            self.monitor_vars[0][1].set(True)
        self.log("Found %d screen(s)." % len(monitors))

    def refresh_audio_devices(self):
        try:
            inputs = audio_mod.list_input_devices()
            outputs = audio_mod.list_output_devices()
        except Exception as exc:
            self.log("ERROR: audio devices unavailable - %s" % exc)
            return

        self.mic_combo["values"] = [label for _i, label in inputs]
        self.system_combo["values"] = [label for _i, label in outputs]

        if self.settings.mic_device in self.mic_combo["values"]:
            self.mic_device_var.set(self.settings.mic_device)
        elif inputs:
            self.mic_device_var.set(
                audio_mod.device_label(audio_mod.default_input_index(), inputs))

        if self.settings.system_device in self.system_combo["values"]:
            self.system_device_var.set(self.settings.system_device)
        elif outputs:
            self.system_device_var.set(
                audio_mod.device_label(audio_mod.default_output_index(), outputs))

        if not audio_mod.loopback_supported():
            self.log("WARNING: system-sound capture is unavailable "
                     "(install it with:  pip install soundcard).")
        self.log("Found %d microphone(s) and %d playback device(s)." % (len(inputs), len(outputs)))

    def _update_region_label(self):
        x, y, w, h = self.settings.region
        self.region_label.configure(text="%dx%d at %d,%d" % (w, h, x, y) if w and h else "not set")

    def _apply_settings_to_widgets(self):
        self._update_region_label()

    def choose_folder(self):
        path = filedialog.askdirectory(initialdir=self.folder_var.get() or os.path.expanduser("~"))
        if path:
            self.folder_var.set(path)

    def open_folder(self):
        path = self.folder_var.get()
        os.makedirs(path, exist_ok=True)
        try:
            os.startfile(path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, "Could not open the folder:\n%s" % exc)

    def pick_region(self):
        self.root.withdraw()
        self.root.update()
        try:
            result = RegionPicker(self.root).pick()
        finally:
            self.root.deiconify()
        if result:
            self.settings.region = result
            self.region_var.set(True)
            self._update_region_label()
            self.log("Region set to %dx%d at %d,%d" % (result[2], result[3], result[0], result[1]))

    def collect_settings(self) -> Settings:
        settings = self.settings
        settings.output_dir = self.folder_var.get().strip() or settings.output_dir
        settings.file_prefix = self.prefix_var.get().strip() or "Recording"
        settings.save_mp4 = self.mp4_var.get()
        settings.save_mp3 = self.mp3_var.get()
        sources = [token for token, var, _label in self.monitor_vars if var.get()]
        if self.region_var.get():
            sources.append("region")
        settings.sources = sources
        try:
            settings.fps = max(5, min(60, int(self.fps_var.get())))
        except Exception:
            settings.fps = 25
        settings.quality = self.quality_var.get()
        settings.show_cursor = self.cursor_var.get()
        settings.record_system = self.system_var.get()
        settings.record_mic = self.mic_var.get()
        settings.system_device = self.system_device_var.get()
        settings.mic_device = self.mic_device_var.get()
        try:
            settings.system_gain = float(self.system_gain_var.get())
            settings.mic_gain = float(self.mic_gain_var.get())
        except Exception:
            settings.system_gain = settings.mic_gain = 1.0
        settings.minimize_on_start = self.minimize_var.get()
        settings.save()
        return settings

    def _set_state(self, state):
        self.state_name = state
        recording = state in (RECORDING, PAUSED)
        self.start_btn.state(["disabled"] if state != IDLE else ["!disabled"])
        self.pause_btn.state(["!disabled"] if recording else ["disabled"])
        self.stop_btn.state(["!disabled"] if recording else ["disabled"])
        self.pause_btn.configure(text="Resume" if state == PAUSED else "Pause")

    # -- level test -------------------------------------------------------
    def toggle_level_test(self):
        if self._preview:
            self._stop_level_test()
            return
        if self.state_name != IDLE:
            return
        settings = self.collect_settings()
        streams, temp_dir = [], tempfile.mkdtemp(prefix="levels_")
        clock = Clock()
        clock.start()
        wanted = []
        if settings.record_system:
            wanted.append(("system", True, settings.system_device))
        if settings.record_mic:
            wanted.append(("mic", False, settings.mic_device))
        for label, loopback, name in wanted:
            try:
                devices = (audio_mod.list_output_devices() if loopback
                           else audio_mod.list_input_devices())
                index = audio_mod.find_device(name, devices)
                if index is None:
                    index = (audio_mod.default_output_index() if loopback
                             else audio_mod.default_input_index())
                stream = audio_mod.make_recorder(
                    label, index, os.path.join(temp_dir, label + ".wav"), clock)
                stream.start()
                streams.append(stream)
            except Exception as exc:
                self.log("Level test: %s unavailable - %s" % (label, exc))
        if not streams:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return
        self._preview = (streams, temp_dir)
        self.test_btn.configure(text="Stop test")
        self.log("Level test running - speak, and play some sound in the meeting app.")

    def _stop_level_test(self):
        streams, temp_dir = self._preview
        self._preview = None
        for stream in streams:
            try:
                stream.stop()
            except Exception:
                pass
        shutil.rmtree(temp_dir, ignore_errors=True)
        self.test_btn.configure(text="Test levels")
        self.system_meter["value"] = 0
        self.mic_meter["value"] = 0

    # -- recording --------------------------------------------------------
    def on_start(self):
        if self.state_name != IDLE:
            return
        if self._preview:
            self._stop_level_test()
        settings = self.collect_settings()
        if not settings.save_mp4 and not settings.save_mp3:
            messagebox.showwarning(APP_NAME, "Tick MP4, MP3, or both.")
            return
        if settings.save_mp4 and not settings.sources:
            messagebox.showwarning(APP_NAME, "Tick at least one screen or a custom region.")
            return
        if settings.save_mp3 and not (settings.record_system or settings.record_mic):
            messagebox.showwarning(APP_NAME, "Tick at least one audio source for the MP3.")
            return

        self.session = RecordingSession(settings, log=self.log)
        self._set_state(FINISHING)
        self.log("Starting...")

        def worker():
            try:
                self.session.start()
                self.ui_queue.put(self._started)
            except Exception as exc:
                self.session = None
                # bind now: `exc` is unbound once the except block ends
                self.ui_queue.put(lambda error=exc: self._failed(error))

        threading.Thread(target=worker, daemon=True).start()

    def _started(self):
        self._set_state(RECORDING)
        self.log("Recording.")
        if self.minimize_var.get():
            self.root.iconify()

    def _failed(self, exc):
        self._set_state(IDLE)
        self.log("ERROR: %s" % exc)
        messagebox.showerror(APP_NAME, str(exc))

    def on_pause(self):
        if self.state_name == RECORDING:
            self.session.pause()
            self._set_state(PAUSED)
            self.log("Paused.")
        elif self.state_name == PAUSED:
            self.session.resume()
            self._set_state(RECORDING)
            self.log("Resumed.")

    def on_stop(self):
        if self.state_name not in (RECORDING, PAUSED) or not self.session:
            return
        session, self.session = self.session, None
        self._set_state(FINISHING)
        self.root.deiconify()
        self.log("Stopping - writing the files, please wait...")

        def worker():
            try:
                session.stop()
                outputs = session.finalize(progress=self.log)
                self.ui_queue.put(lambda: self._finished(outputs, session.errors))
            except Exception as exc:
                session.cleanup()
                self.ui_queue.put(lambda error=exc: self._failed(error))

        threading.Thread(target=worker, daemon=True).start()

    def _finished(self, outputs, errors):
        self._set_state(IDLE)
        self.timer_label.configure(text="00:00:00")
        self.system_meter["value"] = 0
        self.mic_meter["value"] = 0
        if outputs:
            self.log("Saved:")
            for path in outputs:
                self.log("   %s" % path)
        else:
            self.log("Nothing was saved.")
        if errors:
            self.log("Finished with warnings:")
            for item in errors:
                self.log("   %s" % item)

    # -- main loop --------------------------------------------------------
    def _poll(self):
        while True:
            try:
                self.ui_queue.get_nowait()()
            except queue.Empty:
                break
            except Exception as exc:
                self._append_log("Internal error: %s" % exc)

        levels = {}
        if self.session and self.state_name in (RECORDING, PAUSED):
            self.timer_label.configure(text=human_time(self.session.elapsed))
            levels = self.session.levels()
        elif self._preview:
            levels = {stream.label: stream.level for stream in self._preview[0]}

        self.system_meter["value"] = min(100, levels.get("system", 0.0) * 100 * 1.6)
        self.mic_meter["value"] = min(100, levels.get("mic", 0.0) * 100 * 1.6)
        self.root.after(120, self._poll)

    def on_close(self):
        if self.state_name in (RECORDING, PAUSED):
            if not messagebox.askyesno(APP_NAME, "A recording is running. Stop it and quit?"):
                return
            self.on_stop()
            return
        if self.state_name == FINISHING:
            messagebox.showinfo(APP_NAME, "Still writing the files - one moment.")
            return
        if self._preview:
            self._stop_level_test()
        try:
            self.collect_settings()
        except Exception:
            pass
        self.root.destroy()


def main():
    video.enable_dpi_awareness()
    root = tk.Tk()
    root.title("%s %s" % (APP_NAME, __version__))
    root.minsize(880, 620)
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    RecorderApp(root)
    root.mainloop()
