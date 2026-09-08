"""Persisted user settings."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

CONFIG_NAME = "ScreenRecorder"


def config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, CONFIG_NAME)
    os.makedirs(path, exist_ok=True)
    return path


CONFIG_PATH = os.path.join(config_dir(), "settings.json")

QUALITY_CRF = {"High": 20, "Medium": 24, "Low": 28}


def default_output_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Videos", "ScreenRecorder")


@dataclass
class Settings:
    output_dir: str = field(default_factory=default_output_dir)
    file_prefix: str = "Meeting"
    save_mp4: bool = True
    save_mp3: bool = True

    # capture source: "monitor:<n>" | "all" | "region"
    # One entry per capture area, each recorded to its own file:
    # "monitor:1", "monitor:2", ... | "all" (every screen in one frame) | "region"
    sources: list = field(default_factory=lambda: ["monitor:1"])
    region: list = field(default_factory=lambda: [0, 0, 1280, 720])
    fps: int = 25
    quality: str = "High"
    show_cursor: bool = True

    # audio
    record_system: bool = True          # what you hear (other participants)
    record_mic: bool = True             # what you say
    system_device: str = ""             # device name, empty = default
    mic_device: str = ""
    system_gain: float = 1.0
    mic_gain: float = 1.0

    minimize_on_start: bool = True

    @classmethod
    def load(cls) -> "Settings":
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, indent=2)
        except OSError:
            pass
