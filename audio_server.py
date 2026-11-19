import json
import os
import queue
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from pathlib import Path
import wave
from datetime import datetime
from zoneinfo import ZoneInfo

import stick_config

# Host configuration (docs/configuration.md). Checked first, before the heavy imports below, so a missing or invalid configuration is reported at once.
try:
    CONFIG = stick_config.load()
except stick_config.ConfigError as error:
    print(error, file=sys.stderr, flush=True)
    sys.exit(2)

import mlx_whisper
from speech_detect import detect_speech
from notifications import NotificationQueue
from device_link import DeviceLink, KeyGate, MAX_BODY as SYNC_MAX_BODY
import panel_server
import tts
from settings_store import StoreError
from identity import IDENTITY_PROMPT
from conversation import (Conversation, HISTORY_RULES, RESET_REPLY, build_claude_input,
                          is_reset_command)

folder = CONFIG.recordings_dir
folder.mkdir(parents=True, exist_ok=True)

# Speech recognition model: the pinned local copy made by tools/setup_mac.py when it exists (complete: config + weights), otherwise the model's name,
# which Hugging Face downloads into its own cache on first use (the behaviour of every installation made before the setup tool existed).
WHISPER_MODEL = (str(CONFIG.whisper_dir) if (CONFIG.whisper_dir / "config.json").is_file() and (CONFIG.whisper_dir / "weights.safetensors").is_file()
                 else "mlx-community/whisper-large-v3-turbo")

# The Claude call (docs/privacy-review.md). Each voice question runs `claude -p` isolated from the user's own Claude Code setup:
# - tools: web search and web fetch only, explicitly allowed; every other tool call is denied (dontAsk), and every MCP / claude.ai
#   connector tool is removed (strict MCP config, "mcp__*" disallowed). Connectors (email, calendar...) are DISABLED in the preview:
#   an account can carry write-capable connectors that a per-name list could not safely cover;
# - settings: only project settings from an empty working folder are loaded (none), never the user's settings, permission rules,
#   default mode or hooks; all hooks disabled; skills disabled;
# - context: no CLAUDE.md files and no auto memory; the working folder is an empty private folder, not the project;
# - local storage by Claude Code: no session transcript and no prompt history.
# Checked against Claude Code 2.1.288 with synthetic data (tests/README.md). Anthropic's own retention is separate (account terms).
CLAUDE_TOOLS = "WebSearch,WebFetch"
CLAUDE_ALLOWED_TOOLS = "WebSearch,WebFetch"
CLAUDE_ISOLATION = ["--permission-mode", "dontAsk", "--disallowedTools", "mcp__*", "--strict-mcp-config",
                    "--setting-sources", "project", "--settings", json.dumps({"disableAllHooks": True}),
                    "--disable-slash-commands", "--no-session-persistence"]
CLAUDE_ENV = {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
              "STICK_NOTIFY_SKIP": "1"}
CLAUDE_WORKDIR = CONFIG.data_dir / "claude-workdir"       # empty and private: no project files, settings or memory to load
ASSISTANT_PROMPT = (
    "You are a Hebrew voice assistant; your reply is read aloud by text-to-speech. "
    "Answer in short, natural spoken Hebrew, usually one to three sentences. "
    "Never use tables, Markdown, bullet points, emoji, links or code. "
    "For current information use web search. You cannot read the user's email or calendar in this "
    "version: if asked, say so briefly. Never invent results: if a tool fails or returns nothing, "
    "say so briefly. Interpret all dates and times in the Asia/Jerusalem time zone "
    "(today, tomorrow, this week). Only search and read: never send, create, change or "
    "delete anything."
)


def assistant_prompt(with_history=False):
    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    # Permanent instructions, in this order: style, identity (identity.py), time. The conversation history is appended after them (HISTORY_RULES)
    # and, as data, in the user message: a reset or a restart can never remove the identity.
    prompt = (ASSISTANT_PROMPT + IDENTITY_PROMPT + f" The current local time is {now:%Y-%m-%d %H:%M} "
              f"(UTC{now:%z}). Tool timestamps ending in Z are UTC: convert them "
              f"using exactly this offset before saying them.")
    return prompt + HISTORY_RULES if with_history else prompt

# One job per uploaded recording. A single worker processes them in order, so
# the Stick can poll /status while Whisper, Claude and TTS run.
KEEP_JOBS = 10

