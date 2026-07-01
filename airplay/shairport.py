import asyncio
import base64
import logging
import math
import os
import re
import shutil
import signal
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

import config

log = logging.getLogger("airplay")

_CONNECTION_RE = re.compile(r"(?:connection from|new connection from|client[: ]+)([^,\n]+)", re.I)


@dataclass
class AirPlayEvent:
    kind: str
    device_name: str | None = None
    volume: int | None = None
    level: int | None = None
    raw: str = ""


class ShairportManager:
    def __init__(self, queue: asyncio.Queue[AirPlayEvent]):
        self.queue = queue
        self.process: asyncio.subprocess.Process | None = None
        self._metadata_task: asyncio.Task | None = None
        self._event_hook_task: asyncio.Task | None = None
        self._audio_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._aplay_process: subprocess.Popen | None = None
        self._logged_pcm_level = False
        self._running = False

    async def start(self):
        config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        self._prepare_fifo(config.METADATA_FIFO_PATH)
        self._prepare_fifo(config.EVENT_FIFO_PATH)
        self._prepare_fifo(config.PCM_FIFO_PATH)
        self._write_event_hook(config.EVENT_HOOK_PATH)
        self._write_config(config.SHAIRPORT_CONFIG_PATH)
        self._kill_stale_instances()
        self._running = True
        self._event_hook_task = asyncio.create_task(self._event_hook_loop())
        if config.SHAIRPORT_OUTPUT_BACKEND == "pipe":
            self._start_aplay()
            self._audio_task = asyncio.create_task(self._audio_loop())
        self.process = await asyncio.create_subprocess_exec(
            config.SHAIRPORT_BIN,
            "-c",
            str(config.SHAIRPORT_CONFIG_PATH),
            *shlex.split(config.SHAIRPORT_EXTRA_ARGS),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._metadata_task = asyncio.create_task(self._metadata_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        log.info("started shairport-sync pid=%s name=%s output=%s", self.process.pid, config.AIRPLAY_NAME, config.ALSA_OUTPUT_DEVICE)

    async def stop(self):
        self._running = False
        for task in (self._metadata_task, self._event_hook_task, self._audio_task, self._stderr_task):
            if task:
                task.cancel()
        self._poke_fifo(config.METADATA_FIFO_PATH)
        self._poke_fifo(config.EVENT_FIFO_PATH)
        self._poke_fifo(config.PCM_FIFO_PATH)
        if self.process and self.process.returncode is None:
            self._send_process_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._send_process_signal(signal.SIGKILL)
                await self.process.wait()
        self._stop_aplay()

    async def wait(self):
        if not self.process:
            return
        code = await self.process.wait()
        if self._running:
            await self.queue.put(AirPlayEvent("error", raw=f"shairport-sync exited with {code}"))

    def _prepare_fifo(self, path: Path):
        if path.exists():
            if path.is_fifo():
                return
            path.unlink()
        os.mkfifo(path)

    def _poke_fifo(self, path: Path):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return
        try:
            os.write(fd, b"\n")
        except OSError:
            pass
        finally:
            os.close(fd)

    def _write_event_hook(self, path: Path):
        path.write_text(
            f"""#!/usr/bin/env sh
printf '%s\\n' "$1" > "{_escape(str(config.EVENT_FIFO_PATH))}"
""",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _write_config(self, path: Path):
        if config.SHAIRPORT_CONFIG_TEMPLATE:
            shutil.copyfile(config.SHAIRPORT_CONFIG_TEMPLATE, path)
            return
        mixer_lines = ""
        if config.ALSA_MIXER_CONTROL:
            mixer_lines += f'    mixer_control_name = "{_escape(config.ALSA_MIXER_CONTROL)}";\n'
        if config.ALSA_MIXER_DEVICE:
            mixer_lines += f'    mixer_device = "{_escape(config.ALSA_MIXER_DEVICE)}";\n'
        path.write_text(
            f"""
general =
{{
    name = "{_escape(config.AIRPLAY_NAME)}";
    output_backend = "{_escape(config.SHAIRPORT_OUTPUT_BACKEND)}";
    interpolation = "{_escape(config.SHAIRPORT_INTERPOLATION)}";
    ignore_volume_control = "{'yes' if config.SHAIRPORT_IGNORE_VOLUME_CONTROL else 'no'}";
    audio_backend_buffer_desired_length_in_seconds = {config.SHAIRPORT_BUFFER_SECONDS};
    audio_backend_buffer_interpolation_threshold_in_seconds = {config.SHAIRPORT_BUFFER_THRESHOLD_SECONDS};
}};

diagnostics =
{{
    log_verbosity = {config.SHAIRPORT_LOG_LEVEL};
}};

sessioncontrol =
{{
    run_this_before_entering_active_state = "{_escape(str(config.EVENT_HOOK_PATH))} connect";
    run_this_after_exiting_active_state = "{_escape(str(config.EVENT_HOOK_PATH))} disconnect";
    run_this_before_play_begins = "{_escape(str(config.EVENT_HOOK_PATH))} play";
    run_this_after_play_ends = "{_escape(str(config.EVENT_HOOK_PATH))} stop";
}};

alsa =
{{
    output_device = "{_escape(config.ALSA_OUTPUT_DEVICE)}";
{mixer_lines.rstrip()}
    output_rate = {_shairport_number_or_string(config.SHAIRPORT_OUTPUT_RATE)};
    output_format = "{_escape(config.SHAIRPORT_OUTPUT_FORMAT)}";
    use_precision_timing = "no";
}};

pipe =
{{
    name = "{_escape(str(config.PCM_FIFO_PATH))}";
}};

metadata =
{{
    enabled = "yes";
    include_cover_art = "no";
    pipe_name = "{_escape(str(config.METADATA_FIFO_PATH))}";
    pipe_timeout = 5000;
}};
""".strip()
            + "\n",
            encoding="utf-8",
        )

    def _kill_stale_instances(self):
        target_config = str(config.SHAIRPORT_CONFIG_PATH)
        current_pid = os.getpid()
        for proc_dir in Path("/proc").iterdir() if Path("/proc").exists() else []:
            if not proc_dir.name.isdigit():
                continue
            pid = int(proc_dir.name)
            if pid == current_pid:
                continue
            try:
                cmdline = (proc_dir / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
            except OSError:
                continue
            if "shairport-sync" not in cmdline or target_config not in cmdline:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                log.info("terminated stale shairport-sync pid=%s", pid)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                log.warning("failed to terminate stale shairport-sync pid=%s: %s", pid, exc)

    def _send_process_signal(self, sig: signal.Signals):
        if not self.process:
            return
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                self.process.send_signal(sig)
            except ProcessLookupError:
                pass

    async def _metadata_loop(self):
        while self._running:
            try:
                await asyncio.to_thread(self._read_fifo_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("metadata read failed: %s", exc)
                await asyncio.sleep(0.5)

    async def _event_hook_loop(self):
        while self._running:
            try:
                await asyncio.to_thread(self._read_event_fifo_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("event hook read failed: %s", exc)
                await asyncio.sleep(0.5)

    def _start_aplay(self):
        cmd = [
            "aplay",
            "-q",
            "-t",
            "raw",
            "-D",
            config.ALSA_OUTPUT_DEVICE,
            "-f",
            "S16_LE",
            "-r",
            str(config.SHAIRPORT_OUTPUT_RATE),
            "-c",
            "2",
        ]
        self._aplay_process = subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=0)

    def _stop_aplay(self):
        if not self._aplay_process:
            return
        process = self._aplay_process
        self._aplay_process = None
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    async def _audio_loop(self):
        while self._running:
            try:
                await asyncio.to_thread(self._read_audio_fifo_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("audio fifo read failed: %s", exc)
                await asyncio.sleep(0.2)

    def _read_audio_fifo_once(self):
        last_level = -1
        last_emit = 0.0
        with open(config.PCM_FIFO_PATH, "rb", buffering=0) as handle:
            while self._running:
                chunk = handle.read(8192)
                if not chunk:
                    return
                process = self._aplay_process
                if process and process.stdin and process.poll() is None:
                    try:
                        process.stdin.write(chunk)
                    except (BrokenPipeError, OSError):
                        self._stop_aplay()
                        self._start_aplay()
                level = _pcm_level(chunk)
                if level > 0 and not self._logged_pcm_level:
                    self._logged_pcm_level = True
                    log.info("detected live PCM audio level=%s", level)
                now = time.monotonic()
                if level != last_level and now - last_emit >= 0.05:
                    self.queue.put_nowait(AirPlayEvent("level", level=level, raw="pcm"))
                    last_level = level
                    last_emit = now

    def _read_event_fifo_once(self):
        with open(config.EVENT_FIFO_PATH, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if not self._running:
                    return
                text = line.strip().lower()
                if text == "play":
                    self._logged_pcm_level = False
                    self.queue.put_nowait(AirPlayEvent("play", device_name="AirPlay", raw=text))
                elif text == "stop":
                    self.queue.put_nowait(AirPlayEvent("stop", raw=text))
                elif text == "connect":
                    self.queue.put_nowait(AirPlayEvent("device", device_name="AirPlay", raw=text))
                elif text == "disconnect":
                    log.debug("ignoring Shairport active-state exit as a disconnect")

    def _read_fifo_once(self):
        with open(config.METADATA_FIFO_PATH, "r", encoding="utf-8", errors="ignore") as handle:
            buffer = []
            for line in handle:
                if not self._running:
                    return
                text = line.strip()
                if not text:
                    continue
                buffer.append(text)
                if text == "</item>" or len(buffer) > 20:
                    event = self._parse_metadata("\n".join(buffer))
                    buffer.clear()
                    if event:
                        self.queue.put_nowait(event)

    async def _stderr_loop(self):
        if not self.process or not self.process.stderr:
            return
        while self._running:
            line = await self.process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="ignore").strip()
            if not text:
                continue
            if _is_important_log_line(text):
                log.info("shairport-sync: %s", text)
            else:
                log.debug("shairport-sync: %s", text)
            event = self._parse_text_event(text)
            if event:
                await self.queue.put(event)

    def _parse_metadata(self, text: str) -> AirPlayEvent | None:
        code = _decode_code(self._xml_field(text, "code"))
        payload = self._xml_field(text, "data")
        decoded = self._decode_payload(payload)
        sample = decoded or payload or text
        if code:
            log.debug("metadata code=%s sample=%s", code, _shorten(sample))

        if code in {"pbeg", "pbgn", "pend", "pvol", "paus", "prsm"}:
            if code in {"pbeg", "pbgn", "prsm"}:
                return AirPlayEvent("play", raw=text)
            if code in {"pend", "paus"}:
                return AirPlayEvent("stop", raw=text)
            volume = self._volume_from_text(sample)
            if volume is not None:
                return AirPlayEvent("volume", volume=volume, raw=text)

        if code in {"snam"} and decoded:
            return AirPlayEvent("device", device_name=decoded.strip(), raw=text)

        return self._parse_text_event(sample)

    def _xml_field(self, text: str, tag: str) -> str:
        try:
            root = ElementTree.fromstring(text)
            node = root.find(tag)
            return node.text.strip() if node is not None and node.text else ""
        except ElementTree.ParseError:
            return ""

    def _decode_payload(self, payload: str) -> str:
        if not payload:
            return ""
        try:
            return base64.b64decode(payload).decode("utf-8", errors="ignore").strip("\x00\r\n ")
        except Exception:
            return payload

    def _parse_text_event(self, text: str) -> AirPlayEvent | None:
        lower = text.lower()
        if (
            "play begin" in lower
            or "play begins" in lower
            or "start playing" in lower
            or "player_play" in lower
            or "abeg" in lower
            or "am_state: am_active" in lower
        ):
            return AirPlayEvent("play", raw=text)
        if (
            "play ends" in lower
            or "play end" in lower
            or "aend" in lower
            or "connection closed" in lower
            or "closed connection" in lower
            or "am_state: am_inactive" in lower
        ):
            return AirPlayEvent("stop", raw=text)

        if "connection from" in lower or "play connection from" in lower:
            return AirPlayEvent("device", device_name="AirPlay", raw=text)

        match = _CONNECTION_RE.search(text)
        if match:
            name = _clean_connection_name(match.group(1).strip())
            return AirPlayEvent("device", device_name=name, raw=text)

        if "software attenuation" in lower or "loudness gain" in lower:
            return None
        volume = self._volume_from_text(text)
        if volume is not None:
            return AirPlayEvent("volume", volume=volume, raw=text)
        return None

    def _volume_from_text(self, text: str) -> int | None:
        lower = text.lower()
        if "volume mode" in lower or "software attenuation" in lower or "hardware_attenuation" in lower:
            return None
        match = re.search(r"airplay volume(?: is|:)?\s*(?P<value>-?\d+(?:\.\d+)?)", text, re.I)
        if not match:
            match = re.search(r"set initial volume to\s*(?P<value>-?\d+(?:\.\d+)?)", text, re.I)
        if not match:
            match = re.search(r"\bvolume:\s*(?P<value>-?\d+(?:\.\d+)?)\s*dB\b", text, re.I)
        if not match:
            return None
        value = float(match.group("value"))
        if value <= 0 and value >= -30:
            return max(0, min(100, round((value + 30) / 30 * 100)))
        return max(0, min(100, round(value)))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _shairport_number_or_string(value: str) -> str:
    text = str(value).strip()
    if text.isdigit():
        return text
    return f'"{_escape(text)}"'


def _decode_code(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in text):
        try:
            decoded = bytes.fromhex(text).decode("ascii", errors="ignore").strip("\x00")
            if decoded:
                return decoded
        except ValueError:
            pass
    return text


def _shorten(value: str, limit: int = 120) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _clean_connection_name(value: str) -> str:
    text = value.split(" to self", 1)[0].strip()
    if ":" in text and all(part for part in text.replace(".", ":").split(":")):
        return "AirPlay"
    return text or "AirPlay"


def _pcm_level(chunk: bytes) -> int:
    if len(chunk) < 2:
        return 0
    even = len(chunk) - (len(chunk) % 2)
    samples = memoryview(chunk[:even]).cast("h")
    if not samples:
        return 0
    total = 0
    for sample in samples:
        total += sample * sample
    rms = math.sqrt(total / len(samples)) / 32768.0
    if rms <= 0:
        return 0
    level = int(min(100, max(0, (math.log10(1 + rms * 28) / math.log10(29)) * 100)))
    return level


def _is_important_log_line(text: str) -> bool:
    lower = text.lower()
    keywords = (
        "error",
        "warning",
        "fatal",
        "alsa",
        "volume",
        "play begin",
        "play ends",
        "connection from",
        "closed connection",
        "packet out of sequence",
        "dropping",
    )
    return any(keyword in lower for keyword in keywords)
