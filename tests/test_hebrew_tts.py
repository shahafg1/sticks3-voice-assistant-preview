"""Logic of hebrew_tts.py (the project's own TTS adapter), without the voice models. Run: python3 tests/test_hebrew_tts.py"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import hebrew_tts as ht  # noqa: E402

CASES = []
IDS = {"^": [1], "$": [2], "_": [0], " ": [3], "a": [10], "b": [11, 12]}


def case(fn):
    CASES.append(fn)
    return fn


@case
def the_input_is_start_symbols_end_without_padding_and_unknown_symbols_are_skipped():
    sequence, skipped = ht.symbols_to_ids("ab a?", IDS)
    assert sequence == [1, 10, 11, 12, 3, 10, 2] and skipped == 1, (sequence, skipped)


@case
def output_is_scaled_to_the_peak_and_clipped_into_16_bits():
    pcm = ht.to_pcm16(np.array([0.0, 0.5, -0.25], dtype=np.float32))
    assert pcm.dtype == np.int16 and pcm[1] == int(ht.PEAK * 32767) and pcm[2] == int(-ht.PEAK / 2 * 32767)
    assert ht.to_pcm16(np.zeros(4, dtype=np.float32)).tolist() == [0, 0, 0, 0]


@case
def a_config_without_start_or_end_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "c.json"
        path.write_text(json.dumps({"phoneme_id_map": {"^": [1]}, "audio": {"sample_rate": 22050}}))
        try:
            ht.load_voice(path)
            raise AssertionError("accepted")
        except ValueError:
            pass


@case
def the_command_line_contract_is_unchanged_and_failures_exit_1():
    with tempfile.TemporaryDirectory() as tmp:
        base = [sys.executable, str(ROOT / "hebrew_tts.py"), "--model", f"{tmp}/m.onnx", "--config", f"{tmp}/c.json",
                "--phonikud", f"{tmp}/p.onnx", "--output", f"{tmp}/out.wav", "--length-scale", "1.0"]
        empty = subprocess.run(base + ["--text", "  "], capture_output=True, text=True, timeout=60)
        assert empty.returncode == 1 and "empty text" in empty.stderr
        missing = subprocess.run(base + ["--text", "שלום"], capture_output=True, text=True, timeout=60)
        assert missing.returncode == 1 and "hebrew_tts:" in missing.stderr and not Path(f"{tmp}/out.wav").exists()
    usage = subprocess.run([sys.executable, str(ROOT / "hebrew_tts.py"), "--text", "x"], capture_output=True, text=True, timeout=60)
    assert usage.returncode == 2                                            # argparse: the same required options as before


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:500])
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
