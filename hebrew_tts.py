#!/usr/bin/env python3
"""Hebrew text to speech for the assistant: text -> diacritics -> phonemes -> a Piper-format ONNX voice -> WAV.

    python hebrew_tts.py --model VOICE.onnx --config VOICE.onnx.json --phonikud PHONIKUD.onnx --output OUT.wav \
                         [--length-scale 1.0] --text "שלום"

Writes a mono 16-bit WAV at the voice's sample rate (tts.py then converts it for the Stick). Exit status 0 on success, 1 on failure.

Built only on documented interfaces: phonikud-onnx (`Phonikud(path).add_diacritics(text)`) and phonikud (`phonemize(text)`), as shown
in their READMEs; the voice's own config file (`phoneme_id_map`, `audio.sample_rate`, `inference` scales; Piper voice format); and the
model's ONNX interface (inputs `input`, `input_lengths`, `scales`). The voice and its licence are separate: see THIRD_PARTY_NOTICES.md.
Runs in the TTS environment (Python 3.12, onnxruntime, phonikud, phonikud-onnx, numpy).
"""
import argparse
import json
import re
import sys
import wave

import numpy as np

PAUSE_S = 0.25                         # silence between sentences
PEAK = 0.9                             # the loudest sample is scaled to this fraction of full scale
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def load_voice(config_path):
    with open(config_path, encoding="utf-8") as handle:
        config = json.load(handle)
    ids = config["phoneme_id_map"]
    for symbol in ("^", "$"):
        if symbol not in ids:
            raise ValueError(f"the voice config has no '{symbol}' symbol")
    return config, ids


def symbols_to_ids(phonemes, ids):
    """The model's input: the start symbol, each known symbol's id(s), the end symbol. Unknown symbols are skipped.
    This voice was trained without padding between symbols (checked by transcribing its output back, tests/README.md)."""
    out = list(ids["^"])
    skipped = 0
    for symbol in phonemes:
        if symbol in ids:
            out += list(ids[symbol])
        else:
            skipped += 1
    return out + list(ids["$"]), skipped


def synthesize(session, sequence, scales):
    feed = {"input": np.array([sequence], dtype=np.int64),
            "input_lengths": np.array([len(sequence)], dtype=np.int64),
            "scales": np.array(scales, dtype=np.float32)}
    return session.run(None, feed)[0].reshape(-1).astype(np.float32)


def to_pcm16(audio):
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0:
        audio = audio * (PEAK / peak)
    return (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--phonikud", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--length-scale", type=float, default=None)
    parser.add_argument("--text", required=True)
    args = parser.parse_args(argv)
    text = args.text.strip()
    if not text:
        print("hebrew_tts: empty text", file=sys.stderr)
        return 1
    try:
        import onnxruntime
        from phonikud import phonemize
        from phonikud_onnx import Phonikud

        config, ids = load_voice(args.config)
        rate = int(config["audio"]["sample_rate"])
        inference = config.get("inference", {})
        scales = [float(inference.get("noise_scale", 0.667)),
                  float(args.length_scale if args.length_scale is not None else inference.get("length_scale", 1.0)),
                  float(inference.get("noise_w", 0.8))]
        diacritizer = Phonikud(args.phonikud)
        session = onnxruntime.InferenceSession(args.model, providers=["CPUExecutionProvider"])
        pieces, pause = [], np.zeros(int(rate * PAUSE_S), dtype=np.float32)
        for sentence in (s for s in SENTENCE_END.split(text) if s.strip()):
            sequence, skipped = symbols_to_ids(phonemize(diacritizer.add_diacritics(sentence)), ids)
            if skipped:
                print(f"hebrew_tts: {skipped} unknown symbol(s) skipped", file=sys.stderr)
            if len(sequence) > 3:
                pieces += [synthesize(session, sequence, scales), pause]
        if not pieces:
            print("hebrew_tts: nothing to say after phonemization", file=sys.stderr)
            return 1
        pcm = to_pcm16(np.concatenate(pieces[:-1]))
        with wave.open(args.output, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(pcm.tobytes())
        return 0
    except Exception as error:                                   # noqa: BLE001 - the caller only needs success or failure
        print(f"hebrew_tts: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
