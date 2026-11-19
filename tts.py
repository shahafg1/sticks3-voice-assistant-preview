"""Local Hebrew text-to-speech (Piper + phonikud), shared by voice answers and reminders.

One process at a time: both callers hold `lock` while the model runs, so a reminder never runs a second model
process next to an answer's. No network and no cloud model is involved.
"""
import subprocess
import threading
from pathlib import Path

lock = threading.Lock()


def render(root, text, raw_wav, out_wav, timeout=120, python=None, models=None, ffmpeg="ffmpeg"):
    """Synthesize `text` to raw_wav, then convert it to the 16 kHz mono 16-bit WAV the Stick plays (out_wav).
    Raises subprocess.SubprocessError or OSError on failure. The caller holds `lock`.
    root is the project folder; python, models and ffmpeg come from the host configuration (defaults: the historical layout under root)."""
    root = Path(root)
    python = Path(python) if python else root / "tts-env312" / "bin" / "python"
    models = Path(models) if models else root / "tts-models"
    subprocess.run(
        [str(python), str(root / "hebrew_tts.py"),
         "--model", str(models / "piper_medium_male.onnx"), "--config", str(models / "model.config.json"),
         "--phonikud", str(models / "phonikud-1.0.onnx"), "--output", str(raw_wav),
         "--length-scale", "1.0", "--text", text],
        check=True, timeout=timeout)
    subprocess.run(
        [str(ffmpeg), "-y", "-loglevel", "error", "-i", str(raw_wav), "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(out_wav)],
        check=True, timeout=60)
