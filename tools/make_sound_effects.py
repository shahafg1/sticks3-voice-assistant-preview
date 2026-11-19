#!/usr/bin/env python3
"""Write the Stick's shake reactions as original synthesized sound effects (no voice, no recordings, no third-party audio).

    python3 tools/make_sound_effects.py [OUTPUT_FOLDER]      default: stick/sounds/

Three short effects, 16 kHz mono 16-bit WAV like every sound the Stick plays: a falling "boing", a wobble, and two quick pops.
The output is deterministic, so the same files (and hashes) are produced every time. Part of this project (MIT).
After changing them, re-pin the install manifest: python3 tools/stick_install.py pin
"""
import math
import struct
import sys
import wave
from pathlib import Path

RATE = 16000
PEAK = 0.55                      # comfortable loudness on the Stick's small speaker


def envelope(t, length, attack=0.01, release=0.08):
    rise = min(1.0, t / attack) if attack else 1.0
    fall = min(1.0, (length - t) / release) if release else 1.0
    return max(0.0, min(rise, fall))


def boing(length=0.7):
    """A pitch that falls and settles, with a fast decay: the sound of a spring."""
    phase, out = 0.0, []
    for n in range(int(RATE * length)):
        t = n / RATE
        freq = 180 + 520 * math.exp(-6 * t) + 25 * math.sin(2 * math.pi * 9 * t) * math.exp(-3 * t)
        phase += 2 * math.pi * freq / RATE
        out.append(math.sin(phase) * math.exp(-3.2 * t) * envelope(t, length))
    return out


def wobble(length=0.8):
    """A tone that wobbles up and down, like a jelly."""
    phase, out = 0.0, []
    for n in range(int(RATE * length)):
        t = n / RATE
        freq = 420 + 140 * math.sin(2 * math.pi * 7 * t)
        phase += 2 * math.pi * freq / RATE
        tone = math.sin(phase) + 0.3 * math.sin(2 * phase)
        out.append(tone / 1.3 * envelope(t, length, release=0.2))
    return out


def pops(count=2, gap=0.14, length=0.09):
    """Quick rising pops."""
    out = []
    for k in range(count):
        phase = 0.0
        for n in range(int(RATE * length)):
            t = n / RATE
            freq = 600 + 900 * (t / length) + 150 * k
            phase += 2 * math.pi * freq / RATE
            out.append(math.sin(phase) * envelope(t, length, attack=0.003, release=0.03))
        out += [0.0] * int(RATE * (gap - length))
    return out


def write(path, samples):
    peak = max(abs(s) for s in samples) or 1.0
    frames = b"".join(struct.pack("<h", int(round(s / peak * PEAK * 32767))) for s in samples)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(frames)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    folder = Path(argv[0]) if argv else Path(__file__).resolve().parent.parent / "stick" / "sounds"
    folder.mkdir(parents=True, exist_ok=True)
    for name, samples in (("shake1.wav", boing()), ("shake2.wav", wobble()), ("shake3.wav", pops())):
        write(folder / name, samples)
        print(f"wrote {folder / name} ({len(samples) / RATE:.2f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
