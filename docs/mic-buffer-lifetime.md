# Microphone buffer lifetime fix (fix/mic-buffer-lifetime)

**Status:** built from `v0.11.0` and deployed to the Stick. The physical validation is complete, and the owner gave a general confirmation. Proposed as bug-fix release **v0.11.1**, which isn't merged or tagged yet.

## The problem
On v0.11.0, pressing the front button sometimes showed "Get ready" and then restarted the Stick. Once it showed "App error" instead, with `'' object isn't iterable`. Investigation notes and captures are kept locally in `stick-backup-failed-macaudio-20261118_225952/bisect/`.

**How the driver works.** The source matching the firmware is uiflow-micropython `2.4.8`, M5Unified at `55efe50`, `Mic_Class`.
* **Asynchronous recording.** `M5.Mic.record(buf)` puts a raw pointer to `buf` into one of two request slots and returns immediately. A background task fills the buffer later.
* **`isRecording()` is ambiguous.** It is 0 both before the task picks a request up and after it finishes one.
* **`Mic.end()` doesn't cancel queued requests.** It waits for the request being recorded, but a queued request that hasn't started stays queued. The next `Mic.begin()` records into it.
* **No Python reference is kept.** The MicroPython wrapper holds only the raw pointer, so the garbage collector may free the buffer and reuse that memory.

**How v0.11.0 hit this.** At start-up it recorded a 3,200-byte warm-up, checked `isRecording()` once, called `Mic.end()` and deleted the buffer.

## Evidence (what was observed, and how precisely)
* **I3:** v0.11.0 `voice_send.py` plus print-only traces.
  * The first press after a fresh boot failed 3 of 3 times, each at the first "Get ready" redraw after `Mic.begin()`: 2 restarts and 1 `'' object isn't iterable`.
  * A later read-only look at the drawing cache from the REPL restarted the Stick.
* **I4:** I3 plus two changes:
  * the warm-up buffer was **kept** (no `del warmup`);
  * print-only readings of that buffer were added.

  So keeping the buffer was **not literally the only difference**: the readings were new too.
* **I4 results:** 4 of 4 fresh boots passed a silent recording and the automatic sleep that followed. In every boot:
  * `isRecording()` was already 0 at the first check, and the buffer was still all zeros after the start-up `Mic.end()`. This is the start-up race.
  * The kept buffer then received samples after the press's `Mic.begin()`, at the same address each time.
* **Strength of the evidence:** this strongly supports the cause above but does not prove it, and it doesn't show this is the only cause. That the failures began with Statick fits its long-lived drawing cache, but that explanation is untested.

## The fix
The fix is in `stick/mic.py`, used by `stick/voice_send.py`.
1. **Buffer lifetime.** Every buffer given to the driver comes from `Recorder.buffer()`: the warm-up, the 6-second recording and a 0.5-second drain buffer. They are made once at start-up and referenced for the app's lifetime. `voice_send.py` never calls `M5.Mic.record/end/isRecording` directly.
2. **Completion.** A request counts as finished only when `isRecording()` has been seen **non-zero after it was queued, and then zero**. This also covers an older request still queued ahead of it:
   * the driver clears a request's length only after its last sample;
   * while its task runs, `isRecording()` stays non-zero as long as any queued request is unfinished.

   No fixed delay is treated as proof of completion. The 1-second "Get ready" hold is only for the user.
3. **Uncertainty.** If a request is never seen running within 1.5 s, it may not have started, or it may have finished between two checks. The Recorder then becomes **UNSURE**, the attempt fails, and nothing is uploaded. Before any buffer is used again, a drain recording is queued behind everything (the driver serves its two slots in order). Only when the drain has been seen to finish is the state back to **OK**.
4. **Stuck driver.** A request seen running that doesn't finish within its length plus 4.5 s means the driver is stuck. Recording is then **BLOCKED** until a restart: presses show "Restart Stick" and `Mic.end()` is no longer called, because it would wait forever. The same happens if the drain itself isn't seen running in time.
5. **Diagnosis.** Unexpected exceptions now print full MicroPython tracebacks to the USB log: the press handler, the reminder handler and `main.py`. A traceback holds file, line, function and the exception message; it has no local variables, no audio and no key.

## Physical validation (2026-11-19)
This is the clean fix, commit `7acd564`, with no diagnostic code. `mic.py`, `voice_send.py` and `main.py` were hash-verified on the Stick before every fresh boot. Each run had a passive capture: read-only serial from boot, USB presence, and the server's view of boot id and screen state.

| Test | What was done | What the logs show |
|---|---|---|
| Fresh boots 1–5 | After each reset, one silent first press, then about 2 minutes untouched | 5 of 5: no restart, the boot id unchanged, "Audio sent", no `Mic` message, error or traceback, and the automatic sleep after the recording completed |
| Consecutive recordings | 5 silent recordings in one boot, then about 2 minutes untouched (a notification also woke the screen once beforehand) | 5 "Audio sent" lines, no restart or error, and the sleep after the fifth completed |
| Spoken question | A spoken question as the first recording after a fresh boot, then the answer played to the end | No restart or error; the server produced a transcript and a spoken reply (file times and sizes only, contents not read); the sleep after it completed |

**Start-up warm-up:** the fix prints a `Mic` line only when a recording can't be proven complete. No boot printed one, so the warm-up was proven complete each time. That conclusion rests on the absence of a message.

**The owner's confirmation:** the owner ran these tests and reported "No speech", no restart and no error in each silent run, and that the spoken-question test worked. That is a general confirmation, not a separate confirmation of every item above.

For comparison, the same first-press test failed 3 of 3 times on I3 (v0.11.0 logic plus print-only traces). The fix's untaken paths (never started, stuck driver, failed drain) are covered only by the simulated driver and were not triggered on the hardware.

## Remaining limits (driver behavior Python can't change)
* `Mic.end()` and the driver's own wait for a free request slot have no timeout. If I2S stopped delivering data while a request was being recorded, either could block for good. The app would freeze, which is safe for memory but needs a restart.
* An exception while a request is being recorded makes the error handler's `Mic.end()` wait for that request to finish (at most its length when the driver is healthy).
* Speaker playback also passes raw buffer pointers to the driver. Its buffer lifetime hasn't been reviewed in this fix.

## Tests
`tests/test_mic.py` (17 cases) uses a simulated driver that follows the request handling described above. It covers:
* the v0.11.0 bug being reproduced by the model;
* normal and delayed starts;
* a recording that returns only after its last sample;
* a stale queued request served before the buffer is reused;
* a short recording missed between two checks, which becomes UNSURE and is then proven idle;
* an exception while waiting;
* a stuck request, which blocks recording and `Mic.end()`;
* a drain not seen in time, which blocks;
* a driver that refuses to start;
* buffer registration, plus source checks on `voice_send.py`, `main.py` and the deploy order.

Four deliberate breakages of `mic.py` were each caught: treating the first zero as completion, skipping the drain, calling `end()` while blocked, and treating "not started" as done.

## Deploy and roll back
* **Deploy:** `tools/deploy_stick.sh` uploads `mic.py` before `voice_send.py` and `main.py` last, verifies every hash and resets.
* **Roll back:** re-upload the previous Stick files from the pre-deploy backup and remove `mic.py`. Plain v0.11.0 (`data/rollback_to_v0_11_0.sh`) is available but has this bug. No server change, no settings change.
