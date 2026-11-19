# Third-party notices

The project's own code, documentation and sound effects are under the MIT licence (`LICENSE`). Everything below is **not** covered by that licence and keeps its own terms.

**Licences were checked** on 2026-11-19 against package metadata, PyPI, the upstream repositories and Hugging Face. This is a summary, not legal advice. Where a source states no licence, it says so below.

## Not cleared for commercial use
**The Hebrew voice is licensed CC BY-NC 4.0 (non-commercial).** The complete voice assistant therefore can't be used commercially as distributed. Commercial use would need a different, suitably licensed voice.

## Not included in this repository: downloaded by `setup-mac.sh` / `./setup.sh`
**Models** (pinned revisions; sizes and SHA-256 in `requirements/models.json`):

| Component | Licence | Attribution and notes |
|---|---|---|
| Hebrew voice `piper_medium_male.onnx` and `model.config.json`, from https://huggingface.co/notmax123/piper-medium-heb | **CC BY-NC 4.0** | Voice model by notmax123 (Hugging Face). Non-commercial use only. **Training data: not documented upstream** (the config names only `"dataset": "male2"`). Whether the training data carries further restrictions is unknown: neither established nor ruled out |
| `phonikud-1.0.onnx` (Hebrew diacritics model), from the same repository | CC BY-NC 4.0 as published there. The identical file is published under MIT at https://huggingface.co/Phonikud/phonikud-onnx | Based on Dicta's `dictabert-large-char-menaked` (https://huggingface.co/dicta-il/dictabert-large-char-menaked), **CC BY 4.0**: © Dicta |
| Whisper large-v3-turbo, MLX conversion (`weights.safetensors`, `config.json`), downloaded from https://huggingface.co/mlx-community/whisper-large-v3-turbo | **MIT** (OpenAI). That download carries no licence metadata, but the same bytes are explicitly licensed elsewhere (see below) | Copyright (c) 2022 OpenAI, MIT License: "Whisper's code and model weights are released under the MIT License" (https://github.com/openai/whisper) |

**Python packages** (exact pins and hashes in `requirements/*.lock`). They're installed into local environments by setup, not redistributed here; each keeps its own licence files in the installed environment.

| Licence | Packages |
|---|---|
| MIT / MIT-0 | anyio, charset-normalizer, filelock, h11, mlx, mlx-metal, mlx-whisper, more-itertools, PyYAML, setuptools, tiktoken, truststore, urllib3, colorlog, docopt, onnxruntime, mpremote, platformdirs, cffi |
| BSD | click, fsspec, httpcore2, httpx2, idna, Jinja2, MarkupSafe, mpmath, networkx, sympy, protobuf, pycparser, pyserial, numba, scipy, soundfile |
| Apache-2.0 | hf-xet, huggingface_hub, requests, flatbuffers, tokenizers |
| Compound or permissive mixes | llvmlite, numpy, torch, packaging, regex |
| MPL-2.0 | certifi; tqdm (MPL-2.0 and MIT) |
| PSF-2.0 | typing_extensions |
| **LGPL-2.1** | **num2words**; **libsndfile** (with mpg123 and LAME code), bundled inside soundfile |
| **CC BY 4.0** | **phonikud** 0.4.1 and **phonikud-onnx** 1.0.6 (Hebrew grapheme-to-phoneme and diacritics), https://github.com/phonikud/phonikud: © the Phonikud authors. phonikud-onnx's PyPI metadata names no licence; see below for the evidence |

**Notices that travel with these packages:**
* `requests` ships a NOTICE file.
* `onnxruntime` ships `ThirdPartyNotices.txt` (it includes Eigen under MPL-2.0).
* `numpy`, `scipy`, `torch`, `llvmlite` and `numba` ship third-party licence files. `scipy` bundles GCC runtime libraries under GPL-3.0 with the GCC Runtime Library Exception.

All of them are in the installed environments. If you ever redistribute a built environment or app, the LGPL and MPL obligations apply to that distribution.

## Evidence for two components without licence metadata
**These are cases of missing licence metadata, not a missing licence.** They were checked on 2026-11-19.

**phonikud-onnx 1.0.6:**
* **What PyPI shows:** no licence field, licence file or project links.
* **Matched to its source:** its source archive (`phonikud_onnx-1.0.6.tar.gz`, uploaded 2025-09-07) is **byte-identical** to the `phonikud_onnx/` folder of https://github.com/phonikud/phonikud at commit `f7e33e70ee` (2025-09-07, "Update versions for phonikud and phonikud-onnx to 0.4.1 and 1.0.6"). That covers `src/phonikud_onnx/__init__.py`, `model.py`, `pyproject.toml` and `export.py`. The installed files equal that archive.
* **The licence at that commit:** the repository's `LICENSE` is the Creative Commons Attribution 4.0 International Public License. The same licence ships inside the `phonikud` 0.4.1 package released in that commit.
* **Attribution:** phonikud-onnx, © the Phonikud authors, CC BY 4.0, https://github.com/phonikud/phonikud. This project uses it unmodified, installed from PyPI.

**Whisper large-v3-turbo (MLX conversion):**
* **The pinned files are identical to an explicitly licensed copy:** `weights.safetensors` (1,613,977,612 bytes, SHA-256 `951ed3fc…f8a6`) and `config.json` (SHA-256 `b34fc29e…6379`) are byte-identical to `model.safetensors` and `config.json` at https://huggingface.co/mlx-community/whisper-large-v3-turbo-fp16 (revision `258e98b1`).
* **That copy states:** licence Apache-2.0, base model `openai/whisper-large-v3-turbo`, and that it was converted from it.
* **OpenAI's terms:** OpenAI publishes the Whisper weights under MIT (Copyright (c) 2022 OpenAI).
* **Use here:** the files are downloaded by setup and used unmodified; they aren't redistributed by this project.

## Installed by you, not distributed by this project
* **Claude Code (Anthropic):** proprietary, used under Anthropic's terms for your account.
* **ffmpeg** (Homebrew build, GPL): used only as an external program.
* **Python** (PSF).
* **M5Stack UIFlow2 firmware** for the StickS3: the firmware binary's terms weren't determined here. The uiflow-micropython source is MIT. MicroPython is MIT.

## Original material in this repository (MIT, this project)
* The Hebrew TTS adapter `hebrew_tts.py`, written against the documented interfaces of phonikud, phonikud-onnx, onnxruntime and the Piper voice format.
* The shake sound effects in `stick/sounds/`: synthesized by `tools/make_sound_effects.py`; no voice or recording.
* The Statick character (`stick/statick_shapes.py`, generated from `design/statick-prototype/statick.js`).
