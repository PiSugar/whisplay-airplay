import asyncio
import logging
import re
import socket
import subprocess
import time

import config
from airplay import AirPlayEvent, ShairportManager
from display import UIRenderer
from hardware.battery import BatteryMonitor
from hardware.network import NetworkMonitor
from hardware.whisplay_daemon import create_whisplay_hardware

log = logging.getLogger("app")


class WhisplayAirPlayApp:
    def __init__(self):
        self.board = None
        self.display: UIRenderer | None = None
        self.battery = BatteryMonitor()
        self.network = NetworkMonitor(config.NETWORK_POLL_INTERVAL)
        self.events: asyncio.Queue[AirPlayEvent] = asyncio.Queue()
        self.airplay = ShairportManager(self.events)
        self._event_task: asyncio.Task | None = None
        self._display_task: asyncio.Task | None = None
        self._process_task: asyncio.Task | None = None
        self._connection_task: asyncio.Task | None = None
        self._pending_disconnect_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._connected = False
        self._device_name = ""
        self._is_playing = False
        self._played_since_connect = False
        self._last_stop_at = 0.0
        self._volume = -1
        self._audio_level = 0

    async def start(self):
        self.board = create_whisplay_hardware()
        if hasattr(self.board, "on_exit_request"):
            self.board.on_exit_request(lambda: self._stop_event.set())
        self._set_led_idle()

        self.display = UIRenderer(self.board, font_path=config.FONT_PATH, fps=config.DISPLAY_FPS)
        self.display.start()
        self._update_display(status="Waiting", device_name="", volume=self._volume, is_playing=False)

        await self.battery.start()
        await self.network.start()
        try:
            await self.airplay.start()
        except FileNotFoundError:
            log.error("shairport-sync not found; install it with: sudo apt install shairport-sync")
            self._update_display(status="Install shairport", device_name="sudo apt install", volume=-1, is_playing=False)
            self.board.set_rgb(80, 0, 0)
            return

        self._event_task = asyncio.create_task(self._event_loop())
        self._display_task = asyncio.create_task(self._display_loop())
        self._connection_task = asyncio.create_task(self._connection_loop())
        self._process_task = asyncio.create_task(self.airplay.wait())

    async def stop(self):
        for task in (
            self._event_task,
            self._display_task,
            self._connection_task,
            self._process_task,
            self._pending_disconnect_task,
        ):
            if task:
                task.cancel()
        await self.airplay.stop()
        await self.network.stop()
        await self.battery.stop()
        if self.display:
            self.display.stop()
        if self.board:
            self.board.set_rgb(0, 0, 0)
            if hasattr(self.board, "cleanup"):
                self.board.cleanup()

    async def wait(self):
        await self._stop_event.wait()

    async def _event_loop(self):
        while True:
            event = await self.events.get()
            self._handle_airplay_event(event)

    async def _display_loop(self):
        while True:
            self._update_display()
            await asyncio.sleep(1)

    async def _connection_loop(self):
        was_connected = False
        while True:
            peer_ip = await asyncio.to_thread(_airplay_tcp_peer_ip)
            connected = bool(peer_ip)
            if connected != was_connected:
                was_connected = connected
                if connected:
                    device_name = await asyncio.to_thread(_resolve_peer_name, peer_ip)
                    self.events.put_nowait(AirPlayEvent("device", device_name=device_name or "AirPlay", raw="tcp-connected"))
                else:
                    self.events.put_nowait(AirPlayEvent("disconnect", raw="tcp-disconnected"))
            await asyncio.sleep(0.5)

    def _handle_airplay_event(self, event: AirPlayEvent):
        if event.kind == "level":
            log.debug("airplay event kind=level level=%s", event.level)
        else:
            log.info("airplay event kind=%s device=%s volume=%s", event.kind, event.device_name, event.volume)
        if event.kind == "device" and event.device_name:
            self._cancel_pending_disconnect()
            self._connected = True
            clean_name = self._clean_device_name(event.device_name)
            if self._is_friendly_device_name(clean_name):
                self._device_name = clean_name
            self._set_led_connected()
        elif event.kind == "play":
            self._cancel_pending_disconnect()
            self._connected = True
            self._is_playing = True
            self._played_since_connect = True
            self._set_led_playing()
        elif event.kind == "stop":
            self._is_playing = False
            self._last_stop_at = time.monotonic()
            self._audio_level = 0
            if self._connected:
                self._set_led_connected()
            else:
                self._set_led_idle()
        elif event.kind == "disconnect":
            if event.raw == "tcp-disconnected" and self._should_defer_tcp_disconnect():
                log.info("deferring AirPlay disconnect after playback TCP stream closed")
                self._is_playing = False
                self._audio_level = 0
                if self._connected:
                    self._set_led_connected()
                else:
                    self._set_led_idle()
                self._schedule_pending_disconnect()
                self._update_display()
                return
            self._clear_connection()
        elif event.kind == "volume" and event.volume is not None:
            self._volume = event.volume
            self._apply_system_volume(event.volume)
        elif event.kind == "level" and event.level is not None:
            self._audio_level = event.level
        elif event.kind == "error":
            self._is_playing = False
            self._update_display(status="AirPlay Error")
            self.board.set_rgb(80, 0, 0)
            log.error("airplay error: %s", event.raw)
            self._stop_event.set()
        self._update_display()

    def _should_defer_tcp_disconnect(self) -> bool:
        if not self._played_since_connect:
            return False
        return time.monotonic() - self._last_stop_at <= 3.0

    def _schedule_pending_disconnect(self):
        self._cancel_pending_disconnect()
        self._pending_disconnect_task = asyncio.create_task(self._delayed_disconnect())

    async def _delayed_disconnect(self):
        await asyncio.sleep(6.0)
        if self._is_playing:
            return
        log.info("clearing stale AirPlay connection after disconnect grace period")
        self._clear_connection()
        self._update_display()

    def _cancel_pending_disconnect(self):
        current = asyncio.current_task()
        if self._pending_disconnect_task and self._pending_disconnect_task is not current and not self._pending_disconnect_task.done():
            self._pending_disconnect_task.cancel()
        self._pending_disconnect_task = None

    def _clear_connection(self):
        self._cancel_pending_disconnect()
        self._is_playing = False
        self._connected = False
        self._device_name = ""
        self._played_since_connect = False
        self._last_stop_at = 0.0
        self._audio_level = 0
        self._set_led_idle()

    def _update_display(self, **overrides):
        if not self.display:
            return
        status = overrides.get("status")
        if not status:
            if self._is_playing:
                status = "Playing"
            elif self._connected:
                status = "Connected"
            else:
                status = "Waiting"
        self.display.update(
            status=status,
            device_name=overrides.get("device_name", self._device_name or ("Connected" if self._connected else "")),
            volume=overrides.get("volume", self._volume),
            audio_level=overrides.get("audio_level", self._audio_level),
            is_playing=overrides.get("is_playing", self._is_playing),
            battery_level=self.battery.level,
            battery_color=self.battery.get_color(),
            wifi_signal_level=self.network.signal_level,
        )

    def _set_led_connected(self):
        if self.board:
            self.board.set_rgb(0, 0, 80)

    def _set_led_playing(self):
        if self.board:
            self.board.set_rgb(55, 150, 255)

    def _set_led_idle(self):
        if self.board:
            self.board.set_rgb(0, 0, 0)

    def _apply_system_volume(self, volume: int):
        if not config.SET_SYSTEM_VOLUME:
            return
        cmd = [
            "amixer",
            "-D",
            config.ALSA_MIXER_DEVICE,
            "sset",
            config.ALSA_MIXER_CONTROL,
            f"{max(0, min(100, volume))}%",
        ]
        try:
            subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def _clean_device_name(self, name: str) -> str:
        text = name.strip()
        if not text:
            return ""
        text = text.replace(".local", "")
        text = text.replace(".lan", "")
        if text.startswith("[") and "]" in text:
            text = text.split("]", 1)[1].strip()
        return text[:40]

    def _is_friendly_device_name(self, name: str) -> bool:
        text = name.strip()
        if not text or text.lower() in {"airplay", "unknown", "connected"}:
            return False
        if re.fullmatch(r"[0-9a-fA-F]{8,}(?:-[0-9a-fA-F]{4,}){1,}", text):
            return False
        if re.fullmatch(r"[0-9a-fA-F]{12,}", text):
            return False
        if re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", text):
            return False
        if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?", text):
            return False
        if re.fullmatch(r"[0-9a-fA-F:.]{8,}", text):
            return False
        return True