# LAN security (docs/security.md). Every route a Stick calls needs the device key in the X-Device-Key header, checked before any body
# is read and before any job, transcription, Claude or TTS work; there is no unauthenticated route or fallback on the device port.
# This is authentication over plain HTTP: it does NOT encrypt audio, answers or the key on the network.
MAX_AUDIO_BYTES = 1_000_000                 # 31 s of 16 kHz 16-bit mono
MAX_PENDING_JOBS = int(os.environ.get("STICK_MAX_PENDING_JOBS", "2"))   # waiting or running recordings; more are refused (503)
UPLOADS_PER_MIN = float(os.environ.get("STICK_UPLOADS_PER_MIN", "6"))   # sustained authenticated uploads; a burst of UPLOAD_BURST
UPLOAD_BURST = 3
REQUEST_TIMEOUT_S = float(os.environ.get("STICK_REQUEST_TIMEOUT_S", "15"))   # reading a request line, headers or body; a slow client cannot hold a thread
MAX_CONNECTIONS = 16                        # concurrent connections on the device port; more are closed at once
jobs = {}
jobs_lock = threading.Lock()
notifications = NotificationQueue()
conversation = Conversation()   # short follow-up memory, in server memory only
notified = set()   # ids already logged as delivered to the Stick
work = queue.Queue()


class Job:
    def __init__(self, job_id, audio):
        self.id = job_id
        self.audio = audio
        self.state = "pending"     # pending, ready, no_speech, error
        self.detail = ""
        self.reply = None
        self.created = time.monotonic()
        self.done = threading.Event()

    def finish(self, state, detail="", reply=None):
        self.state, self.detail, self.reply = state, detail, reply
        self.audio = None
        self.done.set()


def pending_jobs():
    with jobs_lock:
        return sum(1 for j in jobs.values() if j.state == "pending")


