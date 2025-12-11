import os
import json
import base64
import asyncio
import time
import uuid
import subprocess
import requests  # ✅ ElevenLabs API
import logging
import redis.asyncio as redis  # ✅ Redis client (async)
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles  # ✅ Serving audio
from twilio.twiml.voice_response import VoiceResponse, Start, Stream
from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents
from openai import OpenAI
from dotenv import load_dotenv

# 🔄 Load .env file
load_dotenv("/root/Fast-API-App/.env")

# 🗂️ Log file config
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = f"{LOG_DIR}/app.log"
os.makedirs(LOG_DIR, exist_ok=True)

# 🛠️ Touch the log file to verify path
with open(LOG_FILE, "a") as f:
    f.write("🟢 Log file was touched.\n")

# 🔧 Setup Rotating File Handler
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10_000_000, backupCount=3)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(
    "[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
))

# 🖥️ Optional: Stream to terminal
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(
    "[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
))

# 🚨 IMPORTANT: Don't call logging.basicConfig after handlers are added
# It won't do anything once handlers are set — skip it!
# logging.basicConfig(...) ← REMOVE THIS

# 🔗 Apply handlers
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# 🔍 Quick alias for log
log = logger.info

# ✅ Prove it’s working
logger.info("✅ Log setup complete.")

# 🆔 Instance identifier (for distributed logging)
INSTANCE = (
    os.getenv("FLY_ALLOC_ID")      # Fly.io VM ID
    or os.getenv("HOSTNAME")       # Docker/k8s fallback
    or os.uname().nodename         # Final fallback
)
logger.info(f"🆔 App instance ID: {INSTANCE}")

# 🔐 Load API keys
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")

# 🚫 Fail fast if missing secrets
if not DEEPGRAM_API_KEY:
    raise RuntimeError("Missing DEEPGRAM_API_KEY in environment")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY in environment")
if not ELEVENLABS_API_KEY:
    raise RuntimeError("Missing ELEVENLABS_API_KEY in environment")

# 🧠 OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# 🧠 In-memory session
session_memory = {}
# 🧠 Redis client (for shared session state)
REDIS_URL = os.getenv("REDIS_URL")

redis_client = None
if not REDIS_URL:
    log("⚠️ REDIS_URL not set. Redis features are disabled.")
else:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        log("✅ Redis client initialized from REDIS_URL")
    except Exception as e:
        log(f"❌ Failed to initialize Redis client: {e}")
        redis_client = None

# ⚙️ FastAPI app
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# assumes:
# redis_client = redis.from_url(REDIS_URL, decode_responses=True)
# and `log` + `session_memory` already exist

async def save_transcript(call_sid, user_transcript=None, audio_path=None, gpt_response=None):
    """
    Save transcript-related fields for a call into Redis (and optionally local cache).
    - call_sid: Redis key (one hash per call)
    - user_transcript: latest user text
    - audio_path: path to last audio file
    - gpt_response: last GPT text response
    """
    fields = {}

    if user_transcript:
        ts = time.time()
        fields["user_transcript"] = user_transcript
        fields["transcript_version"] = ts

        log(f"📝 save_transcript Saved user_transcript for {call_sid}: {repr(user_transcript)}")
    else:
        log(f"⚠️ save_transcript No user_transcript provided for {call_sid}")

    if gpt_response:
        fields["gpt_response"] = gpt_response

    if audio_path:
        fields["audio_path"] = audio_path

    # 🔴 Nothing to write, just return
    if not fields:
        return

    # ✅ Primary: write to Redis, with timing
    if redis_client is not None:
        try:
            start = time.perf_counter()
            # hset(key, mapping=dict) stores everything in one hash
            await redis_client.hset(call_sid, mapping=fields)
            elapsed_ms = (time.perf_counter() - start) * 1000.0

            log(
                f"⏱️ Redis hset for {call_sid} "
                f"took {elapsed_ms:.2f} ms (fields={list(fields.keys())})"
            )

            # Optional TTL:
            # await redis_client.expire(call_sid, 7200)

        except Exception as e:
            log(f"❌ Redis hset failed for {call_sid}: {e}")

    else:
        log("⚠️ save_transcript called but redis_client is None; only local cache updated")

    # 🟡 Optional: keep local cache during migration
    session = session_memory.setdefault(call_sid, {})
    for k, v in fields.items():
        session[k] = v

async def get_last_audio_for_call(call_sid: str):
    """
    Return the latest audio_path for this call from Redis.
    - Redis key: call_sid
    - Redis field: "audio_path"
    """
    if redis_client is None:
        logging.error(
            f"❌ get_last_audio_for_call: redis_client is None, "
            f"cannot load audio_path for {call_sid}"
        )
        return None

    try:
        start = time.perf_counter()
        audio_path = await redis_client.hget(call_sid, "audio_path")
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        log(f"⏱️ Redis hget audio_path for {call_sid} took {elapsed_ms:.2f} ms")

        if audio_path:
            log(f"🎧 Retrieved audio path for {call_sid} from Redis: {audio_path}")
            return audio_path

    except Exception as e:
        log(f"❌ Redis hget failed in get_last_audio_for_call for {call_sid}: {e}")

    logging.error(
        f"❌ No audio path found for {call_sid} in Redis."
    )
    return None