def _airplay_tcp_peer_ip() -> str:
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 4:
                continue
            local = parts[1]
            remote = parts[2]
            state = parts[3]
            if state != "01":
                continue
            try:
                port = int(local.rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            if port == 5000:
                peer_ip = _decode_proc_net_ip(remote.rsplit(":", 1)[0])
                if peer_ip and peer_ip not in {"0.0.0.0", "::"}:
                    return peer_ip
    return ""


def _decode_proc_net_ip(value: str) -> str:
    if len(value) == 8:
        try:
            raw = bytes.fromhex(value)
            return socket.inet_ntoa(raw[::-1])
        except OSError:
            return ""
    if len(value) == 32:
        try:
            raw = bytes.fromhex(value)
            words = [raw[i:i + 4][::-1] for i in range(0, 16, 4)]
            return socket.inet_ntop(socket.AF_INET6, b"".join(words))
        except OSError:
            return ""
    return ""


def _resolve_peer_name(peer_ip: str) -> str:
    if not peer_ip:
        return ""
    for candidate in (_reverse_dns_name(peer_ip), _getent_name(peer_ip)):
        if candidate:
            return candidate
    return ""


def _reverse_dns_name(peer_ip: str) -> str:
    try:
        name, _, _ = socket.gethostbyaddr(peer_ip)
    except OSError:
        return ""
    return name


def _getent_name(peer_ip: str) -> str:
    try:
        result = subprocess.run(
            ["getent", "hosts", peer_ip],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    fields = first_line.split()
    return fields[1] if len(fields) > 1 else ""
