# mic.py - microphone recordings whose end is proven, into buffers the driver can never outlive.
#
# Why (docs/mic-buffer-lifetime.md): the M5 microphone driver (UIFlow 2.4.8, M5Unified Mic_Class) records asynchronously.
# record() puts a raw pointer to the buffer into one of two request slots and returns at once; a background task fills it later.
#  * isRecording() is 0 both BEFORE the task has picked a request up and AFTER it has finished it;
#  * Mic.end() waits for the request being recorded, but does not cancel one that is queued and not started: the next
#    Mic.begin() records into it;
#  * the driver keeps no Python reference to the buffer: a buffer freed by Python can be overwritten after its memory was reused.
# The rules kept here:
#  1. every buffer handed to the driver comes from Recorder.buffer() and stays referenced for the whole app;
#  2. a request is complete only when isRecording() was seen non-zero after it was queued, and then zero. The driver clears a
#     request's length only after its last sample is written, and while its task runs isRecording() stays non-zero as long as
#     any queued request is unfinished, so this also covers an older request still queued ahead of ours;
#  3. when that cannot be shown (never seen non-zero in time: not started yet, or started and finished between two looks), the
#     state is UNSURE and no buffer is used again until a drain recording, queued behind everything (the driver serves its two
#     slots in order), has been seen to finish;
#  4. a request seen running that does not finish in time means the driver is stuck: recording is BLOCKED until a restart and
#     Mic.end() is no longer called (it would wait for that request forever).
# Nothing here prints audio.

OK, UNSURE, BLOCKED = "ok", "unsure", "blocked"
RATE = 16000                    # 16-bit mono samples per second
START_TIMEOUT_MS = 1500         # the task picks a request up within a few tens of ms when healthy
FINISH_MARGIN_MS = 3000
POLL_MS = 10
DRAIN_BYTES = 16000             # 0.5 s: long enough that the poll loop cannot miss it

NOT_STARTED = "Mic did not run"
STUCK = "Mic stuck"
NOT_READY = "Mic not ready"
FAILED = "Recording failed"
INTERRUPTED = "Rec interrupted"


class Recorder:
    def __init__(self, mic, ticks_ms, ticks_diff, sleep_ms, log=print):
        self.mic = mic
        self._now, self._diff, self._sleep, self._log = ticks_ms, ticks_diff, sleep_ms, log
        self.buffers = []                       # strong references: the driver may hold a pointer into any of these
        self.drain = self.buffer(DRAIN_BYTES)
        self.state = OK
        self.reason = None

    def buffer(self, nbytes):
        """A recording buffer, kept referenced for the app's lifetime. Make every buffer once, at start-up."""
        buf = bytearray(nbytes)
        self.buffers.append(buf)
        return buf

    def record(self, buf, on_tick=None):
        """Record into buf (16 kHz mono). Returns None only when this request is proven complete; otherwise a short reason,
        and buf holds nothing usable from this attempt. on_tick() is called while waiting (for example to animate)."""
        if not any(b is buf for b in self.buffers):
            raise ValueError("buffer not from Recorder.buffer()")
        if self.state == BLOCKED:
            return self.reason
        if self.state == UNSURE:
            problem = self._settle(on_tick)
            if problem:
                return problem
        if not self.mic.record(buf, RATE, False):
            return FAILED                       # the driver could not start: nothing was queued
        result = self._wait(_ms(len(buf)), on_tick)
        if result == "done":
            return None
        if result == "not started":
            self.state, self.reason = UNSURE, NOT_STARTED
            self._log("Mic: request not seen running; the next recording first proves the driver idle")
            return NOT_STARTED
        return self._block(STUCK)

    def end(self):
        """Mic.end(), except when the driver is stuck (it would wait for that request forever)."""
        if self.state != BLOCKED:
            self.mic.end()

    def _settle(self, on_tick):
        """UNSURE: an earlier request may still be queued. Queue the drain recording behind it and see everything finish."""
        if not self.mic.record(self.drain, RATE, False):
            return FAILED                       # still UNSURE: tried again at the next recording
        longest = 0
        for b in self.buffers:
            longest = max(longest, _ms(len(b)))
        result = self._wait(longest + _ms(len(self.drain)), on_tick)
        if result == "done":
            self.state, self.reason = OK, None
            self._log("Mic: driver proven idle again")
            return None
        return self._block(NOT_READY if result == "not started" else STUCK)

    def _block(self, reason):
        self.state, self.reason = BLOCKED, reason
        self._log("Mic: recording blocked until the Stick restarts:", reason)
        return reason

    def _wait(self, expected_ms, on_tick):
        """After a request was queued: 'done' (seen non-zero, then zero), 'not started' (never seen non-zero in time) or
        'stuck' (seen running, not finished in time). An exception here leaves the state UNSURE and is re-raised."""
        start = self._now()
        started = False
        try:
            while True:
                if self.mic.isRecording():
                    started = True
                elif started:
                    return "done"
                waited = self._diff(self._now(), start)
                if not started and waited >= START_TIMEOUT_MS:
                    return "not started"
                if waited >= START_TIMEOUT_MS + expected_ms + FINISH_MARGIN_MS:
                    return "stuck"
                if on_tick:
                    on_tick()
                self._sleep(POLL_MS)
        except BaseException:
            if self.state == OK:
                self.state, self.reason = UNSURE, INTERRUPTED
            raise


def _ms(nbytes):
    return nbytes // 2 * 1000 // RATE
