"""A fake M5 canvas for running stick/robot.py on CPython: it records every drawing call and rasterises them with Pillow, so the Stick's OWN drawing code
can be inspected (as an approximation: Pillow's rasteriser and font are not the Stick's; colours are reduced to RGB565 like the real canvas)."""
import sys
import types

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont
except ImportError:                       # the tests need only the recorded calls; the picture is a bonus when Pillow is installed
    Image = None


def rgb(color):
    r, g, b = (color >> 16) & 255, (color >> 8) & 255, color & 255
    r, g, b = (r >> 3) << 3 | (r >> 5), (g >> 2) << 2 | (g >> 6), (b >> 3) << 3 | (b >> 5)        # RGB565 and back
    return (r, g, b)


class Canvas:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.img = Image.new("RGB", (w, h), (0, 0, 0)) if Image else None
        self.d = ImageDraw.Draw(self.img) if Image else None
        self.calls = []
        self.pushes = 0
        self._font = None
        self._fg = (255, 255, 255)
        self.texts = []

    # -- primitives (the subset robot.py uses; every call is recorded with its arguments) --
    def fillScreen(self, color):
        self.calls.append(("fillScreen", color))
        if self.d:
            self.d.rectangle([0, 0, self.w, self.h], fill=rgb(color))

    def fillTriangle(self, x0, y0, x1, y1, x2, y2, color):
        self.calls.append(("fillTriangle", x0, y0, x1, y1, x2, y2, color))
        if self.d:
            self.d.polygon([(x0, y0), (x1, y1), (x2, y2)], fill=rgb(color))

    def fillRoundRect(self, x, y, w, h, r, color):
        self.calls.append(("fillRoundRect", x, y, w, h, r, color))
        if self.d:
            self.d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=r, fill=rgb(color))

    def fillRect(self, x, y, w, h, color):
        self.calls.append(("fillRect", x, y, w, h, color))
        if self.d:
            self.d.rectangle([x, y, x + w - 1, y + h - 1], fill=rgb(color))

    def fillCircle(self, x, y, r, color):
        self.calls.append(("fillCircle", x, y, r, color))
        if self.d:
            self.d.ellipse([x - r, y - r, x + r, y + r], fill=rgb(color))

    def drawLine(self, x0, y0, x1, y1, color):
        self.calls.append(("drawLine", x0, y0, x1, y1, color))
        if self.d:
            self.d.line([(x0, y0), (x1, y1)], fill=rgb(color))

    def fillArc(self, x, y, r0, r1, start, end, color):
        self.calls.append(("fillArc", x, y, r0, r1, start, end, color))
        if not self.d:
            return
        mask = Image.new("L", (self.w, self.h), 0)
        md = ImageDraw.Draw(mask)
        md.pieslice([x - r1, y - r1, x + r1, y + r1], start, end, fill=255)
        hole = Image.new("L", (self.w, self.h), 0)
        ImageDraw.Draw(hole).pieslice([x - r0, y - r0, x + r0, y + r0], start - 1, end + 1, fill=255)
        mask = ImageChops.subtract(mask, hole)
        self.img.paste(Image.new("RGB", (self.w, self.h), rgb(color)), (0, 0), mask)

    # -- text --
    def setFont(self, font):
        self._font = font

    def setTextSize(self, n):
        pass

    def setTextColor(self, fg, bg=None):
        self._fg = rgb(fg)

    def textWidth(self, text):
        if not Image:
            return int(len(text) * {"Montserrat14": 8, "Montserrat18": 10, "Montserrat24": 13}.get(self._font, 10))
        return int(self._pil().getlength(text))

    def _pil(self):
        size = {"Montserrat14": 14, "Montserrat18": 18, "Montserrat24": 24}.get(self._font, 18)
        for path in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFNS.ttf", "/Library/Fonts/Arial.ttf"):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return ImageFont.load_default()

    def drawString(self, text, x, y):
        self.calls.append(("drawString", text, x, y))
        self.texts.append((text, x, y))
        if self.d:
            self.d.text((x, y), text, fill=self._fg, font=self._pil())

    def push(self, x, y):
        self.pushes += 1


class Clock:
    def __init__(self):
        self.t = 100000

    def ticks_ms(self):
        return self.t

    @staticmethod
    def ticks_diff(a, b):
        return a - b

    def sleep_ms(self, ms):
        self.t += ms


def install(clock):
    """Put a fake `M5` into sys.modules and a fake clock into `time`; returns (module M5, restore function)."""
    import time
    fonts = types.SimpleNamespace(Montserrat14="Montserrat14", Montserrat18="Montserrat18", Montserrat24="Montserrat24")
    lcd = types.SimpleNamespace(FONTS=fonts, newCanvas=lambda w, h, depth, psram: Canvas(w, h))
    m5 = types.ModuleType("M5")
    m5.Lcd = lcd
    sys.modules["M5"] = m5
    saved = {name: getattr(time, name, None) for name in ("ticks_ms", "ticks_diff", "sleep_ms")}
    time.ticks_ms, time.ticks_diff, time.sleep_ms = clock.ticks_ms, clock.ticks_diff, clock.sleep_ms

    def restore():
        for name, value in saved.items():
            if value is None:
                delattr(time, name)
            else:
                setattr(time, name, value)
        sys.modules.pop("M5", None)
    return m5, restore