async def convert_audio_ulaw(call_sid: str, file_path: str, unique_id: str):
    converted_path = f"static/audio/response_{unique_id}_ulaw.wav"
    os.makedirs(os.path.dirname(converted_path), exist_ok=True)
    loop = asyncio.get_running_loop()

    # local per-call session cache (still useful even with Redis)
    session = session_memory.setdefault(call_sid, {})

    # 1) Run ffmpeg in a thread
    def _run_ffmpeg():
        return subprocess.run(
            [
                "/usr/bin/ffmpeg",
                "-y",
                "-i", file_path,
                "-ar", "8000",
                "-ac", "1",
                "-c:a", "pcm_mulaw",
                converted_path,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    try:
        start = time.time()
        result = await loop.run_in_executor(None, _run_ffmpeg)
        print(f"⏱️ FFmpeg subprocess.run() took {time.time() - start:.4f} seconds")
        print("🧭 Checking absolute path:", os.path.abspath(converted_path))
    except subprocess.CalledProcessError as e:
        print("❌ FFmpeg failed:")
        try:
            print(e.stderr.decode(errors="ignore"))
        except Exception:
            pass
        return None

    # 2) Small guard to be extra sure the file exists
    for i in range(40):
        if os.path.isfile(converted_path):
            print(f"✅ Found converted file after {i * 0.1:.1f}s")
            break
        await asyncio.sleep(0.1)
    else:
        print("❌ Converted file never appeared — aborting")
        return None

    # 3) Measure duration with ffprobe (also in a thread)
    try:
        def _probe_duration():
            return float(
                subprocess.check_output(
                    [
                        "ffprobe",
                        "-v", "error",
                        "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1",
                        converted_path,
                    ],
                    stderr=subprocess.STDOUT,
                )
            )

        duration = await loop.run_in_executor(None, _probe_duration)
        print(f"⏱️ Duration of audio file: {duration:.2f} seconds")

        # 🔹 keep in local session_memory (used by your AI-speaking timing logic)
        session["audio_duration"] = duration

        # 🔹 also persist to Redis for cross-instance visibility
        if redis_client is not None:
            try:
                start_redis = time.perf_counter()
                await redis_client.hset(call_sid, mapping={"audio_duration": duration})
                elapsed_ms = (time.perf_counter() - start_redis) * 1000.0
                log(f"⏱️ Redis hset audio_duration for {call_sid} took {elapsed_ms:.2f} ms")
            except Exception as e:
                log(f"❌ Redis hset audio_duration failed for {call_sid}: {e}")

    except subprocess.CalledProcessError as e:
        print("⚠️ Failed to measure audio duration with ffprobe:")
        try:
            print(e.output.decode(errors="ignore"))
        except Exception:
            pass
        duration = 0.0

    # 4) Save transcript + flags
    audio_bytes = session.get("audio_bytes")
    gpt_text = session.get("gpt_text")

    if not audio_bytes:
        print("❌ audio_bytes missing in session_memory")
        return None

    if len(audio_bytes) > 2000:
        # ✅ This will write audio_path + gpt_response to BOTH Redis and session_memory
        await save_transcript(call_sid, audio_path=converted_path, gpt_response=gpt_text)
    else:
        print("⚠️ Skipping transcript/audio save due to likely blank response.")

    session["ffmpeg_audio_ready"] = True
    print(f"🚩 Flag set: ffmpeg_audio_ready = True for session {call_sid}")

    return converted_path
    
async def get_11labs_audio(call_sid: str):
    """
    Use GPT output for this call_sid, generate ElevenLabs audio,
    and save the relevant state in BOTH session_memory and Redis.
    """
    # 🔹 Ensure local session exists
    session = session_memory.setdefault(call_sid, {})

    # 🔹 Reset GPT-related flags (local cache)
    session["gpt_response_ready"] = False
    session["get_gpt_response_started"] = False
    session["user_transcript"] = None

    # 🔹 Mirror those resets into Redis (optional but good for migration)
    if redis_client is not None:
        try:
            await redis_client.hset(
                call_sid,
                mapping={
                    "gpt_response_ready": "0",          # store as string/flag
                    "get_gpt_response_started": "0",
                    "user_transcript": "",
                },
            )
        except Exception as e:
            log(f"⚠️ Redis hset in get_11labs_audio failed for {call_sid}: {e}")

    # 🔹 Retrieve GPT output
    # 1) Prefer in-memory cache (where get_gpt_response currently writes)
    gpt_text = session.get("gpt_text")

    # 2) Fallback to Redis if not found in memory
    if not gpt_text and redis_client is not None:
        try:
            # Try both possible keys: "gpt_text" (future) and "gpt_response" (from save_transcript)
            redis_vals = await redis_client.hmget(call_sid, "gpt_text", "gpt_response")
            gpt_text = redis_vals[0] or redis_vals[1]
        except Exception as e:
            log(f"⚠️ Redis hmget in get_11labs_audio failed for {call_sid}: {e}")

    if not gpt_text:
        gpt_text = "[Missing GPT Output]"

    print(f"🧠 GPT returned text: {gpt_text}")

    loop = asyncio.get_running_loop()

    # ── 3. TEXT-TO-SPEECH WITH ELEVENLABS (off the event loop) ───────────────
    def _call_elevenlabs():
        return requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            headers={
                "xi-api-key": os.getenv("ELEVENLABS_API_KEY"),
                "Content-Type": "application/json"
            },
            json={
                "text": gpt_text,
                "model_id": "eleven_flash_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
            }
        )

    elevenlabs_response = await loop.run_in_executor(None, _call_elevenlabs)

    print("🧪 ElevenLabs status:", elevenlabs_response.status_code)
    print("🧪 ElevenLabs content type:", elevenlabs_response.headers.get("Content-Type"))
    print("🛰️ ElevenLabs Status Code:", elevenlabs_response.status_code)
    print("🛰️ ElevenLabs Content-Type:", elevenlabs_response.headers.get("Content-Type"))
    print("🛰️ ElevenLabs Response Length:", len(elevenlabs_response.content), "bytes")
    print("🛰️ ElevenLabs Content (first 500 bytes):", elevenlabs_response.content[:500])
    print(
        f"🎙️ ElevenLabs status {elevenlabs_response.status_code}, "
        f"bytes {len(elevenlabs_response.content)}"
    )

    audio_bytes = elevenlabs_response.content
    unique_id = uuid.uuid4().hex
    file_path = f"static/audio/response_{unique_id}.wav"

    # Save everything needed for later use in /4 (local cache)
    session["unique_id"]  = unique_id
    session["file_path"]  = file_path
    session["audio_bytes"] = audio_bytes
    session["gpt_text"]    = gpt_text  # keep for FFmpeg step etc.

    with open(file_path, "wb") as f:
        f.write(audio_bytes)
    print(f"💾 Saved original WAV → {file_path}")

    session["11labs_audio_ready"] = True
    print(f"🚩 Flag set: 11labs_audio_ready = True for session {call_sid}")

    # (Optional) mirror some of this into Redis too
    if redis_client is not None:
        try:
            await redis_client.hset(
                call_sid,
                mapping={
                    "unique_id": unique_id,
                    "file_path": file_path,
                    "gpt_text": gpt_text,
                    "11labs_audio_ready": "1",
                },
            )
        except Exception as e:
            log(f"⚠️ Redis hset (11labs metadata) failed for {call_sid}: {e}")

    await asyncio.sleep(1)

    # ✅ Failure check with print statements
    if not audio_bytes or elevenlabs_response.status_code != 200:
        print("❌ ElevenLabs failed or returned empty audio!")
        print("🔁 GPT Text:", gpt_text)
        print("🛑 Status:", elevenlabs_response.status_code)
        print("📜 Response:", elevenlabs_response.text)

# ✅ GPT handler function (Redis-only)
async def get_gpt_response(call_sid: str) -> None:
    if redis_client is None:
        log("⚠️ get_gpt_response called but redis_client is None; aborting")
        return

    try:
        # 1) Read user_transcript from Redis
        try:
            gpt_input = await redis_client.hget(call_sid, "user_transcript")
        except Exception as e:
            log(f"⚠️ Redis hget(user_transcript) failed in get_gpt_response for {call_sid}: {e}")
            gpt_input = None

        safe_text = gpt_input.strip() if gpt_input else "Hello, how can I help you today?"

        # 2) Call OpenAI
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful AI assistant named Lotus. "
                        "Keep your responses clear and concise."
                    ),
                },
                {"role": "user", "content": safe_text},
            ],
        )

        gpt_text = response.choices[0].message.content or "[GPT returned empty message]"

        # 3) Save to Redis (source of truth)
        fields = {
            "gpt_text": gpt_text,
            "gpt_response_ready": "1",   # store as string flag
        }

        try:
            await redis_client.hset(call_sid, mapping=fields)
            log(f"🚩 Redis: gpt_response_ready=1 for {call_sid}")
        except Exception as e:
            log(f"❌ Redis hset failed in get_gpt_response for {call_sid}: {e}")

    except Exception as e:
        # If OpenAI or anything above fails, write a fallback so /2 doesn't hang forever
        print(f"⚠️ GPT Error: {e}")

        fallback_text = "[GPT failed to respond]"
        fields = {
            "gpt_text": fallback_text,
            "gpt_response_ready": "1",
        }

        try:
            await redis_client.hset(call_sid, mapping=fields)
            log(f"🚩 Redis: wrote fallback GPT text for {call_sid}")
        except Exception as e2:
            log(f"❌ Redis hset failed in error path for {call_sid}: {e2}")

# ✅ Helper to run GPT in executor from a thread
async def print_gpt_response(sentence: str):
    response = await get_gpt_response(sentence)
    print(f"🤖 GPT: {response}")
    
    # Step 3: Save audio to file
    audio_bytes = audio_response.content
    
    # 👇 Make unique filename with UUID
    unique_id = str(uuid.uuid4())
    filename = f"response_{unique_id}.wav"
    file_path = f"static/audio/{filename}"
    converted_path = f"static/audio/response_{unique_id}_ulaw.wav"

    print(f"🔊 Audio file size: {len(audio_bytes)} bytes")
    print(f"💾 Saving audio to {file_path}")
    
    os.makedirs("static/audio", exist_ok=True)
    with open(file_path, "wb") as f:  # ✅ use dynamic path
        f.write(audio_bytes)
        print("✅ Audio file saved at:", file_path)
        print(f"🎧 Got {len(audio_bytes)} audio bytes from ElevenLabs")
        
    for _ in range(10):  # wait up to 5 seconds
        if os.path.exists(converted_path):
            print("✅ File exists for playback:", converted_path)
            break
        print("⌛ Waiting for file to become available...")
        time.sleep(0.5)
    else:
        print("❌ File still not found after 5 seconds!")
        
class VerboseStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        #Build full URL
        scheme   = scope.get("scheme", "http")
        host     = dict(scope["headers"]).get(b"host", b"-").decode()
        full_url = f"{scheme}://{host}{scope['path']}"

        abs_path = os.path.abspath(os.path.join(self.directory, path))
        exists   = os.path.exists(abs_path)
        readable = os.access(abs_path, os.R_OK)

        log(
            f"📂 Static GET {path!r} → exists={exists} "
            f"readable={readable} size={os.path.getsize(abs_path) if exists else '—'}"
        )

        if not exists:
            try:
                parent = os.path.dirname(abs_path)
                log("📑 Dir listing: %s", os.listdir(parent))
            except Exception as e:
                log("⚠️ Could not list directory: %s", e)

        return await super().get_response(path, scope)

@app.get("/")
async def health():
    return{"satus": "ok"}

