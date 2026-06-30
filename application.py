import asyncio
import logging
import subprocess

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
        self._stop_event = asyncio.Event()
        self._device_name = ""
        self._is_playing = False
        self._volume = -1

    async def start(self):
        self.board = create_whisplay_hardware()
        if hasattr(self.board, "on_exit_request"):
            self.board.on_exit_request(lambda: self._stop_event.set())
        self.board.set_rgb(0, 0, 40)

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
        self._process_task = asyncio.create_task(self.airplay.wait())

    async def stop(self):
        for task in (self._event_task, self._display_task, self._process_task):
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

    def _handle_airplay_event(self, event: AirPlayEvent):
        log.info("airplay event kind=%s device=%s volume=%s", event.kind, event.device_name, event.volume)
        if event.kind == "device" and event.device_name:
            self._device_name = self._clean_device_name(event.device_name)
            self._set_led_connected()
        elif event.kind == "play":
            self._is_playing = True
            if not self._device_name:
                self._device_name = "AirPlay"
            self._set_led_connected()
        elif event.kind == "stop":
            self._is_playing = False
            self._set_led_idle()
        elif event.kind == "volume" and event.volume is not None:
            self._volume = event.volume
            self._apply_system_volume(event.volume)
        elif event.kind == "error":
            self._is_playing = False
            self._update_display(status="AirPlay Error")
            self.board.set_rgb(80, 0, 0)
            log.error("airplay error: %s", event.raw)
            self._stop_event.set()
        self._update_display()

    def _update_display(self, **overrides):
        if not self.display:
            return
        status = overrides.get("status")
        if not status:
            if self._is_playing:
                status = "Playing"
            elif self._device_name:
                status = "Connected"
            else:
                status = "Waiting"
        self.display.update(
            status=status,
            device_name=overrides.get("device_name", self._device_name),
            volume=overrides.get("volume", self._volume),
            is_playing=overrides.get("is_playing", self._is_playing),
            battery_level=self.battery.level,
            battery_color=self.battery.get_color(),
            wifi_signal_level=self.network.signal_level,
        )

    def _set_led_connected(self):
        if self.board:
            self.board.set_rgb(0, 36, 70)

    def _set_led_idle(self):
        if self.board:
            self.board.set_rgb(0, 0, 40)

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
        if text.startswith("[") and "]" in text:
            text = text.split("]", 1)[1].strip()
        return text[:40]
