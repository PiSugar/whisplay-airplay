import logging
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
                if snap["version"] != last_version:
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
        image = Image.new("RGBA", (width, height), (0, 0, 0, 255))
        draw = ImageDraw.Draw(image)
        self._draw_header(image, draw, snap, width)
        self._draw_center(draw, snap, width, height)
        self._draw_volume(draw, snap, width, height)
        data = image_to_rgb565(image, width, height)
        self.board.draw_image(0, 0, width, height, data)

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
        color = (104, 211, 145) if snap["is_playing"] else ((96, 165, 250) if connected else (156, 163, 175))

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

    def _draw_volume(self, draw: ImageDraw.ImageDraw, snap: dict, width: int, height: int):
        volume = int(snap["volume"])
        if volume < 0:
            label = "Volume --"
            volume = 0
        else:
            label = f"Volume {volume}%"
        bar_w = 166
        bar_h = 8
        x = (width - bar_w) // 2
        y = height - 58
        draw.rounded_rectangle([x, y, x + bar_w, y + bar_h], radius=4, fill=(45, 45, 45))
        fill_w = max(0, min(bar_w, int(bar_w * volume / 100)))
        if fill_w:
            draw.rounded_rectangle([x, y, x + fill_w, y + bar_h], radius=4, fill=(96, 165, 250))
        lw, _ = text_size(draw, label, self._small_font)
        draw.text(((width - lw) // 2, y + 18), label, font=self._small_font, fill=(210, 210, 210))
