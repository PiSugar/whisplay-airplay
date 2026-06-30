import unicodedata

from PIL import Image, ImageDraw, ImageFont


def luminance(rgb: tuple[int, int, int]) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def image_to_rgb565(image: Image.Image, width: int, height: int) -> bytes:
    image = image.convert("RGB")
    image.thumbnail((width, height), Image.LANCZOS)
    bg = Image.new("RGB", (width, height), (0, 0, 0))
    x = (width - image.width) // 2
    y = (height - image.height) // 2
    bg.paste(image, (x, y))
    raw = bg.tobytes()
    out = bytearray(width * height * 2)
    dst = 0
    for idx in range(0, len(raw), 3):
        r = raw[idx]
        g = raw[idx + 1]
        b = raw[idx + 2]
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out[dst] = (rgb565 >> 8) & 0xFF
        out[dst + 1] = rgb565 & 0xFF
        dst += 2
    return bytes(out)


def _is_wide(char: str) -> bool:
    return unicodedata.east_asian_width(char) in {"W", "F"}


def text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def fit_text(text: str, max_chars: int) -> str:
    width = 0
    out = []
    for char in text:
        width += 2 if _is_wide(char) else 1
        if width > max_chars:
            break
        out.append(char)
    result = "".join(out).rstrip()
    if len(result) < len(text):
        return result[:-1].rstrip() + "..." if len(result) > 3 else result
    return result
