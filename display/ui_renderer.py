import logging
import math
import os
import threading
import time

from PIL import Image, ImageDraw, ImageFont

from display.text_utils import fit_text, image_to_rgb565, luminance, text_size

log = logging.getLogger("display")

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
_WIFI_LEVEL_ICONS = {
    1: "wifi-weak.png",
    2: "wifi-medium.png",
    3: "wifi-strong.png",
}


def _find_font(custom_path: str = "") -> str:
    candidates = [
        custom_path,
        os.path.join(_ASSETS_DIR, "NotoSansSC-Bold.ttf"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return ""


class DisplayState:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "Starting"
        self.device_name = ""
        self.volume = -1
        self.audio_level = 0
        self.is_playing = False
        self.battery_level = -1
        self.battery_color = (128, 128, 128)
        self.wifi_signal_level = 0
        self.version = 0

    def update(self, **kwargs):
        with self.lock:
            for key, value in kwargs.items():
                if value is not None and hasattr(self, key) and getattr(self, key) != value:
                    setattr(self, key, value)
                    self.version += 1

    def snapshot(self):
        with self.lock:
            return {
                "status": self.status,
                "device_name": self.device_name,
                "volume": self.volume,
                "audio_level": self.audio_level,
                "is_playing": self.is_playing,
                "battery_level": self.battery_level,
                "battery_color": self.battery_color,
                "wifi_signal_level": self.wifi_signal_level,
                "version": self.version,
            }


class UIRenderer(threading.Thread):
    def __init__(self, board, font_path: str = "", fps: int = 20):
        super().__init__(daemon=True)
        self.board = board
        self.fps = max(1, fps)
        self.running = False
        self.state = DisplayState()
        self._animation_started_at = time.monotonic()
        self._last_frame_at = self._animation_started_at
        self._display_level = 0.0
        self._level_history = [0.0] * 13
        self._base_image: Image.Image | None = None
        self._base_key: tuple | None = None
        resolved = _find_font(font_path)
        if resolved:
            self._title_font = ImageFont.truetype(resolved, 18)
            self._device_font = ImageFont.truetype(resolved, 20)
            self._small_font = ImageFont.truetype(resolved, 12)
            self._battery_font = ImageFont.truetype(resolved, 12)
        else:
            self._title_font = ImageFont.load_default()
            self._device_font = ImageFont.load_default()
            self._small_font = ImageFont.load_default()
            self._battery_font = ImageFont.load_default()
        self._wifi_cache: dict[int, Image.Image | None] = {}

    def update(self, **kwargs):
        self.state.update(**kwargs)

    def run(self):
        self.running = True
        self.board.set_backlight(100)
        interval = 1.0 / self.fps
        last_version = -1
        while self.running:
            start = time.time()
            try:
                snap = self.state.snapshot()
                if snap["version"] != last_version or snap["is_playing"]:
                    self._render_frame(snap)
                    last_version = snap["version"]
            except Exception as exc:
                log.error("render error: %s", exc)
            delay = interval - (time.time() - start)
            if delay > 0:
                time.sleep(delay)

    def stop(self):
        self.running = False

    def _render_frame(self, snap: dict | None = None):
        snap = snap or self.state.snapshot()
        width, height = self.board.LCD_WIDTH, self.board.LCD_HEIGHT
        base_key = self._base_cache_key(snap)
        base_changed = base_key != self._base_key or self._base_image is None
        if base_changed:
            self._base_image = self._render_base_image(snap, width, height)
            self._base_key = base_key

        if snap["is_playing"] and not base_changed:
            self._draw_meter_region(snap, width, height)
        else:
            self._draw_full_frame(snap, width, height)

    def _base_cache_key(self, snap: dict) -> tuple:
        return (
            snap["status"],
            snap["device_name"],
            snap["is_playing"],
            snap["battery_level"],
            snap["battery_color"],
            snap["wifi_signal_level"],
        )

    def _render_base_image(self, snap: dict, width: int, height: int) -> Image.Image:
        image = Image.new("RGBA", (width, height), (0, 0, 0, 255))
        draw = ImageDraw.Draw(image)
        self._draw_header(image, draw, snap, width)
        self._draw_center(draw, snap, width, height)
        return image

    def _draw_full_frame(self, snap: dict, width: int, height: int):
        image = self._base_image.copy() if self._base_image else self._render_base_image(snap, width, height)
        draw = ImageDraw.Draw(image)
        self._draw_sound_meter(draw, snap, width, height)
        data = image_to_rgb565(image, width, height)
        self.board.draw_image(0, 0, width, height, data)

    def _draw_meter_region(self, snap: dict, width: int, height: int):
        y0, y1 = self._meter_bounds(height)
        image = self._base_image.crop((0, y0, width, y1)) if self._base_image else Image.new("RGBA", (width, y1 - y0), (0, 0, 0, 255))
        draw = ImageDraw.Draw(image)
        self._draw_sound_meter(draw, snap, width, height, y_offset=-y0)
        data = image_to_rgb565(image, width, y1 - y0)
        self.board.draw_image(0, y0, width, y1 - y0, data)

    def _meter_bounds(self, height: int) -> tuple[int, int]:
        base_y = height - 62
        return max(0, base_y - 24), min(height, base_y + 24)

    def _draw_header(self, image: Image.Image, draw: ImageDraw.ImageDraw, snap: dict, width: int):
        x0 = getattr(self.board, "CornerHeight", 20)
        draw.text((x0, 7), "AirPlay", font=self._title_font, fill=(255, 255, 255))

        cursor_x = width - 14
        battery_w = self._measure_battery_icon(snap["battery_level"])
        if battery_w:
            cursor_x -= battery_w
            self._draw_battery(draw, snap, cursor_x, 13)
            cursor_x -= 8

        wifi_icon = self._get_wifi_icon(snap["wifi_signal_level"])
        if wifi_icon:
            cursor_x -= wifi_icon.width
            image.paste(wifi_icon, (cursor_x, 10), wifi_icon)

    def _measure_battery_icon(self, level: int) -> int:
        return 28 if level >= 0 else 0

    def _draw_battery(self, draw: ImageDraw.ImageDraw, snap: dict, x: int, y: int):
        level = snap["battery_level"]
        if level < 0:
            return
        color = snap["battery_color"]
        bw, bh = 26, 14
        draw.rounded_rectangle([x, y, x + bw, y + bh], radius=3, outline="white", width=2)
        draw.rectangle([x + 2, y + 2, x + bw - 2, y + bh - 2], fill=color)
        draw.rectangle([x + bw, y + 5, x + bw + 2, y + 9], fill="white")
        txt = str(level)
        tw, th = text_size(draw, txt, self._battery_font)
        fill = "black" if luminance(color) > 128 else "white"
        draw.text((x + (bw - tw) // 2, y + (bh - th) // 2 - 3), txt, font=self._battery_font, fill=fill)

    def _get_wifi_icon(self, level: int) -> Image.Image | None:
        try:
            lvl = int(level)
        except (TypeError, ValueError):
            return None
        if lvl < 1 or lvl > 3:
            return None
        if lvl in self._wifi_cache:
            return self._wifi_cache[lvl]
        path = os.path.join(_ASSETS_DIR, _WIFI_LEVEL_ICONS[lvl])
        if not os.path.exists(path):
            self._wifi_cache[lvl] = None
            return None
        src = Image.open(path).convert("RGBA")
        target_h = 20
        target_w = max(1, int(src.width * target_h / src.height))
        icon = src.resize((target_w, target_h), Image.LANCZOS)
        self._wifi_cache[lvl] = icon
        return icon

    def _draw_center(self, draw: ImageDraw.ImageDraw, snap: dict, width: int, height: int):
        connected = bool(snap["device_name"])
        status = snap["status"] or ("Connected" if connected else "Waiting")
        device = fit_text(snap["device_name"] or "Waiting", 20)
        color = (125, 211, 252) if snap["is_playing"] else ((59, 130, 246) if connected else (156, 163, 175))

        dot_x = width // 2 - 4
        draw.ellipse([dot_x, 72, dot_x + 8, 80], fill=color)
        st_w, _ = text_size(draw, status, self._small_font)
        draw.text(((width - st_w) // 2, 88), status, font=self._small_font, fill=(190, 190, 190))

        lines = [device]
        if " " in device and len(device) > 14:
            parts = device.split(" ", 1)
            lines = [parts[0], parts[1]]
        y = 124 if len(lines) == 1 else 112
        for line in lines[:2]:
            lw, lh = text_size(draw, line, self._device_font)
            draw.text(((width - lw) // 2, y), line, font=self._device_font, fill=(255, 255, 255))
            y += lh + 8

    def _draw_sound_meter(self, draw: ImageDraw.ImageDraw, snap: dict, width: int, height: int, y_offset: int = 0):
        target_level = max(0.0, min(1.0, float(snap["audio_level"]) / 100.0))
        now = time.monotonic()
        dt = max(0.0, min(0.2, now - self._last_frame_at))
        self._last_frame_at = now

        if snap["is_playing"]:
            tau = 0.045 if target_level > self._display_level else 0.18
        else:
            target_level = 0.0
            tau = 0.10
        alpha = 1.0 - math.exp(-dt / tau) if tau > 0 and dt > 0 else 1.0
        self._display_level += (target_level - self._display_level) * alpha

        if snap["is_playing"]:
            self._level_history.insert(0, self._display_level)
        else:
            self._level_history.insert(0, 0.0)
        self._level_history = self._level_history[:13]

        bars_per_side = 12
        bar_w = 4
        gap = 3
        max_h = 34
        min_h = 4
        center_x = width // 2
        base_y = height - 62 + y_offset

        for side in (-1, 1):
            for index in range(bars_per_side):
                distance = index + 1
                x = center_x + side * (distance * (bar_w + gap))
                if side < 0:
                    x -= bar_w
                if snap["is_playing"]:
                    level = max(0.0, min(1.0, self._level_history[min(index, len(self._level_history) - 1)]))
                    falloff = 1.0 - (index / (bars_per_side + 3))
                    bar_h = int(min_h + max_h * level * falloff)
                    color = (125, 211, 252)
                else:
                    bar_h = min_h
                    color = (55, 65, 81)
                y0 = base_y - bar_h // 2
                y1 = base_y + bar_h // 2
                draw.rounded_rectangle([x, y0, x + bar_w, y1], radius=2, fill=color)

        center_level = max(0.0, min(1.0, self._level_history[0]))
        center_h = int((10 if snap["is_playing"] else min_h) + max_h * 0.48 * center_level * (1 if snap["is_playing"] else 0))
        draw.rounded_rectangle(
            [center_x - 2, base_y - center_h // 2, center_x + 2, base_y + center_h // 2],
            radius=2,
            fill=(186, 230, 253) if snap["is_playing"] else (55, 65, 81),
        )