@app.post("/")
async def twilio_voice_webhook(request: Request):
    print("\n📞 ── [POST] Twilio webhook hit ───────────────────────────────────")
    form_data = await request.form()
    call_sid = form_data.get("CallSid") or str(uuid.uuid4())
    print(f"🆔 Call SID: {call_sid}")

    # If Redis isn't available, we can't do proper stateless routing
    if redis_client is None:
        log("⚠️ twilio_voice_webhook: redis_client is None; using simple fallback")
        vr = VoiceResponse()
        vr.redirect("/greeting")
        return Response(str(vr), media_type="application/xml")

    def _to_bool(val):
        if val is None:
            return False
        return str(val).lower() in {"1", "true", "yes", "on"}

    # ─────────────────────────────────────────────
    # 1) Check if greeting was already played
    # ─────────────────────────────────────────────
    try:
        greeting_played_raw = await redis_client.hget(call_sid, "greeting_played")
        greeting_played = _to_bool(greeting_played_raw)
        print(f"👋 greeting_played_raw={greeting_played_raw}, interpreted={greeting_played}")
    except Exception as e:
        log(f"⚠️ Failed to read greeting_played from Redis for {call_sid}: {e}")
        greeting_played = False

    # First-time caller: mark greeting_played in Redis and redirect
    if not greeting_played:
        try:
            await redis_client.hset(call_sid, mapping={"greeting_played": "1"})
            log(f"✅ Set greeting_played=1 for {call_sid} in Redis")
        except Exception as e:
            log(f"⚠️ Failed to write greeting_played to Redis for {call_sid}: {e}")

        vr = VoiceResponse()
        vr.redirect("/greeting")
        print("👋 First-time caller — redirecting to greeting handler.")
        return Response(content=str(vr), media_type="application/xml")

    # ─────────────────────────────────────────────
    # 2) Fetch transcript-related fields from Redis
    # ─────────────────────────────────────────────
    user_transcript = None
    transcript_version = 0.0
    last_responded_version = 0.0

    try:
        user_transcript, tv_raw, lr_raw = await redis_client.hmget(
            call_sid,
            "user_transcript",
            "transcript_version",
            "last_responded_version",
        )

        transcript_version = float(tv_raw) if tv_raw is not None else 0.0
        last_responded_version = float(lr_raw) if lr_raw is not None else 0.0

        print(
            f"🧾 Redis transcript for {call_sid}: "
            f"user_transcript={repr(user_transcript)}, "
            f"transcript_version={transcript_version}, "
            f"last_responded_version={last_responded_version}"
        )
    except Exception as e:
        log(f"⚠️ Redis HMGET failed for {call_sid} in twilio_voice_webhook: {e}")
        # If we can't read state safely, just send them to /wait
        vr = VoiceResponse()
        vr.redirect("/wait")
        print("⚠️ Redis error reading transcript — redirecting to /wait")
        return Response(content=str(vr), media_type="application/xml")

    # ─────────────────────────────────────────────
    # 3) Decide if this transcript is new/usable
    # ─────────────────────────────────────────────
    if user_transcript and transcript_version > last_responded_version:
        gpt_input = user_transcript
        new_version = transcript_version

        # Mark this version as responded to in Redis
        try:
            await redis_client.hset(
                call_sid,
                mapping={"last_responded_version": new_version}
            )
            log(f"✅ Updated last_responded_version={new_version} for {call_sid} in Redis")
        except Exception as e:
            log(f"⚠️ Failed to write last_responded_version to Redis for {call_sid}: {e}")

        print(f"✅ Transcript ready v{new_version}: {gpt_input!r}")

    elif user_transcript:
        # Transcript exists but we've already responded to this version
        log(
            f"⛔ Skipped GPT input for {call_sid}: "
            f"user_transcript={repr(user_transcript)}, "
            f"version={transcript_version}, last_responded={last_responded_version}"
        )
        return Response(content="No new transcript yet", media_type="application/xml")

    else:
        # Truly no transcript yet — keep Twilio alive and wait
        vr = VoiceResponse()
        vr.redirect("/wait")
        print("⏳ No new transcript — redirecting to /wait")
        return Response(content=str(vr), media_type="application/xml")

    # ─────────────────────────────────────────────
    # 4) Log debug timestamp only in Redis
    # ─────────────────────────────────────────────
    now_ts = time.time()
    try:
        await redis_client.hset(
            call_sid,
            mapping={"debug_gpt_input_logged_at": now_ts}
        )
        log(
            f"🕒 debug_gpt_input_logged_at={now_ts} "
            f"written to Redis for {call_sid}"
        )
    except Exception as e:
        log(f"⚠️ Failed to write debug_gpt_input_logged_at to Redis for {call_sid}: {e}")

    # ─────────────────────────────────────────────
    # 5) Kick off next phase (/2) via TwiML redirect
    # ─────────────────────────────────────────────
    print(f"📝 GPT input candidate: {gpt_input!r}")
    vr = VoiceResponse()
    vr.redirect("/2")  # First redirect in your flow
    print("👋 Redirecting to /2")
    return Response(str(vr), media_type="application/xml")
    
