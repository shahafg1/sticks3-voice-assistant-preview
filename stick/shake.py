# shake.py - deliberate-shake detector for the StickS3's BMI270 accelerometer.
# Pure logic: feed it (x, y, z) in g with a millisecond timestamp, about every
# 20 ms. A shake is several sharp acceleration changes within a short window;
# a single jolt (pickup, button press) or a slow tilt is not. Jolts only count
# when they reverse direction, as back-and-forth shaking does.

PEAK_G = 0.6          # change between samples that counts as one sharp jolt
PEAK_GAP_MS = 60      # jolts closer than this count once
PEAKS_NEEDED = 5      # jolts needed within WINDOW_MS
WINDOW_MS = 1200
SETTLE_G = 0.12       # "still" means every change stays below this...
SETTLE_MS = 800       # ...for this long, before the detector rearms
COOLDOWN_MS = 3000    # minimum time between reactions

try:
    from time import ticks_add, ticks_diff  # MicroPython: wrap-safe tick math
except ImportError:  # CPython, for tests
    def ticks_diff(a, b):
        return a - b

    def ticks_add(a, b):
        return a + b


class ShakeDetector:
    def __init__(self):
        self.reset(0)
        self._cooldown_until = 0

    @property
    def armed(self):
        """True when the device is still and out of cooldown (safe moment for other activity)."""
        return self._armed

    def reset(self, now_ms, cooldown=False):
        """Forget all motion history. With cooldown, also wait COOLDOWN_MS and
        for the device to settle before the next trigger."""
        self._prev = None
        self._peaks = []
        self._last_peak = None
        self._last_dir = None
        self._still_since = None
        self._armed = not cooldown
        if cooldown:
            self._cooldown_until = ticks_add(now_ms, COOLDOWN_MS)

    def feed(self, accel, now_ms):
        """Add one sample; return True once when a deliberate shake is detected."""
        x, y, z = accel
        prev = self._prev
        self._prev = (x, y, z)
        if prev is None:
            return False
        dx, dy, dz = x - prev[0], y - prev[1], z - prev[2]
        delta = (dx * dx + dy * dy + dz * dz) ** 0.5

        if not self._armed:
            if delta >= SETTLE_G:
                self._still_since = None
            elif self._still_since is None:
                self._still_since = now_ms
            if (ticks_diff(now_ms, self._cooldown_until) >= 0 and self._still_since is not None
                    and ticks_diff(now_ms, self._still_since) >= SETTLE_MS):
                self._armed = True
                self._peaks = []
                self._last_peak = None
                self._last_dir = None
            return False

        if delta >= PEAK_G and (self._last_peak is None or ticks_diff(now_ms, self._last_peak) >= PEAK_GAP_MS):
            last = self._last_dir
            if last is None or dx * last[0] + dy * last[1] + dz * last[2] < 0:
                self._peaks.append(now_ms)
            self._last_peak = now_ms
            self._last_dir = (dx, dy, dz)
        self._peaks = [t for t in self._peaks if ticks_diff(now_ms, t) <= WINDOW_MS]
        if len(self._peaks) >= PEAKS_NEEDED:
            self.reset(now_ms, cooldown=True)
            return True
        return False
