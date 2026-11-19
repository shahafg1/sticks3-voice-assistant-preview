# Statick (סטטיק): the assistant's name and identity

Branch `feature/statick-identity`, based on the `v0.10.0` tag. **Status: released as v0.11.0** (name, identity prompt, character on the Stick and in the panel). The owner approved the design and physically tested the deployed version on 2026-11-18 and confirmed that **everything works well**. That is a **general confirmation**: it is not a record that every individual item of the test list was exercised, and the identity prompt's answers to a real question have no separate record. The visual prototype is in `design/statick-prototype/`.

## The names
* Hebrew **סטטיק**, English **Statick**. "Statick" is a play on "Stick" (the device). The artist in the reference photo is *Static*; the assistant is **not** that artist and never claims to be, and does not imitate anyone's voice. The previously proposed Hebrew spelling ending in ס is not used anywhere (a test checks the whole repository).

## Where it is defined (one place)
`identity.py` holds `NAME_HE`, `NAME_EN` and `IDENTITY_PROMPT` (built from the two names). `audio_server.py` adds `IDENTITY_PROMPT` to the **permanent system instructions** of every voice answer (`assistant_prompt()`: the existing style rules, then the identity, then the current time; the conversation-history rules are appended after them only when there is history). The history itself travels as data in the user message and never contains the identity. Consequences, all tested:
* it is present on every question, including the first one after a server restart (it lives in code, not in memory) and after "שיחה חדשה" (a reset clears only the history; `conversation.py` knows nothing about identity);
* the concise, natural Hebrew style rules are untouched.

## Behaviour the prompt asks for
Knows its name and treats a form of address (at the start of a request, for example) as being spoken to; answers naturally and briefly when asked its name ("קוראים לי סטטיק"); does **not** introduce itself or mention its name in every answer; tolerates transcription spellings (for example סטאטיק, Static, Statik) without remarking on them; does not treat the ordinary word "static" (static electricity) as an address; Statick is its identity, not the artist.
**Not a wake word:** recording still starts only when the button is pressed. **Transcription is unchanged:** the Whisper text reaches the model exactly as transcribed; there are no text replacements to force the name (a test feeds four spellings through the real server and checks they arrive verbatim).

## Where the name is visible
The panel's tab title and heading ("סטטיק Statick · פאנל העוזר הקולי"), the voice fader label ("הקול של סטטיק", formerly "קול הרובוט"), the illustration's accessible names, and the identity text above. Not renamed: project folders, the repository, files, desktop shortcuts, API routes, element ids and setting keys (a test checks that no file was renamed and the key ids exist). "הסטיק" in the panel still means the **device**.

## For the future setup kit (no customisation interface exists)
Let each owner choose a name by changing only `NAME_HE` and `NAME_EN` in `identity.py`, which should become two values of the host configuration (for example `assistant_name_he`, `assistant_name_en` in `[assistant]`, read by `identity.py`; the installer branch's `stick_config.py` is the natural place). The panel's visible strings are static HTML, so the kit would substitute them when serving, the way `__CSRF__` already is. The TTS voice is unrelated to the name and stays the current local Hebrew voice.

## What was tested, and what was not
* **Tested (isolation: temporary folders, a test port, a stand-in for `claude`; no live server, data or account):** names exact; prompt text content; the identity in the system prompt of every call, with and without history, ahead of the history rules and absent from the history block; unchanged after a reset; present in each of two separate server processes; transcripts arrive verbatim; panel title/heading/label; no renamed files. These test **prompt construction and pipeline behaviour only**.
* **NOT tested:** how a real model answers to the name (no real Claude call was made), whether Whisper transcribes the spoken name correctly, how the current TTS voice pronounces "סטטיק", and any real voice conversation. A short real conversation is the way to find out (ask the name, greet it by name, ask a plain question and check it does not introduce itself).

