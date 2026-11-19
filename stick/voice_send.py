
def mouth_level(data, pcm_start, rate, started):
    # Estimate the mouth opening from the reply audio at the current playback time.
    # This follows the loudness of the audio being played, based on elapsed time.
    pos = pcm_start + (time.ticks_diff(time.ticks_ms(), started) * rate // 1000) * 2
    end = min(len(data) - 2, pos + rate // 10)
    if pos >= end:
        return 0.0
    total = count = 0
    for off in range(pos, end, 32):
        total += abs(struct.unpack_from("<h", data, off)[0])
        count += 1
    return min(1.0, total / count / 6000)


def play_wav(data, rate, pcm_start):
    """Start a WAV at the configured voice volume (answers and shake clips). The volume is read here, at the start of the
    playback, never during one. At 0 percent nothing is played at all (true silence), but the same duration is
    returned so the mouth animation and the stop button behave exactly as when audible. Returns the time a muted
    playback ends, or None when it is really playing."""
    raw = device_state.voice_raw()
    if raw <= 0:
        return time.ticks_add(time.ticks_ms(), (len(data) - pcm_start) * 1000 // (rate * 2))
    recorder.end()
    M5.Speaker.begin()
    M5.Speaker.setVolume(raw)
    M5.Speaker.playWav(data)
    return None


def audio_playing(muted_until):
    if muted_until is None:
        return M5.Speaker.isPlaying()
    return time.ticks_diff(muted_until, time.ticks_ms()) > 0


def stop_audio(muted_until):
    if muted_until is None:
        M5.Speaker.stop()


SERVER = ""              # "http://<host>:<port>" from /flash/device_config.json (stick/server_config.py), set at start-up
WAIT_LIMIT_MS = 240000   # give up on one question after this long
POLL_MS = 1000           # how often to ask the server for this question's status
POLL_TIMEOUT_S = 3       # each status request is short and bounded
POLL_FAILURES = 5        # consecutive failed status requests before giving up


def auth_headers(extra=None):
    """Every request to the server carries the device key (docs/security.md); without one nothing is sent."""
    if not poller.key:
        raise RuntimeError("No device key")
    headers = {"X-Device-Key": poller.key}
    if extra:
        headers.update(extra)
    return headers


def server_problem(status):
    """A short, plain reason for a refused request (the screen has room for very little)."""
    if status in (401, 403):
        return "Key rejected"
    if status in (429, 503):
        return "Server busy"
    return "Server status: " + str(status)


def wait_for_result(job_id):
    # Poll this recording's status, animating between short, bounded requests.
    robot.set("thinking", elapsed=True)
    start = time.ticks_ms()
    next_poll = start
    failures = 0
    while True:
        M5.update()
        robot.update()
        time.sleep_ms(20)
        now = time.ticks_ms()
        if time.ticks_diff(now, start) > WAIT_LIMIT_MS:
            raise RuntimeError("Timed out")
        if time.ticks_diff(now, next_poll) < 0:
            continue
        next_poll = time.ticks_add(now, POLL_MS)
        try:
            response = requests.get(SERVER + "/status?id=" + job_id, timeout=POLL_TIMEOUT_S, headers=auth_headers())
            try:
                code = response.status_code
                status = response.json() if code == 200 else None
            finally:
                response.close()
        except Exception as error:
            failures += 1
            print("Status poll failed:", error)
            if failures >= POLL_FAILURES:
                raise RuntimeError("Server lost")
            continue
        if code != 200:
            raise RuntimeError(server_problem(code))     # refused (key, busy) or gone: say so at once, no retries
        failures = 0
        state = status.get("state")
        if state != "pending":
            return state, status.get("detail") or ""


def play_reply(job_id):
    state, detail = wait_for_result(job_id)
    if state == "no_speech":
        # The server found no speech in the recording: a normal outcome.
        robot.set("puzzled", "No speech", "Try again")
        robot.hold(2500)
        return
    if state == "error":
        raise RuntimeError(detail or "Server error")
    if state != "ready":
        raise RuntimeError("Server restarted" if state == "unknown" else "Bad status")

    robot.relabel("Downloading")
    response = requests.get(SERVER + "/reply.wav?id=" + job_id, timeout=30, headers=auth_headers())
    try:
        if response.status_code != 200:
            raise RuntimeError("Voice reply unavailable")

        data = bytearray()
        while True:
            chunk = response.raw.read(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 2000000:
                raise RuntimeError("Voice reply too large")
            robot.update()
    finally:
        response.close()

    if len(data) < 44:
        raise RuntimeError("Voice file incomplete")
    rate = struct.unpack_from("<I", data, 24)[0]
    pcm_start = data.find(b"data", 12, 200) + 8

    robot.set("speaking")
    # playWav plays from memory in the background, so the face can animate.
    muted_until = play_wav(data, rate, pcm_start)
    started = time.ticks_ms()

    # A press during the answer stops it. It only counts once the button has
    # been seen released during playback, so a press held from before does not.
    M5.update()
    stop_armed = not M5.BtnA.isPressed()
    stopped = False
    while audio_playing(muted_until):
        M5.update()
        if M5.BtnA.isPressed():
            if stop_armed:
                stop_audio(muted_until)
                stopped = True
                break
        else:
            stop_armed = True
        robot.update(level=mouth_level(data, pcm_start, rate, started))
        time.sleep_ms(20)
    del data

    if stopped:
        robot.set("idle")
        # The stopping press must not also start a recording: wait for release.
        while M5.BtnA.isPressed():
            M5.update()
            robot.update()
            time.sleep_ms(20)
    return stopped


def load_shake_clips():
    clips = []
    for i in range(1, 10):
        try:
            with open("/flash/sounds/shake%d.wav" % i, "rb") as f:
                clips.append(f.read())
        except OSError:
            break
    return clips


def react_to_shake():
    # Dizzy face plus a short prerecorded line; no network needed.
    global last_clip
    robot.set("dizzy")
    if not shake_clips:
        robot.hold(1500)
        return
    choices = [i for i in range(len(shake_clips)) if i != last_clip] or [0]
    last_clip = random.choice(choices)
    data = shake_clips[last_clip]
    rate = struct.unpack_from("<I", data, 24)[0]
    pcm_start = data.find(b"data", 12, 200) + 8
    robot.hold(300)

    muted_until = play_wav(data, rate, pcm_start)
    started = time.ticks_ms()
    while audio_playing(muted_until):
        M5.update()
        robot.update(level=mouth_level(data, pcm_start, rate, started))
        time.sleep_ms(20)
    robot.update(level=0)
    robot.hold(400)


def show_notification(note):
    # A Claude Code event (or a chime-only reminder) from the Mac: a face and a soft chime, then back to idle.
    # Returns True if a button press dismissed it.
    reminder = note.get("kind") == "reminder"
    attention = note.get("kind") == "attention" or reminder
    print("Notification:", note.get("kind"), note.get("label"))
    robot.set("attention" if attention else "done",
              "Reminder" if reminder else ("Needs you" if attention else "Reply ready"), note.get("label") or "")
    chime = device_state.chime_raw()        # read once, at the start of this notification; 0 = silent
    if chime > 0:
        recorder.end()
        M5.Speaker.begin()
        M5.Speaker.setVolume(chime)
    tones = CHIME_ATTENTION if attention else CHIME_DONE
    show_ms = SHOW_REMINDER_MS if reminder else (SHOW_ATTENTION_MS if attention else SHOW_DONE_MS)
    start = time.ticks_ms()
    next_tone = 0
    index = 0
    M5.update()
    armed = not M5.BtnA.isPressed()     # a press already held does not count as a dismissal
    dismissed = False
    while time.ticks_diff(time.ticks_ms(), start) < show_ms:
        elapsed = time.ticks_diff(time.ticks_ms(), start)
        if chime > 0 and index < len(tones) and elapsed >= next_tone:
            freq, duration = tones[index]
            M5.Speaker.tone(freq, duration)
            next_tone = elapsed + duration + 60
            index += 1
        M5.update()
        if M5.BtnA.isPressed():
            if armed:
                print("Notification dismissed by button")
                dismissed = True
                break                   # a fresh press dismisses the notification
        else:
            armed = True
        robot.update()
        time.sleep_ms(20)
    if chime > 0:
        M5.Speaker.stop()
    robot.set("idle")
    while M5.BtnA.isPressed():          # the dismissing press must not start a recording
        M5.update()
        robot.update()
        time.sleep_ms(20)
    return dismissed


# ---- one-time reminders (docs/reminders.md) -------------------------------------------------------------------
def post_reminder_state(occ, state):
    """Tell the Mac a stage or result right away (best effort, short timeout). The sync repeats results later, so a lost
    report here is not a lost result."""
    try:
        body = json.dumps({"occ": occ, "state": state, "boot": device_state.boot})
        response = requests.post(SERVER + "/device/reminder/state", data=body, timeout=2,
                                 headers=auth_headers({"Content-Type": "application/json"}))
        response.close()
    except Exception as error:
        print("Reminder state not sent:", state, error)


def fetch_reminder_audio(occ):
    """Download this reminder's own audio (authenticated). None if the server refuses it (edited, expired, cancelled)."""
    response = requests.get(SERVER + "/device/reminder.wav?occ=%d" % occ, timeout=30, headers=auth_headers())
    try:
        if response.status_code != 200:
            return None
        data = bytearray()
        while True:
            chunk = response.raw.read(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 900000:
                return None
            robot.update()
    finally:
        response.close()
    return data if len(data) >= 44 else None


def speak_reminder(rem):
    robot.set("attention", "Reminder", rem["label"])
    try:
        data = fetch_reminder_audio(rem["occ"])
    except Exception as error:
        print("Reminder audio error:", error)
        data = None
    if data is None:
        return "media_failed"
    rate = struct.unpack_from("<I", data, 24)[0]
    pcm_start = data.find(b"data", 12, 200) + 8
    robot.set("speaking", "Reminder", rem["label"])
    muted_until = play_wav(data, rate, pcm_start)         # the voice volume; 0 percent is silent (the face still shows it)
    started = time.ticks_ms()
    post_reminder_state(rem["occ"], "presented")
    # A fresh press stops the speech. A press held from before does not count: it must be seen released first.
    M5.update()
    armed = not M5.BtnA.isPressed()
    stopped = False
    while audio_playing(muted_until):
        M5.update()
        if M5.BtnA.isPressed():
            if armed:
                stop_audio(muted_until)
                stopped = True
                break
        else:
            armed = True
        robot.update(level=mouth_level(data, pcm_start, rate, started))
        time.sleep_ms(20)
    del data
    return "dismissed" if stopped else ("muted" if muted_until is not None else "completed")


def deliver_reminder(rem):
    """Present one reminder: wake the screen, chime or speak, report what really happened. Called only while idle."""
    occ = rem["occ"]
    print("Reminder:", occ, rem["mode"])
    if device_state.sleeper.asleep:
        wake_screen()
    device_state.reminders.begin(occ)                     # a reset from here on is reported as 'interrupted'
    post_reminder_state(occ, "received")
    try:
        if rem["mode"] == "speak":
            state = speak_reminder(rem)
        else:
            post_reminder_state(occ, "presented")
            quiet = device_state.chime_raw() <= 0
            state = "dismissed" if show_notification({"kind": "reminder", "label": rem["label"]}) else ("muted" if quiet else "completed")
    except Exception as error:
        print("Reminder error:", error)
        sys.print_exception(error)
        M5.Speaker.stop()
        state = "failed"
    device_state.reminders.finish(occ, state)
    post_reminder_state(occ, state)
    robot.set("idle")
    while M5.BtnA.isPressed():                            # a stopping press must not also start a recording
        M5.update()
        robot.update()
        time.sleep_ms(20)
    shake.reset(time.ticks_ms())
    device_state.sleeper.activity(time.ticks_ms())        # the normal idle-sleep timer starts again


def wake_screen():
    """Wake the screen: draw the awake face first, then restore the saved awake brightness."""
    now = time.ticks_ms()
    robot.set("idle")
    device_state.screen_apply("wake", now)      # also restarts the idle timer
    shake.reset(now)                            # no delayed shake trigger from the motion around the wake


def sleep_step():
    """One screen-sleep decision. Called only while idle (never during recording, thinking, downloading,
    playback, a shake reaction or a notification), and independent of Wi-Fi and the server."""
    now = time.ticks_ms()
    action = device_state.sleeper.step(now)
    if action == "sleep":
        robot.set("sleeping")                   # the face is shown briefly at the awake brightness...
        device_state.screen_apply("sleep", now)
        shake.reset(now)
    elif action in ("dim", "off"):
        device_state.screen_apply(action, now)  # ...then the backlight is lowered or turned off
        shake.reset(now)
    elif action == "wake":
        wake_screen()


import M5
import sys
import time
import network
import requests
import struct
import random
import json
import server_config
from robot import Robot
from shake import ShakeDetector
from device import DeviceState, DeviceSync
from mic import Recorder, BLOCKED

# Claude Code notifications: soft chimes (frequency Hz, duration ms) and how long the face stays.
# Their volume is the chime_volume setting (DeviceState.chime_raw), default exactly 70 as before.
CHIME_DONE = ((784, 110), (1047, 170))
CHIME_ATTENTION = ((587, 110), (587, 110), (784, 170))
SHOW_DONE_MS = 2500
SHOW_ATTENTION_MS = 4000
SHOW_REMINDER_MS = 6000

M5.begin()

# The first recording after power-on comes back as all zeros, so make a
# short throwaway recording before the first real one.
# The microphone driver keeps a raw pointer to every buffer it is given and may write into it after the app thought the
# recording was over (stick/mic.py), so both recording buffers are made once, here, kept for the whole app, and every
# recording, the warm-up included, is awaited until the driver has really finished it.
recorder = Recorder(M5.Mic, time.ticks_ms, time.ticks_diff, time.sleep_ms)
warmup = recorder.buffer(3200)
audio = recorder.buffer(16000 * 2 * 6)
M5.Speaker.end()
M5.Mic.begin()
problem = recorder.record(warmup)
if problem:
    print("Mic warm-up:", problem)
recorder.end()

wifi = network.WLAN(network.STA_IF)
robot = Robot()
SERVER_HOST, SERVER_PORT, CONFIG_ERROR = server_config.load()
if SERVER_HOST is None:
    # No usable server address: never guess one. Say so on the screen and the serial console, and send nothing anywhere.
    print("Device configuration problem:", CONFIG_ERROR)
    robot.set("error", "No server", "Setup needed")
    while True:
        M5.update()
        robot.update()
        time.sleep_ms(100)
SERVER = "http://%s:%d" % (SERVER_HOST, SERVER_PORT)
shake_clips = load_shake_clips()
last_clip = None
shake = ShakeDetector()
shake.reset(time.ticks_ms())
try:
    import esp32
    _firmware = esp32.firmware_info()[3]
except Exception:
    _firmware = ""
device_state = DeviceState(M5.Lcd, M5.Power, wifi, _firmware)    # restores the cached brightness
poller = DeviceSync(device_state, SERVER_HOST, SERVER_PORT)

while True:
    M5.update()
    if M5.BtnA.isPressed():
        poller.abort()
        if device_state.sleeper.asleep:
            # The first press while asleep only wakes the screen. The whole press is consumed, however long it is
            # held, so it can never leak into a recording; recording needs a new press.
            wake_screen()
            while M5.BtnA.isPressed():
                M5.update()
                robot.update()
                time.sleep_ms(20)
            continue
        while M5.BtnA.isPressed():
            M5.update()
            time.sleep_ms(20)

        if not wifi.isconnected():
            robot.set("error", "No Wi-Fi")
            robot.hold(2000)
            robot.set("idle")
            shake.reset(time.ticks_ms())
            device_state.sleeper.activity(time.ticks_ms())
            continue

        try:
            if recorder.state == BLOCKED:
                raise RuntimeError("Restart Stick")     # the microphone driver is stuck (the reason is in the USB log)
            M5.Speaker.end()
            if not M5.Mic.begin():
                raise RuntimeError("Microphone failed")

            if not poller.key:
                raise RuntimeError("No device key")     # never record and upload without one (pair: tools/stick_setup.py key)
            robot.set("ready")
            robot.hold(1000)
            robot.set("listening", duration_ms=6000)

            problem = recorder.record(audio, robot.update)
            if problem:
                raise RuntimeError(problem)             # never upload a recording that is not proven complete

            recorder.end()
            robot.set("thinking", "Sending")

            response = requests.post(
                SERVER + "/audio",
                data=audio,
                headers=auth_headers({"Content-Type": "application/octet-stream"}),
                timeout=30,
            )
            try:
                status = response.status_code
                job_id = response.json().get("id") if status == 200 else None
            finally:
                response.close()

            if status != 200:
                raise RuntimeError(server_problem(status))
            if not job_id:
                raise RuntimeError("No job id")

            if play_reply(job_id):
                print("Playback stopped by button.")
            else:
                print("Audio sent. Check the Mac for transcription.")
                robot.set("idle")
                robot.hold(2000)

        except Exception as error:
            recorder.end()
            print("Error:", error)
            sys.print_exception(error)
            robot.set("error", "Error", str(error))
            robot.hold(3000)

        robot.set("idle")
        # A press held through the wait or the answer must not start a new
        # recording: require release before the next press counts.
        while M5.BtnA.isPressed():
            M5.update()
            robot.update()
            time.sleep_ms(20)
        # Motion during recording, requests or answers is discarded, not replayed.
        shake.reset(time.ticks_ms())
        device_state.sleeper.activity(time.ticks_ms())      # a finished interaction restarts the idle timer

    elif shake.feed(M5.Imu.getAccel(), time.ticks_ms()):
        poller.abort()
        if device_state.sleeper.asleep:
            wake_screen()                                   # a deliberate shake wakes the device, then the usual reaction
        react_to_shake()
        robot.set("idle")
        # Cooldown starts after playback; the device must also settle to rearm.
        shake.reset(time.ticks_ms(), cooldown=True)
        device_state.sleeper.activity(time.ticks_ms())

    else:
        if shake.armed and wifi.isconnected():
            # Idle and still: look for a Claude Code notification (never blocks). Polling is not activity.
            note = poller.step()
            if note:
                if device_state.sleeper.asleep:
                    wake_screen()                           # a notification may wake the screen and play its chime
                show_notification(note)
                poller.ack(note["id"])
                shake.reset(time.ticks_ms())
                device_state.sleeper.activity(time.ticks_ms())   # afterwards the configured idle timer starts again
        if shake.armed and device_state.reminders.pending:
            rem = device_state.reminders.take(time.ticks_ms())     # idle and still: deliver it now, or drop it if it expired
            if rem:
                deliver_reminder(rem)
        sleep_step()                                        # local: works without Wi-Fi or the server

    if device_state.sleeper.state != "off":
        robot.update()
    time.sleep_ms(20)
