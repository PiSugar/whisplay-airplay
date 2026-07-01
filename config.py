import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _int(key: str, default: str) -> int:
    try:
        return int(_get(key, default))
    except ValueError:
        return int(default)


def _bool(key: str, default: str) -> bool:
    return _get(key, default).lower() in {"1", "true", "yes", "on"}


def _read_alsa_cards() -> list[str]:
    try:
        with open("/proc/asound/cards", "r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()
    except OSError:
        return []

    cards: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or not stripped[0].isdigit():
            continue
        parts = stripped.split("[", 1)
        if len(parts) != 2:
            continue
        card_name = parts[1].split("]", 1)[0].strip()
        if card_name:
            cards.append(card_name)
    return cards


def _detect_whisplay_card() -> str | None:
    preferred_cards = [
        "whisplaysound",
        "wm8960soundcard",
        "ES8388Audio",
        "ES8389Audio",
    ]
    available = set(_read_alsa_cards())
    for card_name in preferred_cards:
        if card_name in available:
            return card_name
    return None


def _default_alsa_output() -> str:
    card_name = _detect_whisplay_card()
    if not card_name:
        return "default"
    if card_name == "whisplaysound":
        return "playback"
    return f"plughw:CARD={card_name}"


APP_ID = _get("WHISPLAY_AIRPLAY_APP_ID", "whisplay-airplay")
APP_NAME = _get("WHISPLAY_AIRPLAY_APP_NAME", "AirPlay")
APP_ICON = _get("WHISPLAY_AIRPLAY_APP_ICON", "AP")

AIRPLAY_NAME = _get("WHISPLAY_AIRPLAY_NAME", os.uname().nodename or "Whisplay AirPlay")
SHAIRPORT_BIN = _get("SHAIRPORT_BIN", shutil.which("shairport-sync") or "shairport-sync")
SHAIRPORT_CONFIG_TEMPLATE = _get("SHAIRPORT_CONFIG_TEMPLATE")
SHAIRPORT_LOG_LEVEL = _int("SHAIRPORT_LOG_LEVEL", "1")
SHAIRPORT_LATENCY_OFFSET = _int("SHAIRPORT_LATENCY_OFFSET", "0")
SHAIRPORT_EXTRA_ARGS = _get("SHAIRPORT_EXTRA_ARGS")
SHAIRPORT_IGNORE_VOLUME_CONTROL = _bool("SHAIRPORT_IGNORE_VOLUME_CONTROL", "false")
SHAIRPORT_INTERPOLATION = _get("SHAIRPORT_INTERPOLATION", "basic")
SHAIRPORT_OUTPUT_RATE = _get("SHAIRPORT_OUTPUT_RATE", "44100")
SHAIRPORT_OUTPUT_FORMAT = _get("SHAIRPORT_OUTPUT_FORMAT", "S16")
SHAIRPORT_OUTPUT_BACKEND = _get("SHAIRPORT_OUTPUT_BACKEND", "pipe")
SHAIRPORT_BUFFER_SECONDS = _get("SHAIRPORT_BUFFER_SECONDS", "1.0")
SHAIRPORT_BUFFER_THRESHOLD_SECONDS = _get("SHAIRPORT_BUFFER_THRESHOLD_SECONDS", "0.35")

ALSA_OUTPUT_DEVICE = _get("ALSA_OUTPUT_DEVICE", _default_alsa_output())
ALSA_MIXER_DEVICE = _get("ALSA_MIXER_DEVICE", "")
ALSA_MIXER_CONTROL = _get("ALSA_MIXER_CONTROL", "")
SET_SYSTEM_VOLUME = _bool("WHISPLAY_AIRPLAY_SET_SYSTEM_VOLUME", "false")

RUNTIME_DIR = Path(_get("WHISPLAY_AIRPLAY_RUNTIME_DIR", str(BASE_DIR / "runtime")))
SHAIRPORT_CONFIG_PATH = RUNTIME_DIR / "shairport-sync.conf"
METADATA_FIFO_PATH = RUNTIME_DIR / "shairport-sync-metadata"
EVENT_FIFO_PATH = RUNTIME_DIR / "shairport-sync-events"
EVENT_HOOK_PATH = RUNTIME_DIR / "airplay-event-hook.sh"
PCM_FIFO_PATH = RUNTIME_DIR / "shairport-sync-audio"

LCD_BRIGHTNESS = _int("LCD_BRIGHTNESS", "100")
FONT_PATH = _get("FONT_PATH")
DISPLAY_FPS = _int("DISPLAY_FPS", "30")

PISUGAR_ENABLED = _bool("PISUGAR_ENABLED", "true")
PISUGAR_HOST = _get("PISUGAR_HOST", "127.0.0.1")
PISUGAR_PORT = _int("PISUGAR_PORT", "8423")
BATTERY_POLL_INTERVAL = _int("BATTERY_POLL_INTERVAL", "5")
NETWORK_POLL_INTERVAL = _int("NETWORK_POLL_INTERVAL", "10")

DAEMON_SOCKET_PATH = _get("WHISPLAY_DAEMON_SOCKET_PATH", "/tmp/whisplay-daemon.sock")
