"""Local speech detection for Stick recordings (16 kHz mono 16-bit WAV).

Decides whether a recording contains speech before it is sent to Whisper and
Claude. It does not depend on absolute loudness (the Stick microphone is quiet):
each frame is compared with the recording's own noise floor, and it must also
be voiced, i.e. periodic at a human pitch (80-400 Hz), which noise is not.
"""
import wave

import numpy as np
from scipy.signal import butter, sosfilt

RATE = 16000
FRAME = 480            # 30 ms
HOP = 160              # 10 ms
MIN_LAG = RATE // 400  # 400 Hz
MAX_LAG = RATE // 80   # 80 Hz

ABOVE_FLOOR_DB = 4.0   # frame energy above the recording's own noise floor
VOICING = 0.45         # normalized autocorrelation peak at a pitch lag
MIN_SPEECH_S = 0.25    # total voiced speech needed
MIN_RUN_S = 0.10       # at least one continuous voiced stretch this long

_HIGHPASS = butter(4, 80, "highpass", fs=RATE, output="sos")


def _frames(x):
    count = 1 + (len(x) - FRAME) // HOP
    index = np.arange(FRAME)[None, :] + HOP * np.arange(count)[:, None]
    return x[index]


def _voicing(frames):
    frames = frames - frames.mean(axis=1, keepdims=True)
    spectrum = np.fft.rfft(frames, 2 * FRAME, axis=1)
    autocorr = np.fft.irfft(np.abs(spectrum) ** 2, axis=1)[:, :FRAME]
    energy = autocorr[:, :1] + 1e-9
    return (autocorr[:, MIN_LAG:MAX_LAG] / energy).max(axis=1)


def detect_speech(path):
    """Return (has_speech, details) for a 16 kHz mono 16-bit WAV file."""
    with wave.open(str(path), "rb") as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (RATE, 1, 2):
            raise ValueError("expected 16 kHz mono 16-bit audio")
        samples = np.frombuffer(audio.readframes(audio.getnframes()), np.int16)

    peak = int(np.abs(samples.astype(np.int32)).max()) if len(samples) else 0
    if peak <= 2 or len(samples) < FRAME:
        return False, {"reason": "digital silence", "peak": peak, "speech_s": 0.0}

    x = sosfilt(_HIGHPASS, samples.astype(np.float64))
    frames = _frames(x)
    level_db = 10 * np.log10((frames ** 2).mean(axis=1) + 1e-9)
    floor_db = float(np.percentile(level_db, 10))
    loud = level_db > floor_db + ABOVE_FLOOR_DB
    voiced = _voicing(frames) > VOICING
    speech = loud & voiced

    longest = run = 0
    for flag in speech:
        run = run + 1 if flag else 0
        longest = max(longest, run)
    speech_s = speech.sum() * HOP / RATE
    longest_s = longest * HOP / RATE
    has_speech = speech_s >= MIN_SPEECH_S and longest_s >= MIN_RUN_S
    return has_speech, {
        "reason": "speech" if has_speech else "no voiced speech",
        "peak": peak,
        "floor_db": round(floor_db, 1),
        "speech_s": round(float(speech_s), 2),
        "longest_s": round(float(longest_s), 2),
    }


if __name__ == "__main__":
    import sys

    for name in sys.argv[1:]:
        print(name, *detect_speech(name))
