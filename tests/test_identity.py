"""Tests for the assistant's permanent name and identity (identity.py, docs/identity.md).

What these are: tests of PROMPT CONSTRUCTION and PIPELINE BEHAVIOUR. The real audio_server.py runs with a stand-in for `claude` that logs the system
prompt and the user message it receives; the stand-in answers with a fixed echo. NO real model is involved, so nothing here shows how a model actually
answers to the name: that needs a real conversation (see docs/identity.md, "What is not verified").
Run (needs numpy/scipy like the other server suites): voice-env/bin/python tests/test_identity.py
"""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import re
import subprocess
import sys
from pathlib import Path

import test_conversation_server as T          # its Harness: the real server with stand-ins for Claude, Whisper and the TTS (temporary folders, test port)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import identity  # noqa: E402
from conversation import HISTORY_RULES  # noqa: E402

CASES = []
REJECTED_HEBREW = "סטטי" + "ס"                  # the spelling that was proposed and replaced (built from parts so this file does not contain it)


def case(fn):
    CASES.append(fn)
    return fn


def has_identity(system):
    return identity.NAME_EN in system and identity.NAME_HE in system and "not the artist Static" in system


@case
def the_names_are_exactly_the_agreed_ones_and_the_rejected_spelling_exists_nowhere():
    assert identity.NAME_HE == "סטטיק" and identity.NAME_EN == "Statick"
    assert identity.NAME_HE != REJECTED_HEBREW
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True).stdout.decode().split("\0")
    for name in filter(None, files + ["identity.py", "docs/identity.md", "design/statick-prototype/index.html"]):
        path = ROOT / name
        if path.is_file() and path.suffix in (".py", ".md", ".html", ".js", ".css", ".txt", ".json", ".sh", ".command"):
            assert REJECTED_HEBREW not in path.read_text(errors="replace"), f"{name} contains the rejected spelling"


@case
def the_identity_text_states_what_the_brief_requires():
    text = identity.IDENTITY_PROMPT
    assert "play on the word 'Stick'" in text and "your identity" in text
    assert "not the artist Static" in text and "never claim to be them" in text and "imitate anyone's voice" in text
    assert "Do not mention your name or introduce yourself" in text and "only when the user asks" in text           # not in every response
    assert "differently in the text" in text and "(static electricity" in text                                       # tolerant, but not a broad rule
    assert "Recording starts only when the user presses the button" in text                                           # not a wake word
    assert "wake word" not in text.lower().replace("never say that your name woke you", "")


@case
def every_voice_answer_carries_the_identity_in_the_permanent_instructions_not_in_the_history():
    h = T.Harness()
    try:
        state, calls = h.ask("מה השם שלך")
        assert state["state"] == "ready" and len(calls) == 1
        first = calls[0]
        assert has_identity(first["system"]) and "Hebrew voice assistant" in first["system"]                             # identity AND the existing style rules
        assert "short, natural spoken Hebrew" in first["system"]                                                          # the concise style is unchanged
        assert identity.IDENTITY_PROMPT[:60] not in first["input"]                                                        # not smuggled into the user message
        assert HISTORY_RULES not in first["system"]                                                                       # no history yet: the rules are not there...
        state, calls = h.ask("ומה השעה")
        second = calls[0]
        assert HISTORY_RULES in second["system"] and has_identity(second["system"])                                     # ...with history both are, separately
        assert second["system"].index(identity.IDENTITY_PROMPT) < second["system"].index(HISTORY_RULES.strip()[:40])    # identity first, history rules after
        assert "<conversation_history>" in second["input"] and identity.IDENTITY_PROMPT[:60] not in second["input"]      # the history block never carries it
    finally:
        h.close()


@case
def a_conversation_reset_removes_the_history_but_never_the_identity():
    h = T.Harness()
    try:
        h.ask("שאלה ראשונה")
        h.ask("שאלה שנייה")
        before = len(h.calls())
        state, calls = h.ask(T.RESET)                                                                                      # "שיחה חדשה": answered locally, no model call
        assert state["state"] == "ready" and len(calls) == 0 and len(h.calls()) == before
        state, calls = h.ask("מה שמך")
        call = calls[0]
        assert has_identity(call["system"]) and HISTORY_RULES not in call["system"]                                       # history gone, identity intact
        assert "<conversation_history>" not in call["input"]
    finally:
        h.close()


@case
def a_restarted_server_has_the_identity_from_its_very_first_question_because_it_lives_in_code_not_in_memory():
    for _ in range(2):                                                                                                     # two independent server processes
        h = T.Harness()
        try:
            state, calls = h.ask("שלום")
            assert has_identity(calls[0]["system"])
        finally:
            h.close()
    source = (ROOT / "conversation.py").read_text()
    assert "identity" not in source.lower() and identity.NAME_EN not in source                                              # the memory module knows nothing of it


@case
def transcription_is_unchanged_and_no_text_is_replaced_to_force_the_name():
    h = T.Harness()
    try:
        for spoken in ("סטטיק מה השעה", "סטאטיק מה השעה", "Static what time is it", REJECTED_HEBREW + " מה השעה"):
            state, calls = h.ask(spoken)
            current = re.search(r"<current_user_message>\s*(.*?)\s*</current_user_message>", calls[0]["input"], re.S)
            text = current.group(1) if current else calls[0]["input"].strip()
            assert text == spoken, (spoken, text)                                                                          # exactly what Whisper produced
    finally:
        h.close()
    server = (ROOT / "audio_server.py").read_text()
    assert ".replace(" not in server.split("def process(job)")[1].split("def synthesize")[0].replace("temporary.replace(target)", "")


@case
def the_panel_shows_the_name_and_no_internal_identifier_was_renamed():
    html = (ROOT / "panel" / "index.html").read_text()
    assert identity.NAME_HE in html.split("<title>")[1].split("</title>")[0] and identity.NAME_HE in html.split('class="chrome-title">')[1].split("</h1>")[0]
    assert f'lang="en" dir="ltr">{identity.NAME_EN}<' in html and "הקול של סטטיק" in html
    for needed in ('id="robot"', 'id="f-voice"', 'data-key="voice_volume"', 'id="server-line"'):
        assert needed in html                                                                                              # ids, keys and routes keep their names


if __name__ == "__main__":
    failures = 0
    for fn in CASES:
        try:
            fn()
            print("ok   " + fn.__name__, flush=True)
        except BaseException as error:                  # noqa: BLE001
            import traceback
            failures += 1
            print("FAIL %s: %s: %s" % (fn.__name__, type(error).__name__, error), flush=True)
            traceback.print_exc()
    print("%d/%d identity cases passed" % (len(CASES) - failures, len(CASES)))
    sys.exit(1 if failures else 0)
