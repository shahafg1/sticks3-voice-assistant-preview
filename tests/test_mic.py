"""Tests for stick/mic.py (proven-complete recordings into buffers the microphone driver can never outlive), and for how
stick/voice_send.py and stick/main.py use it. Run: python3 tests/test_mic.py

FakeMic models M5Unified's Mic_Class (commit 55efe50, as built into UIFlow 2.4.8) as far as these rules depend on it:
two request slots served in order, a background task that picks requests up asynchronously (`pickup_ms` after begin()
starts it, at once when it is already waiting), isRecording() computed exactly as the driver does
(`_is_recording ? bool(len0) + bool(len1) : 0`), a request's length cleared only after its last sample, end() that waits
for the request being recorded but leaves a queued one for the next begin(), and record() that waits for a free slot.
Writes go into the real buffers and are logged, so a write into a buffer the app has freed, or into one the app is using
for a newer request, is visible."""
import isolation_guard  # noqa: F401  (blocks production data, secrets, hooks and live endpoints)
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "stick"))
import mic  # noqa: E402

SAMPLES_PER_MS = 16


class Hang(Exception):
    """The real driver would block forever here."""


class Clock:
    def __init__(self):
        self.t = 0
        self.sim = None
        self.stalls = []                    # extra milliseconds added to the next sleep_ms calls (a stalled poll loop)

    def ticks_ms(self):
        return self.t

    def ticks_diff(self, a, b):
        return a - b

    def sleep_ms(self, ms):
        self.run(ms + (self.stalls.pop(0) if self.stalls else 0))

    def run(self, ms):
        for _ in range(ms):
            self.t += 1
            if self.sim:
                self.sim.step(self.t)