def new_job(audio):
    """A job for this recording, or None when MAX_PENDING_JOBS are already waiting (nothing is queued then)."""
    with jobs_lock:
        if sum(1 for j in jobs.values() if j.state == "pending") >= MAX_PENDING_JOBS:
            return None
        job = Job(str(time.time_ns() // 1_000_000), audio)
        jobs[job.id] = job
        for old in sorted(jobs, key=lambda k: jobs[k].created)[:-KEEP_JOBS]:
            del jobs[old]
    work.put(job)
    return job


def next_notification(after, client_ip):
    """Next pending Claude Code notification after the cursor, or None (shared by /notify/next and sync)."""
    item = notifications.next_after(after)
    if item is not None and item["id"] not in notified:
        notified.add(item["id"])
        print(f"[notify] delivered {item['kind']} '{item['label']}' to {client_ip}", flush=True)
    return item


def synthesize_reminder(text, out_path):
    """Reminder speech: the same local TTS as answers, into the reminder's own file (never the answer files)."""
    raw = Path(out_path).with_suffix(".raw.wav")
    try:
        with tts.lock:
            tts.render(CONFIG.project_root, text, raw, out_path, timeout=90, python=CONFIG.tts_python, models=CONFIG.models_dir, ffmpeg=CONFIG.ffmpeg or "ffmpeg")
    finally:
        raw.unlink(missing_ok=True)


# Desktop panel (docs/desktop-panel.md). STICK_PANEL=0 turns the panel and /device/sync off.
PANEL_PORT = CONFIG.panel_port
device = None
if os.environ.get("STICK_PANEL", "1") != "0":
    try:
        device = DeviceLink(next_notification, busy_hint=lambda: any(j.state == "pending" for j in list(jobs.values())),
                            synth=synthesize_reminder,
                            data_dir=CONFIG.data_dir, secrets_dir=CONFIG.secrets_dir)
        device.reminders.start_worker()
        print("[panel] enabled; device key " + ("provisioned" if device.key_provisioned else "NOT provisioned"), flush=True)
    except (StoreError, OSError) as error:
        print(f"[panel] disabled: {error}", flush=True)
# The same key check guards every LAN route, with or without the panel.
gate = device.gate if device is not None else KeyGate(CONFIG.secrets_dir / "device_key")
if not gate.provisioned:
    print("[security] device key NOT provisioned: every request from the Stick will be refused (tools/stick_setup.py key)", flush=True)
upload_limiter = panel_server.RateLimiter(rate=UPLOADS_PER_MIN / 60.0, burst=UPLOAD_BURST)


def accept_notification(kind, label, session, prompt):
    """From the Claude Code hook, through the loopback-only panel listener (panel_server, POST /hook/notify)."""
    accepted, reason = notifications.add(kind, label, session, prompt)
    print(f"[notify] {kind} '{label}': {reason}", flush=True)
    return accepted, reason


def claude_workdir():
    """The empty private folder the Claude call runs in (created on first use, 0700)."""
    CLAUDE_WORKDIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    return CLAUDE_WORKDIR


def process(job):
    """Run one recording through speech detection, Whisper, Claude and TTS."""
    (folder / "reply_for_stick.wav").unlink(missing_ok=True)
    audio_path = folder / "recording.wav"
    with wave.open(str(audio_path), "wb") as audio_file:
        audio_file.setnchannels(1)
        audio_file.setsampwidth(2)
        audio_file.setframerate(16000)
        audio_file.writeframes(job.audio)

    try:
        has_speech, details = detect_speech(audio_path)
    except Exception as error:
        # Never block a question because of the detector; fall through to Whisper.
        has_speech, details = True, {"reason": f"detector error: {error}"}
    print(f"\n[job {job.id}] Speech detection: {details}", flush=True)
    if not has_speech:
        (folder / "transcript.txt").write_text("", encoding="utf-8")
        print("No speech detected - skipping Whisper, Claude and TTS.", flush=True)
        return job.finish("no_speech")

    print("Audio received. Transcribing...", flush=True)
    try:
        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=WHISPER_MODEL,
            language="he",
            task="transcribe",
            temperature=0,
        )
    except Exception as error:
        print(f"Transcription error: {error}", flush=True)
        return job.finish("error", "Transcription failed")
    text = result["text"].strip()
    (folder / "transcript.txt").write_text(text, encoding="utf-8")
    print("\n--- התמלול ---", flush=True)
    print(text or "(No speech detected)", flush=True)
    if not text:
        return job.finish("no_speech")

    if is_reset_command(text):
        # A standalone "new conversation" command: forget everything, no Claude call.
        conversation.reset()
        print("[memory] new conversation started", flush=True)
        (folder / "reply.txt").write_text(RESET_REPLY, encoding="utf-8")
        voice = synthesize(RESET_REPLY)
        if voice is None:
            return job.finish("error", "Voice failed")
        return job.finish("ready", reply=voice)

    # Jobs run one at a time in upload order, so this history already includes every
    # earlier completed exchange; the epoch stops a job from committing across a reset.
    epoch, history = conversation.begin()
    print(f"[memory] {len(history)} earlier exchange(s) in context", flush=True)

    print("Asking Claude...", flush=True)
    try:
        answer = subprocess.run(
            [CONFIG.claude or "claude", "-p", "--tools", CLAUDE_TOOLS,
             "--allowedTools", CLAUDE_ALLOWED_TOOLS, *CLAUDE_ISOLATION,
             "--output-format", "text",
             "--append-system-prompt", assistant_prompt(with_history=bool(history))],
            input=build_claude_input(text, history), capture_output=True, text=True,
            timeout=120, cwd=str(claude_workdir()),
            env=dict(os.environ, **CLAUDE_ENV),
        )
    except subprocess.TimeoutExpired:
        print("Claude error: timed out", flush=True)
        return job.finish("error", "Claude timed out")
    if answer.returncode != 0:
        print("Claude error:", answer.stderr or answer.stdout, flush=True)
        return job.finish("error", "Claude failed")
    reply = answer.stdout.strip()
    if not reply:
        print("Claude error: empty answer", flush=True)
        return job.finish("error", "Empty answer")
    (folder / "reply.txt").write_text(reply, encoding="utf-8")
    print("\nCLAUDE:\n" + reply, flush=True)

    voice = synthesize(reply)
    if voice is None:
        return job.finish("error", "Voice failed")      # an exchange without a voiced reply is not completed

    # The exchange counts as completed once its reply exists. It records what the assistant
    # generated, not what the user heard: stopping playback on the Stick does not change it.
    if conversation.commit(epoch, text, reply):
        print(f"[memory] saved; {conversation.size()} exchange(s) kept", flush=True)
    else:
        print("[memory] not saved: the conversation was reset meanwhile", flush=True)
    job.finish("ready", reply=voice)


def synthesize(reply):
    """Turn reply text into the WAV the Stick plays. Returns the bytes, or None on failure."""
    print("Generating voice...", flush=True)
    target = folder / "reply_for_stick.wav"
    temporary = folder / "reply_for_stick.tmp.wav"
    try:
        with tts.lock:
            tts.render(CONFIG.project_root, reply, folder / "reply.wav", temporary, python=CONFIG.tts_python, models=CONFIG.models_dir, ffmpeg=CONFIG.ffmpeg or "ffmpeg")
    except (subprocess.SubprocessError, OSError) as error:
        print(f"Voice error: {error}", flush=True)
        return None
    temporary.replace(target)
    print("Voice ready for the Stick!", flush=True)
    return target.read_bytes()


def worker():
    while True:
        job = work.get()
        try:
            process(job)
        except Exception as error:
            print(f"[job {job.id}] Unexpected error: {error}", flush=True)
            job.finish("error", "Server error")
        print(f"[job {job.id}] {job.state} after {time.monotonic() - job.created:.1f}s", flush=True)


class AudioReceiver(BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT_S

    def log_message(self, format, *args):
        # The Stick polls /status and /device/sync constantly; keep the console readable. Paths never carry the key.
        if not self.path.startswith(("/status", "/device/sync", "/device/reminder")):
            super().log_message(format, *args)

    def send_json(self, data, code=200, extra=()):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        """The device key, checked before anything is read or done. Answers 401/429 itself and returns False when refused."""
        refused = gate.check(self.headers.get("X-Device-Key", ""), self.client_address[0])
        if refused:
            self.send_json(refused[1], refused[0])
            return False
        return True

    def body_size(self, limit):
        """The declared body size, or None after answering 411/413 (nothing is read then)."""
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_json({"error": "length required"}, 411)
            return None
        if not 0 < size <= limit:
            self.send_json({"error": "bad size"}, 413)
            return None
        return size

    def send_result(self, job):
        if job is None:
            self.send_json({"error": "unknown recording"}, 404)
        elif job.state == "ready":
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(job.reply)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(job.reply)
        elif job.state == "no_speech":
            # Normal outcome: the recording had no speech.
            self.send_response(204)
            self.send_header("X-Outcome", "no-speech")
            self.end_headers()
        elif job.state == "pending":
            self.send_json({"state": "pending"}, 202)
        else:
            self.send_json({"error": job.detail or "processing failed"}, 500)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/device/reminder.wav" and device is not None:
            status, result = device.reminder_media(self.headers.get("X-Device-Key", ""),
                                                   parse_qs(url.query).get("occ", [""])[0], self.client_address[0])
            if status != 200:
                return self.send_json(result, status)
            body = result.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(body)
        if url.path not in ("/status", "/reply.wav"):
            return self.send_json({"error": "not found"}, 404)       # (no /notify/next: notifications reach the Stick by /device/sync)
        if not self.authorized():
            return
        job_id = parse_qs(url.query).get("id", [None])[0]
        if not job_id:
            return self.send_json({"error": "id required"}, 400)      # never "the latest recording"
        with jobs_lock:
            job = jobs.get(job_id)
        if url.path == "/status":
            if job is None:
                return self.send_json({"state": "unknown"})
            return self.send_json({"state": job.state, "detail": job.detail, "elapsed": round(time.monotonic() - job.created, 1)})
        return self.send_result(job)

    def do_POST(self):
        if self.path == "/device/sync" and device is not None:
            return self.handle_device_sync()
        if self.path == "/device/reminder/state" and device is not None:
            return self.handle_reminder_state()
        if self.path != "/audio":
            return self.send_json({"error": "not found"}, 404)       # (no /notify: the hook uses the loopback panel listener)
        if not self.authorized():
            return
        size = self.body_size(MAX_AUDIO_BYTES)
        if size is None:
            return
        if size % 2:
            return self.send_json({"error": "invalid recording size"}, 400)
        if not upload_limiter.take():
            return self.send_json({"error": "too many recordings"}, 429, [("Retry-After", "10")])
        if pending_jobs() >= MAX_PENDING_JOBS:
            return self.send_json({"error": "busy"}, 503, [("Retry-After", "10")])
        print(f"Receiving audio: expecting {size} bytes", flush=True)
        data = self.rfile.read(size)
        if len(data) != size:
            return self.send_json({"error": "incomplete recording"}, 400)
        job = new_job(data)
        if job is None:
            return self.send_json({"error": "busy"}, 503, [("Retry-After", "10")])
        self.send_json({"id": job.id})

    def handle_device_sync(self):
        if not self.authorized():
            return
        size = self.body_size(SYNC_MAX_BODY)
        if size is None:
            return
        status, data = device.sync(self.headers.get("X-Device-Key", ""), self.rfile.read(size), self.client_address[0])
        self.send_json(data, status)

    def handle_reminder_state(self):
        if not self.authorized():
            return
        size = self.body_size(256)
        if size is None:
            return
        status, data = device.reminder_state(self.headers.get("X-Device-Key", ""), self.rfile.read(size), self.client_address[0])
        self.send_json(data, status)


class BoundedServer(ThreadingHTTPServer):
    """At most MAX_CONNECTIONS connections at once on the device port; further ones are closed immediately."""
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


threading.Thread(target=worker, daemon=True).start()
print("Ready: audio receiver + Hebrew transcription", flush=True)
server = BoundedServer((CONFIG.bind_host, CONFIG.device_port), AudioReceiver)
if device is not None:
    try:
        panel = panel_server.start(device, PANEL_PORT, notify=accept_notification)
        print(f"[panel] http://127.0.0.1:{panel.server_address[1]}/", flush=True)
    except OSError as error:
        print(f"[panel] could not start on port {PANEL_PORT}: {error}", flush=True)
server.serve_forever()