@app.post("/2")
async def post2(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid")

    # --- 1️⃣ Load user_transcript from Redis (preferred), and mirror into session_memory ---
    gpt_input = None

    if redis_client is not None:
        try:
            gpt_input = await redis_client.hget(call_sid, "user_transcript")
        except Exception as e:
            log(f"⚠️ Redis hget(user_transcript) failed for {call_sid}: {e}")

    # Fallback to session_memory if Redis had nothing / error
    if gpt_input is None:
        gpt_input = session_memory.get(call_sid, {}).get("user_transcript")
    else:
        # Mirror Redis value back into session_memory for this call
        session = session_memory.setdefault(call_sid, {})
        session["user_transcript"] = gpt_input

    # ✅ If no transcript or unclear, just go back to WAIT2 loops
    if not gpt_input or len(gpt_input.strip()) < 4:
        vr = VoiceResponse()
        vr.redirect("/wait2")
        print("⚠️ No valid transcript — redirecting to /wait2")
        return Response(str(vr), media_type="application/xml")

    # --- 2️⃣ Check / set get_gpt_response_started flag (Redis + local) ---
    def _to_bool(val):
        if val is None:
            return False
        return str(val).lower() in {"1", "true", "yes"}

    gpt_started = False

    if redis_client is not None:
        try:
            started_raw = await redis_client.hget(call_sid, "get_gpt_response_started")
            gpt_started = _to_bool(started_raw)
        except Exception as e:
            log(f"⚠️ Redis hget(get_gpt_response_started) failed for {call_sid}: {e}")
            gpt_started = bool(
                session_memory.get(call_sid, {}).get("get_gpt_response_started")
            )
    else:
        gpt_started = bool(
            session_memory.get(call_sid, {}).get("get_gpt_response_started")
        )

    # ✅ If GPT isn’t started yet, start it **once**
    if not gpt_started:
        # local cache
        session = session_memory.setdefault(call_sid, {})
        session["get_gpt_response_started"] = True

        # Redis flag (store as "1" so _to_bool works consistently)
        if redis_client is not None:
            try:
                await redis_client.hset(
                    call_sid,
                    mapping={"get_gpt_response_started": "1"}
                )
            except Exception as e:
                log(f"⚠️ Failed to write get_gpt_response_started for {call_sid}: {e}")

        asyncio.create_task(get_gpt_response(call_sid))
        print("🚀 Started GPT task in background")

    vr = VoiceResponse()

    # --- 3️⃣ Check gpt_response_ready (Redis first, then local) ---
    gpt_ready = False

    if redis_client is not None:
        try:
            ready_raw = await redis_client.hget(call_sid, "gpt_response_ready")
            gpt_ready = _to_bool(ready_raw)
        except Exception as e:
            log(f"⚠️ Redis hget(gpt_response_ready) failed for {call_sid}: {e}")
            gpt_ready = bool(
                session_memory.get(call_sid, {}).get("gpt_response_ready")
            )
    else:
        gpt_ready = bool(
            session_memory.get(call_sid, {}).get("gpt_response_ready")
        )

    # ✅ If GPT finished, move to /3
    if gpt_ready:
        print("✅ GPT response is ready — redirecting to /3")
        vr.redirect("/3")
    else:
        print("⏳ GPT not ready — redirecting to /wait2")
        vr.redirect("/wait2")

    return Response(str(vr), media_type="application/xml")

@app.post("/3")
async def post3(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid")

    def _to_bool(val):
        if val is None:
            return False
        return str(val).lower() in {"1", "true", "yes"}

    # --- 1️⃣ Check if 11labs_audio_fetch_started (Redis first, then local) ---
    fetch_started = False

    if redis_client is not None:
        try:
            started_raw = await redis_client.hget(call_sid, "11labs_audio_fetch_started")
            fetch_started = _to_bool(started_raw)
        except Exception as e:
            log(f"⚠️ Redis hget(11labs_audio_fetch_started) failed for {call_sid}: {e}")
            fetch_started = bool(
                session_memory.get(call_sid, {}).get("11labs_audio_fetch_started")
            )
    else:
        fetch_started = bool(
            session_memory.get(call_sid, {}).get("11labs_audio_fetch_started")
        )

    # If not started yet, flip flag (Redis + local) and spawn background task
    if not fetch_started:
        session = session_memory.setdefault(call_sid, {})
        session["11labs_audio_fetch_started"] = True

        if redis_client is not None:
            try:
                await redis_client.hset(
                    call_sid,
                    mapping={"11labs_audio_fetch_started": True}
                )
            except Exception as e:
                log(f"⚠️ Failed to write 11labs_audio_fetch_started for {call_sid}: {e}")

        asyncio.create_task(get_11labs_audio(call_sid))
        print("🚀 Started 11Labs task in background")

    vr = VoiceResponse()

    # --- 2️⃣ Check if 11labs_audio_ready (Redis first, then local) ---
    audio_ready = False

    if redis_client is not None:
        try:
            ready_raw = await redis_client.hget(call_sid, "11labs_audio_ready")
            audio_ready = _to_bool(ready_raw)
        except Exception as e:
            log(f"⚠️ Redis hget(11labs_audio_ready) failed for {call_sid}: {e}")
            audio_ready = bool(
                session_memory.get(call_sid, {}).get("11labs_audio_ready")
            )
    else:
        audio_ready = bool(
            session_memory.get(call_sid, {}).get("11labs_audio_ready")
        )

    if audio_ready:
        print("✅ 11 Labs audio is ready — redirecting to /4")
        vr.redirect("/4")
        return Response(str(vr), media_type="application/xml")
    else:
        vr.redirect("/wait3")
        print("👋 Redirecting to /wait3")
        return Response(str(vr), media_type="application/xml")

@app.post("/4")
async def post4(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid")

    def _to_bool(val):
        if isinstance(val, bool):
            return val
        if val is None:
            return False
        return str(val).lower() in {"1", "true", "yes", "on"}

    # 🔒 1) Load session from Redis (then merge local session_memory)
    session: dict = {}

    if redis_client is not None:
        try:
            redis_data = await redis_client.hgetall(call_sid)
            if redis_data:
                session.update(redis_data)
        except Exception as e:
            log(f"⚠️ Redis hgetall failed for {call_sid} in /4: {e}")

    # Merge in local (this can contain non-Redis stuff like audio_bytes)
    local_session = session_memory.get(call_sid, {})
    if local_session:
        session.update(local_session)

    # If still nothing, we really don't have state for this CallSid
    if not session:
        print(f"❌ /4 hit but no session found for {call_sid} (Redis + local empty)")
        vr = VoiceResponse()
        vr.say("Sorry, something went wrong. Let me reset.")
        vr.redirect("/")   # or vr.redirect("/3")
        return Response(str(vr), media_type="application/xml")

    # From here on, `session` is our merged view
    unique_id = session.get("unique_id")
    file_path = session.get("file_path")

    # 🔒 2) If ElevenLabs step never populated these, handle gracefully
    if not unique_id or not file_path:
        print(f"❌ Missing unique_id or file_path for {call_sid}")
        vr = VoiceResponse()
        vr.redirect("/3")   # try to re-trigger 11Labs
        return Response(str(vr), media_type="application/xml")

    # 🔒 3) Kick off FFmpeg only once
    ffmpeg_started = _to_bool(session.get("ffmpeg_audio_fetch_started"))
    if not ffmpeg_started:
        # Update local cache
        local = session_memory.setdefault(call_sid, {})
        local["ffmpeg_audio_fetch_started"] = True
        local["11labs_audio_fetch_started"] = False
        local["11labs_audio_ready"] = False

        # Persist flags to Redis
        if redis_client is not None:
            try:
                await redis_client.hset(
                    call_sid,
                    mapping={
                        "ffmpeg_audio_fetch_started": True,
                        "11labs_audio_fetch_started": False,
                        "11labs_audio_ready": False,
                    },
                )
            except Exception as e:
                log(f"⚠️ Failed to write FFmpeg/11Labs flags for {call_sid}: {e}")

        # Start FFmpeg conversion in background
        asyncio.create_task(convert_audio_ulaw(call_sid, file_path, unique_id))
        print("🚀 Started FFmpeg task in background")
        print(f"🚩 Flag set: 11labs_audio_fetch_started = False for {call_sid}")
        print(f"🚩 Flag set: 11labs_audio_ready = False for {call_sid}")

    vr = VoiceResponse()

    # 🔒 4) Only redirect to /5 once FFmpeg says the audio is ready
    ffmpeg_ready = False

    # Prefer Redis for readiness flag
    if redis_client is not None:
        try:
            ready_raw = await redis_client.hget(call_sid, "ffmpeg_audio_ready")
            ffmpeg_ready = _to_bool(ready_raw)
        except Exception as e:
            log(f"⚠️ Redis hget(ffmpeg_audio_ready) failed for {call_sid}: {e}")

    # Also OR with local in-memory flag (so existing convert_audio_ulaw logic still works)
    ffmpeg_ready = ffmpeg_ready or bool(
        session_memory.get(call_sid, {}).get("ffmpeg_audio_ready")
    )

    if ffmpeg_ready:
        print("✅ FFmpeg audio is ready — redirecting to /5")
        vr.redirect("/5")
    else:
        print("👋 Redirecting to /wait4")
        vr.redirect("/wait4")

    return Response(str(vr), media_type="application/xml")

@app.post("/5")
async def post5(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid")

    # 🔐 Local session view (for non-Redis stuff like audio_bytes if needed)
    session = session_memory.setdefault(call_sid, {})

    # 🔁 Reset FFmpeg flags locally
    session["ffmpeg_audio_ready"] = False
    print(f"🚩 Flag set: ffmpeg_audio_ready = False for session {call_sid}")
    
    session["ffmpeg_audio_fetch_started"] = False
    print(f"🚩 Flag set: ffmpeg_audio_fetch_started = False for session {call_sid}")

    # 🔁 Also reset FFmpeg flags in Redis
    if redis_client is not None:
        try:
            await redis_client.hset(
                call_sid,
                mapping={
                    "ffmpeg_audio_ready": False,
                    "ffmpeg_audio_fetch_started": False,
                },
            )
        except Exception as e:
            log(f"⚠️ Redis hset failed when resetting FFmpeg flags for {call_sid}: {e}")

    # ── 5. BUILD TWIML ─────────────────────────────────────────────────────────
    vr = VoiceResponse()

    # Start Deepgram stream
    start = Start()
    start.stream(
        url="wss://silent-sound-1030.fly.dev/media",
        content_type="audio/x-mulaw;rate=8000"
    )
    vr.append(start)

    log("📡 Starting Deepgram stream to WebSocket endpoint")

    # Try to retrieve the most recent converted file with retries
    audio_path = None
    for _ in range(10):
        current_path = await get_last_audio_for_call(call_sid)  # ← now Redis-aware
        log(f"🔁 Checking session store for {call_sid} → {current_path}")
        if current_path and os.path.exists(current_path):
            audio_path = current_path
            break
        await asyncio.sleep(1)

    if audio_path:
        ulaw_filename = os.path.basename(audio_path)

        block_start_time = time.time()
        session["block_start_time"] = block_start_time
        print(f"✅ Set block_start_time: {block_start_time}")

        # Set ai_is_speaking flag to True right before the file is played in POST
        session["ai_is_speaking"] = True
        print(
            f"🚩 Flag set: ai_is_speaking = {session['ai_is_speaking']} "
            f"for session {call_sid} at {time.time()}"
        )

        logger.info(
            f"🟥 [User Input] Processing complete — unblocking writes for {call_sid}"
        )
        session["user_response_processing"] = False

        # 🔁 Persist these flags & timestamp to Redis as well
        if redis_client is not None:
            try:
                await redis_client.hset(
                    call_sid,
                    mapping={
                        "block_start_time": block_start_time,
                        "ai_is_speaking": True,
                        "user_response_processing": False,
                    },
                )
            except Exception as e:
                log(f"⚠️ Redis hset failed when setting flags in /5 for {call_sid}: {e}")

        vr.play(f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}")
        print(
            "🔗 Final playback URL:",
            f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}",
        )
        print(f"✅ Queued audio for playback: {ulaw_filename}")
    else:
        print("❌ Audio not found after retry loop")
        vr.say("Sorry, something went wrong.")
        
    # ✅ Replace hangup with redirect back to self
    vr.redirect("/")
    print("📝 Returning TwiML to Twilio (with redirect).")
    return Response(content=str(vr), media_type="application/xml")
    
@app.post("/greeting")
async def greeting_rout(request: Request):
    print("\n📞 ── [POST] Greeting handler hit ───────────────────────────────────")
    form_data = await request.form()
    call_sid = form_data.get("CallSid") or str(uuid.uuid4())
    print(f"🆔 Call SID: {call_sid}")

    # 🔍 Debug: show Redis session instead of raw session_memory keys
    if redis_client is not None:
        try:
            redis_session = await redis_client.hgetall(call_sid)
            print(f"🧠 Redis session for {call_sid}: {redis_session}")
        except Exception as e:
            print(f"⚠️ Failed to read Redis session for {call_sid}: {e}")
    else:
        # Fallback debug while migrating
        print(f"🧠 Current session_memory keys: {list(session_memory.keys())}")

    # ── 2. 1 TIME GREETING ───────────────────────────────────────
    gpt_text = "Hello my name is Lotus, how can I help you today?"
    print(f"✅ GPT greeting: \"{gpt_text}\"")

    # ── 3. TEXT-TO-SPEECH WITH ELEVENLABS ──────────────────────────────────────
    elevenlabs_response = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
        headers={
            "xi-api-key": os.getenv("ELEVENLABS_API_KEY"),
            "Content-Type": "application/json"
        },
        json={
            "text": gpt_text,
            "model_id": "eleven_flash_v2_5",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
        }
    )

    print("🧪 ElevenLabs status:", elevenlabs_response.status_code)
    print("🧪 ElevenLabs content type:", elevenlabs_response.headers.get("Content-Type"))
    print("🛰️ ElevenLabs Status Code:", elevenlabs_response.status_code)
    print("🛰️ ElevenLabs Content-Type:", elevenlabs_response.headers.get("Content-Type"))
    print("🛰️ ElevenLabs Response Length:", len(elevenlabs_response.content), "bytes")
    print("🛰️ ElevenLabs Content (first 500 bytes):", elevenlabs_response.content[:500])
    print(
        f"🎙️ ElevenLabs status {elevenlabs_response.status_code}, "
        f"bytes {len(elevenlabs_response.content)}"
    )

    audio_bytes = elevenlabs_response.content
    unique_id = uuid.uuid4().hex
    file_path = f"static/audio/response_{unique_id}.wav"

    with open(file_path, "wb") as f:
        f.write(audio_bytes)
    print(f"💾 Saved original WAV → {file_path}")

    await asyncio.sleep(1)

    # ✅ Failure check with print statements
    if not audio_bytes or elevenlabs_response.status_code != 200:
        print("❌ ElevenLabs failed or returned empty audio!")
        print("🔁 GPT Text:", gpt_text)
        print("🛑 Status:", elevenlabs_response.status_code)
        print("📜 Response:", elevenlabs_response.text)
        return

    # ── 4. CONVERT TO μ-LAW 8 kHz ──────────────────────────────────────────────
    converted_path = f"static/audio/response_{unique_id}_ulaw.wav"
    try:
        subprocess.run([
            "/usr/bin/ffmpeg", "-y", "-i", file_path,
            "-ar", "8000", "-ac", "1", "-c:a", "pcm_mulaw", converted_path
        ], check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ FFmpeg failed: {e}")
        return Response("Audio conversion failed", status_code=500)

    print("🧭 Checking absolute path:", os.path.abspath(converted_path))

    # ✅ Wait for file to become available (race condition guard)
    for i in range(40):
        if os.path.isfile(converted_path):
            print(f"✅ Found converted file after {i * 0.1:.1f}s")
            break
        await asyncio.sleep(0.1)
    else:
        print("❌ Converted file never appeared — aborting")
        return Response("Converted audio not available", status_code=500)

    print(f"🎛️ Converted WAV (8 kHz μ-law) → {converted_path}")
    log("✅ Audio file saved at %s", converted_path)

    # ⏱️ Measure duration using ffprobe
    try:
        duration = float(subprocess.check_output([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            converted_path
        ]))
        print(f"⏱️ Duration of audio file: {duration:.2f} seconds")

        # 🔒 Store for later — keep both Redis + local cache during migration
        session = session_memory.setdefault(call_sid, {})
        session["audio_duration"] = duration

        if redis_client is not None:
            try:
                await redis_client.hset(call_sid, mapping={"audio_duration": duration})
            except Exception as e:
                log(f"⚠️ Failed to write audio_duration to Redis for {call_sid}: {e}")
    except Exception as e:
        print(f"⚠️ Failed to measure audio duration: {e}")
        duration = 0.0

    # ✅ Only save if audio is a reasonable size (avoid silent/broken audio)
    if len(audio_bytes) > 2000:
        await save_transcript(call_sid, audio_path=converted_path, gpt_response=gpt_text)
        # Note: save_transcript already writes to Redis + session_memory
        print(f"🧠 Session updated AFTER save (local cache): {session_memory.get(call_sid)}")
    else:
        print("⚠️ Skipping transcript/audio save due to likely blank response.")

    # ── 5. BUILD TWIML ─────────────────────────────────────────────────────────
    vr = VoiceResponse()

    # Start Deepgram stream
    start = Start()
    start.stream(
        url="wss://silent-sound-1030.fly.dev/media",
        content_type="audio/x-mulaw;rate=8000"
    )
    vr.append(start)

    log("📡 Starting Deepgram stream to WebSocket endpoint")

    # Try to retrieve the most recent converted file with retries
    audio_path = None
    for _ in range(10):
        current_path = await get_last_audio_for_call(call_sid)  # ← already Redis-backed
        log(f"🔁 Checking Redis/session for {call_sid} → {current_path}")
        if current_path and os.path.exists(current_path):
            audio_path = current_path
            break
        await asyncio.sleep(1)

    if audio_path:
        ulaw_filename = os.path.basename(audio_path)

        block_start_time = time.time()
        # Local cache
        session = session_memory.setdefault(call_sid, {})
        session["block_start_time"] = block_start_time
        session["ai_is_speaking"] = True
        session["user_response_processing"] = False

        print(f"✅ Set block_start_time: {block_start_time}")
        print(
            f"🚩 Flag set: ai_is_speaking = {session['ai_is_speaking']} "
            f"for session {call_sid} at {time.time()}"
        )

        logger.info(f"🟥 [User Input] Processing complete — unblocking writes for {call_sid}")

        # 🔁 Mirror flags into Redis
        if redis_client is not None:
            try:
                await redis_client.hset(
                    call_sid,
                    mapping={
                        "block_start_time": block_start_time,
                        "ai_is_speaking": True,
                        "user_response_processing": False,
                    }
                )
            except Exception as e:
                log(f"⚠️ Failed to write greeting flags to Redis for {call_sid}: {e}")

        vr.play(f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}")
        print("🔗 Final playback URL:",
              f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}")
        print(f"✅ Queued audio for playback: {ulaw_filename}")
    else:
        print("❌ Audio not found after retry loop")
        vr.say("Sorry, something went wrong.")

    # ✅ Replace hangup with redirect back to self
    vr.redirect("/")
    print("📝 Returning TwiML to Twilio (with redirect).")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/wait")
async def wait_route(request: Request):
    print("\n📞 ── [POST] WAIT handler hit ───────────────────────────────────")
    form_data = await request.form()
    call_sid = form_data.get("CallSid") or str(uuid.uuid4())
    print(f"🆔 Call SID: {call_sid}")

    # 🔁 Debug: show this call's Redis session instead of session_memory
    if redis_client is not None:
        try:
            redis_session = await redis_client.hgetall(call_sid)
            print(f"🧠 Redis session for {call_sid}: {redis_session}")
        except Exception as e:
            print(f"⚠️ Failed to read Redis session for {call_sid}: {e}")
    else:
        print("⚠️ Redis disabled; cannot dump Redis session state.")

    vr = VoiceResponse()
    vr.pause(length=1)
    print("✅ Heartbeat sent: <Pause length='1'/>")
    await asyncio.sleep(1)

    vr.redirect("/")
    print("📝 Returning TwiML to Twilio (with redirect to /).")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/wait2")
async def wait2_route(request: Request):
    print("\n📞 ── [POST] WAIT2 handler hit ───────────────────────────────────")
    form_data = await request.form()
    call_sid = form_data.get("CallSid") or str(uuid.uuid4())
    print(f"🆔 Call SID: {call_sid}")

    if redis_client is not None:
        try:
            redis_session = await redis_client.hgetall(call_sid)
            print(f"🧠 Redis session for {call_sid}: {redis_session}")
        except Exception as e:
            print(f"⚠️ Failed to read Redis session for {call_sid}: {e}")
    else:
        print("⚠️ Redis disabled; cannot dump Redis session state.")

    vr = VoiceResponse()
    vr.pause(length=1)
    print("✅ Heartbeat sent: <Pause length='1'/>")
    await asyncio.sleep(1)

    vr.redirect("/2")
    print("📝 Returning TwiML to Twilio (with redirect to /2).")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/wait3")
async def wait3_route(request: Request):
    print("\n📞 ── [POST] WAIT3 handler hit ───────────────────────────────────")
    form_data = await request.form()
    call_sid = form_data.get("CallSid") or str(uuid.uuid4())
    print(f"🆔 Call SID: {call_sid}")

    if redis_client is not None:
        try:
            redis_session = await redis_client.hgetall(call_sid)
            print(f"🧠 Redis session for {call_sid}: {redis_session}")
        except Exception as e:
            print(f"⚠️ Failed to read Redis session for {call_sid}: {e}")
    else:
        print("⚠️ Redis disabled; cannot dump Redis session state.")

    vr = VoiceResponse()
    vr.pause(length=1)
    print("✅ Heartbeat sent: <Pause length='1'/>")
    await asyncio.sleep(1)

    vr.redirect("/3")
    print("📝 Returning TwiML to Twilio (with redirect to /3).")
    return Response(content=str(vr), media_type="application/xml")
    
@app.post("/wait4")
async def wait4_route(request: Request):
    print("\n📞 ── [POST] WAIT4 handler hit ───────────────────────────────────")
    form_data = await request.form()
    call_sid = form_data.get("CallSid") or str(uuid.uuid4())
    print(f"🆔 Call SID: {call_sid}")

    if redis_client is not None:
        try:
            redis_session = await redis_client.hgetall(call_sid)
            print(f"🧠 Redis session for {call_sid}: {redis_session}")
        except Exception as e:
            print(f"⚠️ Failed to read Redis session for {call_sid}: {e}")
    else:
        print("⚠️ Redis disabled; cannot dump Redis session state.")

    vr = VoiceResponse()
    vr.pause(length=1)
    print("✅ Heartbeat sent: <Pause length='1'/>")
    await asyncio.sleep(1)

    vr.redirect("/4")
    print("📝 Returning TwiML to Twilio (with redirect to /4).")
    return Response(content=str(vr), media_type="application/xml")

@app.get("/test_redis")
async def test_redis():
    if redis_client is None:
        log("❌ Redis test failed: redis_client is None")
        return {"status": "error", "detail": "redis_client is None"}

    try:
        pong = await redis_client.ping()
        log(f"📡 Redis PING response: {pong}")

        await redis_client.set("lotus:test", "hello from lotus")
        log("📝 Redis SET: lotus:test='hello from lotus'")

        val = await redis_client.get("lotus:test")
        log(f"📖 Redis GET: lotus:test='{val}'")

        return {"status": "ok", "ping": pong, "value": val}
    except Exception as e:
        log(f"❌ Redis test failed: {e}")
        return {"status": "error", "detail": str(e)}

@app.websocket("/media")
async def media_stream(ws: WebSocket):
    await ws.accept()
    print("★ Twilio WebSocket connected")

    # ~4 seconds of 8kHz μ-law audio (8000 bytes/sec)
    MAX_BUFFER_BYTES = 32000

    ws_state = {"closed": False}
    call_sid_holder = {"sid": None}
    last_input_time = {"ts": time.time()}
    last_transcript = {"text": "", "confidence": 0.5, "is_final": False}
    finished = {"done": False}

    state = {
        "is_final": False,
        "sentence": "",
        "confidence": 0.0,
        "last_is_final_time": None,  # 👈 add this
    }

    final_transcripts = []
    
    loop = asyncio.get_running_loop()
    deepgram = DeepgramClient(DEEPGRAM_API_KEY)
    dg_connection = None
    
    try:
        print("⚙️ Connecting to Deepgram live transcription...")

        try:
            live_client = deepgram.listen.live

            deepgram_options = {
                "punctuate": True,
                "interim_results": True,
                "endpointing": 2000  # 🟢 Wait 2000ms of silence before finalizing
                }
            
            dg_connection = await asyncio.to_thread(live_client.v, "1")
            print(f"✅ [DG CONNECT] low-level Deepgram WS established")
        except Exception as e:
            print(f"⛔ Failed to create Deepgram connection: {e}")
            await ws.close()
            return

        async def deepgram_close_watchdog():
            while True:
                await asyncio.sleep(0.02)
                sid = call_sid_holder.get("sid")
                if not sid:
                    continue

                # 🔍 Original logic: check local session_memory
                session = session_memory.setdefault(sid, {})
                if not session.get("close_requested"):
                    continue

                print(f"🛑 Closing Deepgram for {sid}")
                try:
                    dg_connection.finish()
                except Exception as e:
                    print(f"⚠️ Error closing Deepgram for {sid}: {e}")

                if ws_state["closed"]:
                    print(f"ℹ️ deepgram_close_watchdog: ws already closed for {sid}, skipping ws.close()")

                    # Optional: still mirror clean_websocket_close to Redis
                    if redis_client is not None:
                        try:
                            await redis_client.hset(sid, mapping={"clean_websocket_close": "1"})
                            log(f"🧼 [Redis] clean_websocket_close=True for {sid} (ws already closed)")
                        except Exception as e:
                            log(f"⚠️ Redis hset failed for clean_websocket_close on {sid}: {e}")
                    return

                # ✅ Mark closed locally so other tasks see it
                session["clean_websocket_close"] = True
                ws_state["closed"] = True
                print(f"🧼 clean_websocket_close = True for {sid} deepgram_close_watchdog")

                # ✅ Mirror to Redis (non-fatal if this fails)
                if redis_client is not None:
                    try:
                        await redis_client.hset(sid, mapping={"clean_websocket_close": "1"})
                        log(f"🧼 [Redis] clean_websocket_close=True for {sid}")
                    except Exception as e:
                        log(f"⚠️ Redis hset failed for clean_websocket_close on {sid}: {e}")

                try:
                    print(f"🔻 deepgram_close_watchdog: calling ws.close() for {sid}")
                    await ws.close()
                except Exception as e:
                    print(f"⚠️ Error closing WebSocket in deepgram_close_watchdog: {e}")

                return
        
                async def deepgram_is_final_watchdog():
                    while True:
                        await asyncio.sleep(0.02)

                        sid = call_sid_holder.get("sid")
                        if not sid:
                            continue

                        # ✅ Ensure local session exists
                        session = session_memory.setdefault(sid, {})

                        # ✅ Initialize warned once per session
                        if "warned" not in session:
                            session["warned"] = False

                        last_time = session.get("last_is_final_time")
                        if not last_time:
                            continue  # no is_final seen yet

                        elapsed = time.time() - last_time

                        if (
                            elapsed > 2.5
                            and not session["warned"]
                            and session.get("close_requested") is False
                            and session.get("ai_is_speaking") is False
                            and session.get("user_response_processing") is False
                        ):
                            print(f"⚠️ No is_final received in {elapsed:.2f}s for {sid}")
                            session["warned"] = True
                            print(f"🚩 Flag set: warned = True for session {sid}")

                            session["zombie_detected"] = True
                            print(f"🧟 Detected Deepgram zombie stream for {sid}, reconnecting...")

                            # 🔄 Mirror flags to Redis (best-effort, non-fatal)
                            if redis_client is not None:
                                try:
                                    start = time.time()
                                    await redis_client.hset(
                                        sid,
                                        mapping={
                                            "warned": "1",
                                            "zombie_detected": "1",
                                            "last_is_final_time": str(last_time),
                                        },
                                    )
                                    log(
                                        f"⏱️ Redis hset (zombie flags) for {sid} "
                                        f"took {(time.time() - start) * 1000:.2f} ms"
                                    )
                                except Exception as e:
                                    log(f"⚠️ Redis hset failed for zombie flags on {sid}: {e}")

                # make sure this is still inside the same `try:` as above
                loop.create_task(deepgram_is_final_watchdog())

        async def deepgram_error_reconnection():
            nonlocal dg_connection  # so we can replace the shared connection

            while True:
                await asyncio.sleep(1)  # check every second

                sid = call_sid_holder.get("sid")
                if not sid:
                    continue

                # -----------------------------
                # 🔍 Check zombie flag via Redis only
                # -----------------------------
                zombie_detected = False

                if redis_client is not None:
                    try:
                        zflag = await redis_client.hget(sid, "zombie_detected")
                        if zflag is not None:
                            zombie_detected = str(zflag).lower() in ("1", "true", "yes")
                    except Exception as e:
                        log(f"⚠️ Redis hget failed for zombie_detected on {sid}: {e}")
                        # If Redis is broken, we just skip this iteration
                        continue
                else:
                    # No Redis available -> this reconnection logic does nothing
                    continue

                if not zombie_detected:
                    # Redis says: "no zombie" -> nothing to do
                    continue

                print(f"💀 Zombie detected for sid={sid} — reconnecting Deepgram")

                # ---------------------------------------
                # 🧼 Clear flags in Redis (source of truth)
                # ---------------------------------------
                if redis_client is not None:
                    try:
                        await redis_client.hset(
                            sid,
                            mapping={
                                "zombie_detected": "0",
                                "warned": "0",
                                "last_is_final_time": "",
                            },
                        )
                        log(f"🧼 [Redis] Cleared zombie flags for {sid}")
                    except Exception as e:
                        log(f"⚠️ Redis hset failed clearing zombie flags for {sid}: {e}")
                        # Even if we fail to clear, still attempt reconnect once
                # ---------------------------------------
                # 🔌 Close old connection and reconnect
                # ---------------------------------------
                try:
                    # Close old connection if present
                    try:
                        if dg_connection is not None:
                            print("🔌 Finishing old Deepgram connection before reconnect…")
                            dg_connection.finish()
                    except Exception as e:
                        print(f"⚠️ Error finishing old Deepgram connection: {e}")

                    # Create a new connection just like at startup
                    live_client = deepgram.listen.live
                    new_conn = await asyncio.to_thread(live_client.v, "1")

                    # Reattach handlers
                    new_conn.on(LiveTranscriptionEvents.Transcript, on_transcript)
                    new_conn.on(
                        LiveTranscriptionEvents.Error,
                        lambda err: print(f"🔴 Deepgram error (reconnected): {err}"),
                    )
                    new_conn.on(
                        LiveTranscriptionEvents.Close,
                        lambda: print("🔴 Deepgram WebSocket closed (reconnected)"),
                    )

                    # Start streaming with the same options you used originally
                    new_conn.start(deepgram_options)

                    # Swap connection reference so keepalives use the new one
                    dg_connection = new_conn

                    print("🔁 Deepgram reconnected successfully")

                    # NOTE: We are NOT using session or audio_buffer here anymore.
                    # If you still want buffered audio flush, that has to live
                    # somewhere other than session_memory, or you accept local use.

                except Exception as e:
                    print(f"❌ Failed to reconnect Deepgram: {e}")

        # schedule the task
        loop.create_task(deepgram_error_reconnection())
        
        def on_transcript(*args, **kwargs):
            try:
                print("📥 RAW transcript event:")
                result = kwargs.get("result") or (args[0] if args else None)
                metadata = kwargs.get("metadata")

                if result is None:
                    print("⚠️ No result received.")
                    return

                print("📂 Type of result:", type(result))

                if hasattr(result, "to_dict"):
                    payload = result.to_dict()
                    print(json.dumps(payload, indent=2))

                    sid = call_sid_holder.get("sid")
                    now = time.time()
                    speech_final = payload.get("speech_final", False)

                    try:
                        alt = payload["channel"]["alternatives"][0]
                        sentence = alt.get("transcript", "")
                        confidence = alt.get("confidence", 0.0)
                        is_final = payload["is_final"] if "is_final" in payload else False

                        state["is_final"] = is_final
                        state["sentence"] = sentence
                        state["confidence"] = confidence
                        
                        if is_final and sentence.strip() and confidence >= 0.6:
                            print(f"✅ Final transcript received: \"{sentence}\" (confidence: {confidence})")
                            session_memory[sid]["last_is_final_time"] = time.time()

                            last_input_time["ts"] = time.time()
                            last_transcript["text"] = sentence
                            last_transcript["confidence"] = confidence
                            last_transcript["is_final"] = True

                            final_transcripts.append(sentence)

                            if speech_final:
                                sid = call_sid_holder.get("sid")
                                session_memory.setdefault(sid, {})
                                print("🧠 speech_final received — concatenating full transcript")
                                full_transcript = " ".join(final_transcripts)
                                log(f"🧪 [DEBUG] full_transcript after join: {repr(full_transcript)}")

                                # STOP immediately if we already processed a final transcript this turn
                                if session_memory[sid].get("user_response_processing"):
                                    print(f"🚫 Ignoring transcript — already processing user response for {sid}")
                                    return

                                if not full_transcript:
                                    log(f"⚠️ Skipping save — full_transcript is empty")
                                    return

                                if call_sid_holder["sid"]:
                                    sid = call_sid_holder["sid"]
                                    session_memory.setdefault(sid, {})

                                    # ... overwrite detection, etc. ...

                                    # flip ai_is_speaking off if block_start_time + audio_duration passed
                                    block_start_time = session_memory.get(sid, {}).get("block_start_time")
                                    print(f"🧠 Retrieved block_start_time: {block_start_time}")
                                    if (
                                        block_start_time is not None
                                        and session_memory[sid].get("audio_duration") is not None
                                        and time.time() > block_start_time + session_memory[sid]["audio_duration"]
                                    ):
                                        session_memory[sid]["ai_is_speaking"] = False
                                        log(f"🏁 [{sid}] AI finished speaking. Flag flipped OFF.")

                                    # ✅ Main save gate
                                    if (
                                        session_memory[sid].get("ai_is_speaking") is False and
                                        session_memory[sid].get("user_response_processing") is False
                                    ):
                                        # 🔴 ONLY HERE: we actually want to close Deepgram/Twilio
                                        session_memory[sid]["close_requested"] = True
                                        print(f"🛑 Requested Deepgram close for {sid} (accepted transcript)")

                                        # ✅ Proceed with save
                                        session_memory[sid]["user_transcript"] = full_transcript
                                        session_memory[sid]["ready"] = True
                                        session_memory[sid]["transcript_version"] = time.time()

                                        log(f"✍️ [{sid}] user_transcript saved at {time.time()}")
                                        loop.create_task(
                                            save_transcript(sid, user_transcript=full_transcript)
                                        )

                                        logger.info(f"🟩 [User Input] Processing started — blocking writes for {sid}")
                                        session_memory[sid]["user_response_processing"] = True

                                        # ✅ Clear after successful save
                                        final_transcripts.clear()
                                        last_transcript["text"] = ""
                                        last_transcript["confidence"] = 0.0
                                        last_transcript["is_final"] = False
                                    else:
                                        log(f"🚫 [{sid}] Save skipped — AI still speaking or still processing previous turn")

                                        # 🧹 Clear junk to avoid stale input
                                        final_transcripts.clear()
                                        last_transcript["text"] = ""
                                        last_transcript["confidence"] = 0.0
                                        last_transcript["is_final"] = False
                                        
                        elif is_final:
                            print(f"⚠️ Final transcript was too unclear: \"{sentence}\" (confidence: {confidence})")

                    except KeyError as e:
                        print(f"⚠️ Missing expected key in payload: {e}")
                    except Exception as inner_e:
                        print(f"⚠️ Could not extract transcript sentence: {inner_e}")
            except Exception as e:  # ← This closes the OUTER try
                print(f"⚠️ Error handling transcript: {e}")
                
        dg_connection.on(LiveTranscriptionEvents.Transcript, on_transcript)
        dg_connection.on(LiveTranscriptionEvents.Error, lambda err: print(f"🔴 Deepgram error: {err}"))
        dg_connection.on(
            LiveTranscriptionEvents.Close,
            lambda *args, **kwargs: print("🔴 Deepgram WebSocket closed")
        )

        options = LiveOptions(
            model="nova-3",
            language="en-US",
            encoding="mulaw",
            sample_rate=8000,
            punctuate=True,
        )
        print("✏️ LiveOptions being sent:", options.__dict__)
        dg_connection.start(options)
        print("✅ Deepgram connection started")

        # -------------------------------------------------
        # 🟢 REAL Keep-Alive Loop — send SILENT MULAW audio
        # -------------------------------------------------
        SILENCE_FRAME = b"\xff" * 160  # correct mulaw silence (20ms @ 8kHz)
        dg_connection.last_media_time = time.time()  # initialize timestamp

        async def deepgram_keepalive():
            counter = 0
            while True:
                await asyncio.sleep(0.02)  # run every 20ms

                sid = call_sid_holder.get("sid")

                # 🔍 Debug: is keepalive still running?
                # (throttled so you don't print 50x/sec)
                if counter % 50 == 0:  # ~once per second
                    print(f"📡 keepalive still running for sid={sid}, "
                          f"clean_websocket_close={session_memory.get(sid, {}).get('clean_websocket_close') if sid else None}")
                counter += 1

                if sid and session_memory.get(sid, {}).get("clean_websocket_close"):
                    print(f"🧼 Stopping deepgram_keepalive for {sid} (clean_websocket_close=True)")
                    break

                try:
                    # If Twilio has been silent for 50ms → send silence
                    if time.time() - dg_connection.last_media_time > 0.05:
                        dg_connection.send(SILENCE_FRAME)
                except Exception as e:
                    print(f"⚠️ KeepAlive error sending silence: {e}")
                    break

        loop.create_task(deepgram_keepalive())

        async def deepgram_text_keepalive():
            while True:
                await asyncio.sleep(5)  # Send every 5 seconds

                sid = call_sid_holder.get("sid")
                if sid and session_memory.get(sid, {}).get("clean_websocket_close"):
                    print(f"🧼 Stopping deepgram_text_keepalive for {sid} (clean_websocket_close=True)")
                    break

                try:
                    dg_connection.send(json.dumps({"type": "KeepAlive"}))
                    print(f"📡 Used .send for Silence Fram in deepgram_text_keepalive")
                    #print(f"📨 Sent text KeepAlive at {time.time()}")

                except Exception as e:
                    print(f"❌ Error sending text KeepAlive: {e}")
                    break  # Stop the loop if the connection is closed or broken

        loop.create_task(deepgram_text_keepalive())

        async def monitor_user_done():
            while not finished["done"]:
                await asyncio.sleep(0.5)
                elapsed = time.time() - last_input_time["ts"]

                if (
                    elapsed > 2.0 and
                    last_transcript["confidence"] >= 0.5 and
                    last_transcript.get("is_final", False)
                ):
                    print(
                        f"✅ User finished speaking (elapsed: {elapsed:.1f}s, "
                        f"confidence: {last_transcript['confidence']}"
                    )
                    finished["done"] = True

                    print("⏳ Waiting for POST to handle GPT + TTS...")

                    # We need the CallSid to look up audio_path in Redis
                    sid = call_sid_holder.get("sid")
                    if not sid:
                        print("⚠️ No Call SID in call_sid_holder, cannot check audio_path")
                        return

                    # 🔁 Poll Redis (via get_last_audio_for_call) for up to 4 seconds
                    for _ in range(40):  # up to 4 seconds
                        try:
                            audio_path = await get_last_audio_for_call(sid)
                        except Exception as e:
                            print(f"⚠️ Error calling get_last_audio_for_call({sid}): {e}")
                            audio_path = None

                        if audio_path and os.path.exists(audio_path):
                            print(f"✅ POST-generated audio is ready: {audio_path}")
                            break

                        await asyncio.sleep(0.1)
                    else:
                        print("❌ Timed out waiting for POST to generate GPT audio.")

        # schedule it
        loop.create_task(monitor_user_done())

        async def sender():
            send_counter = 0  # already there
            last_recv_log = 0.0  # already there

            while True:
                # 🛑 If some other task already closed the WebSocket, exit cleanly
                if ws_state["closed"]:
                    print("ℹ️ sender(): ws_state.closed=True, exiting sender loop")
                    break

                try:
                    raw = await ws.receive_text()

                    now = time.time()
                    if now - last_recv_log >= 0.5:  # only log every 500ms
                        print("📡 Used ws.receive_text in Sender")
                        last_recv_log = now

                except WebSocketDisconnect:
                    print("✖️ Twilio WebSocket disconnected (sender)")
                    ws_state["closed"] = True
                    break

                except Exception as e:
                    msg = str(e)
                    if "not connected" in msg or "Need to call \"accept\" first" in msg:
                        # This just means the socket was already closed elsewhere
                        print(f"ℹ️ sender(): WebSocket not connected anymore ({e}), exiting loop")
                        ws_state["closed"] = True
                        break

                    # Only truly unexpected stuff gets logged as an error
                    print(f"⚠️ Unexpected error receiving message: {e}")
                    ws_state["closed"] = True
                    break

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as e:
                    print(f"⚠️ JSON decode error: {e}")
                    continue

                # ... rest of your event handling (start/media/stop) unchanged ...
 
                event = msg.get("event")

                if event == "start":
                    sid = (
                        msg["start"].get("callSid")
                        or msg["start"].get("CallSid")
                        or msg["start"].get("callerSid")
                        or msg["start"].get("CallerSid")
                    )

                    call_sid_holder["sid"] = sid

                    session = session_memory.setdefault(sid, {})
                    session["close_requested"] = False   # ← RESET HERE ONLY

                    # Reset deepgram_is_final_watchdog
                    session["warned"] = False
                    print(f"🚩 Flag set: warned = False for session")
                    session["last_is_final_time"] = None

                    # 🔁 Init / reset audio buffer for this call
                    session["audio_buffer"] = bytearray()
                    print(f"🧺 Initialized audio_buffer for {sid}")

                    print(f"📞 Stream started for {sid}, close_requested=False")

                    #Let Keep Alive Logic Run 
                    session_memory[sid]["clean_websocket_close"] = False
                    print("🧼 clean_websocket_close = False")

                elif event == "media":
                    try:
                        payload = base64.b64decode(msg["media"]["payload"])
                        dg_connection.last_media_time = time.time()

                        # 🔊 Look up the current sid
                        sid = call_sid_holder.get("sid")
                        if sid:
                            session = session_memory.setdefault(sid, {})

                            # 🧺 Get / init buffer
                            buf = session.setdefault("audio_buffer", bytearray())
                            buf.extend(payload)

                            # 🧽 Keep only the last MAX_BUFFER_BYTES
                            if len(buf) > MAX_BUFFER_BYTES:
                                # keep tail only
                                session["audio_buffer"] = buf[-MAX_BUFFER_BYTES:]

                        # 🔴 Try to send live to Deepgram (may fail during reconnect)   
                        try:
                            dg_connection.send(payload)

                            # throttle this log: only print ~every 50 sends
                            if send_counter % 50 == 0:
                                print(f"📡 Used .send for payload in sender (count={send_counter})")
                            send_counter += 1

                        except Exception as e:
                            print(f"⚠️ Error sending to Deepgram (live): {e}")

                    except Exception as e:
                        print(f"⚠️ Error processing Twilio media: {e}")
                        
                elif event == "stop":
                    print("⏹ Stream stopped by Twilio")
                    break

        await sender()

    except Exception as e:
        print(f"⛔ Deepgram error: {e}")

    finally:
        if dg_connection:
            try:
                dg_connection.finish()
            except Exception as e:
                print(f"⚠️ Error closing Deepgram connection: {e}")

        sid = call_sid_holder.get("sid")

        try:
            if ws_state["closed"]:
                print(f"ℹ️ finally: ws_state.closed already True (sid={sid}), skipping ws.close()")
            else:
                if sid:
                    session = session_memory.setdefault(sid, {})
                    if not session.get("clean_websocket_close", False):
                        print(f"🔻 finally: WebSocket still open for {sid}, closing now")
                        session["clean_websocket_close"] = True
                        ws_state["closed"] = True
                        await ws.close()
                        print(f"🧼 clean_websocket_close from sender = True for {sid} (finally)")
                    else:
                        print(f"ℹ️ finally: clean_websocket_close already True for {sid}, skipping ws.close()")
                else:
                    print(f"🔻 [WS CLOSE] About to call ws.close() for sid={sid} at {time.time():.3f}")
                    ws_state["closed"] = True
                    await ws.close()
                    print(f"✅ [WS CLOSE] ws.close() completed for sid={sid} at {time.time():.3f}")
        except Exception as e:
            print(f"⚠️ Error closing WebSocket in finally: {e}")

        print("✅ Connection closed")
        