class FakeMic:
    def __init__(self, clock, pickup_ms=30):
        self.clock = clock
        clock.sim = self
        self.pickup_ms = pickup_ms
        self.begin_ok = True
        self.stall = False                  # I2S delivers no data: a request being recorded never finishes
        self.slots = [None, None]
        self.flip = 0
        self.is_recording = False
        self.running = False
        self.phase = None                   # None (no task), 'init', 'loop', 'wait', 'rec'
        self.init_until = 0
        self.cur = None
        self.next_id = 1
        self.writes = []                    # (time, id(buffer), request id)
        self.freed = {}                     # id(buffer) -> time the app freed it
        self.queued_at = {}                 # request id -> time
        self.finished_at = {}               # request id -> time
        self.calls = []

    def _len(self, i):
        return self.slots[i]["length"] if self.slots[i] else 0

    # --- the API voice_send uses ---
    def begin(self):
        self.calls.append("begin")
        if not self.begin_ok:
            return False
        if not self.running:
            self.running = True
            self.phase = "init"
            self.init_until = self.clock.t + self.pickup_ms
        return True

    def end(self):
        self.calls.append("end")
        if not self.running:
            return
        self.running = False
        if self.phase == "wait":
            self.phase = "loop"             # the notification wakes the task, which then sees it must stop
        guard = 0
        while self.phase is not None:       # end() returns only when the task has exited
            if self.phase == "rec" and self.stall:
                raise Hang("Mic.end() would wait forever for a stuck request")
            self.clock.t += 1
            self.step(self.clock.t)
            guard += 1
            assert guard < 200000

    def record(self, buf, rate, stereo):
        self.calls.append("record")
        if not self.begin():
            return False
        guard = 0
        while self._len(self.flip):         # the slot it fills next is still taken: wait for the task to move on
            if self.phase == "rec" and self.stall:
                raise Hang("record() would wait forever for a free slot")
            self.clock.t += 1
            self.step(self.clock.t)
            guard += 1
            if guard > 60000:
                raise Hang("record() would wait forever for a free slot")
        rid = self.next_id
        self.next_id += 1
        self.slots[self.flip] = {"buf": buf, "length": len(buf) // 2, "pos": 0, "id": rid}
        self.queued_at[rid] = self.clock.t
        if self.phase == "wait":
            self.phase = "loop"             # xTaskNotifyGive
        return True

    def isRecording(self):
        return (bool(self._len(0)) + bool(self._len(1))) if self.is_recording else 0

    # --- the driver's task, one millisecond at a time ---
    def step(self, t):
        if self.phase is None or self.phase == "wait":
            return
        if self.phase == "init":
            if t < self.init_until:
                return
            self.phase = "loop"
        if self.phase == "loop":
            if not self.running:
                self.is_recording = False
                self.phase = None
                return
            cur = 1 - self.flip
            if not self._len(cur):
                self.flip = 1 - self.flip
                cur = 1 - self.flip
                if not self._len(cur):
                    self.is_recording = False
                    self.phase = "wait"
                    return
            self.is_recording = True
            self.cur = cur
            self.phase = "rec"
            return
        if self.phase == "rec" and not self.stall:
            s = self.slots[self.cur]
            n = min(SAMPLES_PER_MS, s["length"] - s["pos"])
            buf = s["buf"]
            for k in range(s["pos"] * 2, (s["pos"] + n) * 2):
                buf[k] = s["id"] & 0xFF or 1
            self.writes.append((t, id(buf), s["id"]))
            s["pos"] += n
            if s["pos"] >= s["length"]:
                s["length"] = 0             # only after the last sample
                self.finished_at[s["id"]] = t
                self.phase = "loop"

    # --- helpers for the tests ---
    def free(self, buf):
        self.freed[id(buf)] = self.clock.t

    def writes_after_free(self):
        return [w for w in self.writes if w[1] in self.freed and w[0] > self.freed[w[1]]]

    def pending(self):
        return [s["id"] for s in self.slots if s and s["length"]]


def setup(pickup_ms=30):
    clock = Clock()
    fake = FakeMic(clock, pickup_ms)
    logs = []
    rec = mic.Recorder(fake, clock.ticks_ms, clock.ticks_diff, clock.sleep_ms, log=lambda *a: logs.append(a))
    return clock, fake, rec, logs


def boot(rec, fake):
    """voice_send's start-up: both buffers made once, Mic.begin(), the awaited warm-up, Mic.end()."""
    warmup = rec.buffer(3200)
    audio = rec.buffer(16000 * 2 * 6)
    fake.begin()
    problem = rec.record(warmup)
    rec.end()
    return warmup, audio, problem


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def model_reproduces_the_v0_11_0_bug():
    """The old start-up code (look once, end, free) leaves the warm-up queued; the next begin() writes into freed memory."""
    clock, fake, _rec, _ = setup()
    fake.begin()
    warmup = bytearray(3200)
    fake.record(warmup, 16000, False)
    loops = 0
    while fake.isRecording():
        loops += 1
        clock.sleep_ms(20)
    fake.end()
    fake.free(warmup)
    assert loops == 0, loops                                    # as seen on the Stick in all four I4 boots
    assert fake.pending() == [1], fake.pending()
    clock.run(60000)
    fake.begin()                                                # the press
    clock.run(300)
    assert fake.writes_after_free(), "expected writes into the freed buffer"


@case
def warmup_is_awaited_until_really_finished():
    clock, fake, rec, _ = setup(pickup_ms=30)
    warmup, audio, problem = boot(rec, fake)
    assert problem is None and rec.state == mic.OK, (problem, rec.state)
    assert fake.finished_at.get(1) is not None and all(b for b in warmup)
    fake.begin()                                                # a later press: nothing left queued, so nothing is written
    clock.run(1000)
    assert [w for w in fake.writes if w[2] == 1 and w[0] > fake.finished_at[1]] == []
    assert fake.pending() == []


@case
def delayed_start_within_the_timeout_completes():
    clock, fake, rec, _ = setup(pickup_ms=1200)
    _warmup, _audio, problem = boot(rec, fake)
    assert problem is None and rec.state == mic.OK, (problem, rec.state)


@case
def recording_returns_only_after_the_last_sample():
    clock, fake, rec, _ = setup()
    _warmup, audio, _ = boot(rec, fake)
    fake.begin()
    clock.run(1000)
    assert rec.record(audio) is None
    rid = fake.next_id - 1
    assert fake.finished_at[rid] <= clock.t and fake.pending() == []
    assert all(b == rid & 0xFF for b in audio)


@case
def start_not_seen_is_unsure_and_the_next_recording_waits_for_the_stale_request():
    clock, fake, rec, _ = setup(pickup_ms=5000)                 # the task does not pick the warm-up up in time
    warmup, audio, problem = boot(rec, fake)                    # end() stops the task before it ever records
    assert problem == mic.NOT_STARTED and rec.state == mic.UNSURE, (problem, rec.state)
    assert fake.pending() == [1]                                # the warm-up is still queued, exactly the dangerous case
    fake.pickup_ms = 30                                         # the next task is healthy
    clock.run(30000)
    fake.begin()                                                # the press: the new task records the stale warm-up first
    clock.run(1000)
    t_queue = None
    problem = rec.record(audio)
    for rid, t in fake.queued_at.items():
        if rid > 2:
            t_queue = t
    assert problem is None and rec.state == mic.OK, (problem, rec.state)
    assert fake.finished_at[1] < fake.queued_at[2] < fake.finished_at[2] <= t_queue, (fake.finished_at, fake.queued_at)
    assert all(b == 3 for b in audio)                           # the upload holds only the new request
    assert not fake.writes_after_free() and warmup is rec.buffers[1]


@case
def missed_short_interval_is_unsure_not_done():
    clock, fake, rec, _ = setup(pickup_ms=5)
    clock.stalls = [300]                                        # one 300 ms stall: the 100 ms warm-up starts and ends unseen
    warmup, audio, problem = boot(rec, fake)
    assert fake.finished_at.get(1) is not None                  # it did finish ...
    assert problem == mic.NOT_STARTED and rec.state == mic.UNSURE, (problem, rec.state)   # ... but that was not seen
    fake.begin()
    assert rec.record(audio) is None and rec.state == mic.OK    # proven idle by the drain, then recorded


@case
def interrupted_recording_is_settled_before_the_buffer_is_reused():
    """An exception while waiting (here: from the animation) leaves the request queued; it must finish before audio is reused."""
    clock, fake, rec, _ = setup()
    _warmup, audio, _ = boot(rec, fake)
    clock.run(5000)
    fake.pickup_ms = 50
    fake.begin()                                                # the press: a new task, not yet ready to pick anything up

    def boom():
        raise RuntimeError("draw failed")
    try:
        rec.record(audio, boom)
        raise AssertionError("expected the exception")
    except RuntimeError:
        pass
    assert rec.state == mic.UNSURE and rec.reason == mic.INTERRUPTED
    stale = fake.next_id - 1
    rec.end()                                                   # what the app's error handler does
    assert fake.pending() == [stale]                            # left queued by end()
    fake.begin()                                                # the next press
    assert rec.record(audio) is None and rec.state == mic.OK
    new = fake.next_id - 1
    assert fake.finished_at[stale] < fake.queued_at[new], "audio reused while the stale request could still write"
    assert not [w for w in fake.writes if w[2] == stale and w[0] >= fake.queued_at[new]]
    assert all(b == new & 0xFF for b in audio)


@case
def stuck_request_blocks_recording_and_end():
    clock, fake, rec, logs = setup()
    _warmup, audio, _ = boot(rec, fake)
    fake.begin()
    clock.run(500)
    fake.stall = True                                           # I2S stops delivering data
    problem = rec.record(audio)
    assert problem == mic.STUCK and rec.state == mic.BLOCKED, (problem, rec.state)
    calls = list(fake.calls)
    rec.end()                                                   # must not call Mic.end(): it would never return
    assert rec.record(audio) == mic.STUCK and fake.calls == calls
    assert any("blocked" in " ".join(map(str, a)) for a in logs)


@case
def drain_that_never_runs_blocks():
    """UNSURE with nothing left queued, then a task that does not pick the drain up in time: safety cannot be shown."""
    clock, fake, rec, _ = setup(pickup_ms=5)
    clock.stalls = [300]
    _warmup, audio, _ = boot(rec, fake)
    assert rec.state == mic.UNSURE and fake.pending() == []
    fake.pickup_ms = 5000
    fake.begin()
    problem = rec.record(audio)
    assert problem == mic.NOT_READY and rec.state == mic.BLOCKED, (problem, rec.state)
    calls = list(fake.calls)
    rec.end()
    assert rec.record(audio) == mic.NOT_READY and fake.calls == calls      # blocked: the driver is not touched again


@case
def slow_task_with_a_stale_request_is_absorbed_by_the_drivers_own_wait():
    """With the stale request still in the slot, the driver's record() itself waits for the slow task, so the drain is seen."""
    clock, fake, rec, _ = setup(pickup_ms=5000)
    _warmup, audio, _ = boot(rec, fake)
    assert rec.state == mic.UNSURE and fake.pending() == [1]
    fake.begin()
    assert rec.record(audio) is None and rec.state == mic.OK
    assert fake.finished_at[1] < fake.queued_at[3]


@case
def driver_refusing_to_start_queues_nothing():
    clock, fake, rec, _ = setup()
    _warmup, audio, _ = boot(rec, fake)
    fake.begin_ok = False
    assert rec.record(audio) == mic.FAILED and rec.state == mic.OK and fake.pending() == []
    fake.begin_ok = True
    fake.begin()
    assert rec.record(audio) is None


@case
def failed_drain_queue_stays_unsure():
    clock, fake, rec, _ = setup(pickup_ms=5000)
    _warmup, audio, _ = boot(rec, fake)
    fake.begin_ok = False
    assert rec.record(audio) == mic.FAILED and rec.state == mic.UNSURE


@case
def only_registered_buffers_and_they_stay_referenced():
    clock, fake, rec, _ = setup()
    warmup, audio, _ = boot(rec, fake)
    try:
        rec.record(bytearray(3200))
        raise AssertionError("an unregistered buffer was accepted")
    except ValueError:
        pass
    assert rec.buffers[0] is rec.drain and rec.buffers[1] is warmup and rec.buffers[2] is audio


# ---- how the app uses it (source checks: voice_send.py runs only on the Stick) ----------------------------------------
VOICE = (ROOT / "stick" / "voice_send.py").read_text()
MAIN = (ROOT / "stick" / "main.py").read_text()


@case
def app_never_drives_the_microphone_directly():
    for banned in ("M5.Mic.record", "M5.Mic.end", "M5.Mic.isRecording", "del warmup", "bytearray(16000 * 2 * 6)"):
        assert banned not in VOICE, banned
    assert "warmup = recorder.buffer(3200)" in VOICE and "audio = recorder.buffer(16000 * 2 * 6)" in VOICE
    assert VOICE.count("recorder.end()") == 5        # playback, chime, warm-up, after recording, error handler


@case
def upload_only_after_a_proven_recording():
    post = VOICE.index('SERVER + "/audio"')
    rec_call = VOICE.index("problem = recorder.record(audio, robot.update)")
    guard = VOICE.index("raise RuntimeError(problem)", rec_call)
    assert rec_call < guard < post
    assert VOICE.index("if recorder.state == BLOCKED:") < VOICE.index("M5.Speaker.end()\n            if not M5.Mic.begin()")


@case
def tracebacks_go_to_the_usb_log():
    assert "sys.print_exception(error)" in MAIN and "\nimport sys\n" in MAIN
    handler = VOICE.index('print("Error:", error)')
    assert VOICE.index("sys.print_exception(error)", handler) - handler < 60
    assert "import sys" in VOICE
    for src in (VOICE, MAIN, (ROOT / "stick" / "mic.py").read_text()):
        ast.parse(src)


@case
def deploy_uploads_mic_before_voice_send():
    files = (ROOT / "tools" / "deploy_stick.sh").read_text().split("FILES=(")[1].split(")")[0].split()
    assert files.index("mic.py") < files.index("voice_send.py") and "main.py" in files


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as error:
            failed += 1
            print("FAIL", fn.__name__, str(error)[:300])
        except Exception as error:                                              # noqa: BLE001
            failed += 1
            print("ERROR", fn.__name__, repr(error)[:300])
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
