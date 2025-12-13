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
    # ✅ BUT: transcript_version is Redis-only (do NOT write it to session_memory)
    session = session_memory.setdefault(call_sid, {})
    for k, v in fields.items():
        if k == "transcript_version":
            continue
        session[k] = v

async def get_last_audio_for_call(call_sid: str):
    """
    Return the latest audio_path for this call from:
    1) session_memory (fast, in-process cache)
    2) Redis hash (fallback: key = call_sid, field = "audio_path")
    """
    # 1) Try local in-memory cache first
    data = session_memory.get(call_sid)
    if data and "audio_path" in data:
        log(f"🎧 Retrieved audio path for {call_sid} from session_memory: {data['audio_path']}")
        return data["audio_path"]

    # 2) Fallback to Redis
    if redis_client is not None:
        try:
            start = time.perf_counter()
            audio_path = await redis_client.hget(call_sid, "audio_path")
            elapsed_ms = (time.perf_counter() - start) * 1000.0

            log(f"⏱️ Redis hget audio_path for {call_sid} took {elapsed_ms:.2f} ms")

            if audio_path:
                log(f"🎧 Retrieved audio path for {call_sid} from Redis: {audio_path}")
                # 🔁 Cache in session_memory for future quick access
                session = session_memory.setdefault(call_sid, {})
                session["audio_path"] = audio_path
                return audio_path

        except Exception as e:
            log(f"❌ Redis hget failed in get_last_audio_for_call for {call_sid}: {e}")

    # 3) Nothing found anywhere
    logging.error(f"❌ No audio path found for {call_sid} in session_memory or Redis.")
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

    # ✅ Redis-only flag write (no session_memory flag write)
    if redis_client is not None:
        try:
            start_redis = time.perf_counter()
            # store as JSON so you can json.loads(...) elsewhere to get a real bool
            await redis_client.hset(call_sid, mapping={"ffmpeg_audio_ready": json.dumps(True)})
            elapsed_ms = (time.perf_counter() - start_redis) * 1000.0
            log(f"🚩 Redis flag set: ffmpeg_audio_ready=True for {call_sid} ({elapsed_ms:.2f} ms)")
        except Exception as e:
            log(f"❌ Redis hset ffmpeg_audio_ready failed for {call_sid}: {e}")
    else:
        log("⚠️ redis_client is None — ffmpeg_audio_ready flag was NOT set")

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

# ✅ GPT handler function
async def get_gpt_response(call_sid: str) -> None:
    try:
        # 1) Read user_transcript – prefer Redis, fall back to session_memory
        gpt_input = None

        if redis_client is not None:
            try:
                gpt_input = await redis_client.hget(call_sid, "user_transcript")
            except Exception as e:
                log(f"⚠️ Redis hget failed in get_gpt_response for {call_sid}: {e}")

        if gpt_input is None:
            gpt_input = session_memory.get(call_sid, {}).get("user_transcript")

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

        # 3) Save to Redis
        fields = {
            "gpt_text": gpt_text,
            "gpt_response_ready": "1",   # strings in Redis
        }

        if redis_client is not None:
            try:
                await redis_client.hset(call_sid, mapping=fields)
            except Exception as e:
                log(f"❌ Redis hset failed in get_gpt_response for {call_sid}: {e}")

        # 4) Also keep local cache in session_memory (during migration)
        session = session_memory.setdefault(call_sid, {})
        session["gpt_text"] = gpt_text
        session["gpt_response_ready"] = True

        print(f"🚩 Flag set: gpt_response_ready = True for session {call_sid}")

    except Exception as e:
        print(f"⚠️ GPT Error: {e}")

        fallback_text = "[GPT failed to respond]"
        fields = {
            "gpt_text": fallback_text,
            "gpt_response_ready": "1",
        }

        # Try to persist even on error so the POST route doesn't hang forever
        if redis_client is not None:
            try:
                await redis_client.hset(call_sid, mapping=fields)
            except Exception as e2:
                log(f"❌ Redis hset failed in error path for {call_sid}: {e2}")

        session = session_memory.setdefault(call_sid, {})
        session["gpt_text"] = fallback_text
        session["gpt_response_ready"] = True

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

    # 🧠 Load session snapshot from Redis (preferred) + keep local cache for migration
    session: dict = session_memory.get(call_sid, {}).copy()

    # ✅ transcript_version is Redis-only: never keep/copy it in local session snapshot
    session.pop("transcript_version", None)

    if redis_client is not None:
        try:
            redis_session = await redis_client.hgetall(call_sid)  # returns dict[str,str]
            print(f"🧠 Redis session for {call_sid}: {redis_session}")
            # Merge Redis values into local snapshot (strings are fine for flags)
            session.update(redis_session)

            # ✅ transcript_version is Redis-only: do not keep/copy it into session_memory
            session.pop("transcript_version", None)

        except Exception as e:
            print(f"⚠️ Failed to read Redis session for {call_sid}: {e}")
    else:
        print(f"🧠 Current session_memory keys: {list(session_memory.keys())}")

    # 🚦 Check if greeting has already been played
    # Note: in Redis this may be stored as "True"/"1"/"yes" – treat any truthy as played
    greeting_played_raw = session.get("greeting_played")
    greeting_played = str(greeting_played_raw).lower() in {"1", "true", "yes"} if greeting_played_raw is not None else False

    if not greeting_played:
        # Mark as played (local + Redis)
        session["greeting_played"] = True

        # ✅ transcript_version is Redis-only: ensure it is NOT written into session_memory
        session.pop("transcript_version", None)
        session_memory[call_sid] = session  # keep cache updated

        if redis_client is not None:
            try:
                await redis_client.hset(call_sid, mapping={"greeting_played": True})
            except Exception as e:
                log(f"⚠️ Failed to write greeting_played to Redis for {call_sid}: {e}")

        vr = VoiceResponse()
        vr.redirect("/greeting")
        print("👋 First-time caller — redirecting to greeting handler.")
        return Response(content=str(vr), media_type="application/xml")

    # 🧾 Fetch transcript-related fields primarily from Redis
    user_transcript = None
    transcript_version = 0.0
    last_responded_version = 0.0

    if redis_client is not None:
        try:
            user_transcript, tv_raw, lr_raw = await redis_client.hmget(
                call_sid,
                "user_transcript",
                "transcript_version",
                "last_responded_version",
            )

            # Convert numeric-ish fields
            transcript_version = float(tv_raw) if tv_raw is not None else 0.0
            last_responded_version = float(lr_raw) if lr_raw is not None else 0.0
        except Exception as e:
            log(f"⚠️ Redis HMGET failed for {call_sid}: {e}")
            # Fallback to local cache (✅ but transcript_version stays Redis-only)
            data = session_memory.get(call_sid, {})
            user_transcript = data.get("user_transcript")
            transcript_version = 0.0  # ✅ DO NOT read transcript_version from session_memory
            last_responded_version = data.get("last_responded_version", 0.0) or 0.0
    else:
        # Pure in-memory fallback (✅ but transcript_version stays Redis-only)
        data = session_memory.get(call_sid, {})
        user_transcript = data.get("user_transcript")
        transcript_version = 0.0  # ✅ DO NOT read transcript_version from session_memory
        last_responded_version = data.get("last_responded_version", 0.0) or 0.0

    # (This old var isn't used in logic anymore; you can delete if you want)
    last_known_version = transcript_version

    if user_transcript and transcript_version > last_responded_version:
        gpt_input = user_transcript
        new_version = transcript_version

        # Mark this version as responded to (Redis + local)
        session_memory.setdefault(call_sid, {})["last_responded_version"] = new_version

        if redis_client is not None:
            try:
                await redis_client.hset(
                    call_sid,
                    mapping={"last_responded_version": new_version}
                )
            except Exception as e:
                log(f"⚠️ Failed to write last_responded_version to Redis for {call_sid}: {e}")

        print(f"✅ Transcript ready v{new_version}: {gpt_input!r}")

    elif user_transcript:  # user_transcript exists but version not newer
        log(
            f"⛔ Skipped GPT input for {call_sid}: "
            f"user_transcript={repr(user_transcript)}, "
            f"version={transcript_version}, last_responded={last_responded_version}"
        )
        return Response(content="No new transcript yet", media_type="application/xml")

    else:  # truly no transcript — redirect
        vr = VoiceResponse()
        vr.redirect("/wait")
        print("⏳ No new transcript — redirecting to /wait")
        return Response(content=str(vr), media_type="application/xml")

    print(f"📝 GPT input candidate: \"{gpt_input}\"")
    now_ts = time.time()

    # Store debug timestamp (local + Redis)
    session_memory.setdefault(call_sid, {})["debug_gpt_input_logged_at"] = now_ts

    if redis_client is not None:
        try:
            await redis_client.hset(
                call_sid,
                mapping={"debug_gpt_input_logged_at": now_ts}
            )
        except Exception as e:
            log(f"⚠️ Failed to write debug_gpt_input_logged_at to Redis for {call_sid}: {e}")

    vr = VoiceResponse()
    vr.redirect("/2")  # ✅ First redirect
    print("👋 Redirecting to /2")
    return Response(str(vr), media_type="application/xml")
    
@app.post("/2")
async def post2(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid")

    # --- 1️⃣ Load user_transcript from Redis (preferred), fallback to session_memory ---
    gpt_input = None

    if redis_client is not None:
        try:
            gpt_input = await redis_client.hget(call_sid, "user_transcript")
        except Exception as e:
            log(f"⚠️ Redis hget(user_transcript) failed for {call_sid}: {e}")

    if gpt_input is None:
        gpt_input = session_memory.get(call_sid, {}).get("user_transcript")

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

        # Redis flag
        if redis_client is not None:
            try:
                await redis_client.hset(
                    call_sid,
                    mapping={"get_gpt_response_started": True}
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

    if not session_memory[call_sid].get("11labs_audio_fetch_started"):
        session_memory[call_sid]["11labs_audio_fetch_started"] = True
        asyncio.create_task(get_11labs_audio(call_sid))
        print("🚀 Started 11Labs task in background")

    vr = VoiceResponse()

    if session_memory[call_sid].get("11labs_audio_ready"):
        print("✅ 11 Labs audio is ready — redirecting to /4")
        vr.redirect("/4")
        return Response(str(vr), media_type="application/xml")  # ✅ FIX
    else:
        vr.redirect("/wait3")
        print("👋 Redirecting to /wait3")
        return Response(str(vr), media_type="application/xml")

@app.post("/4")
async def post4(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid")

    # 🔒 1) Make sure we actually have a session for this CallSid
    session = session_memory.get(call_sid)
    if not session:
        print(f"❌ /4 hit but no session_memory entry for {call_sid}")
        vr = VoiceResponse()
        vr.say("Sorry, something went wrong. Let me reset.")
        vr.redirect("/")   # or vr.redirect("/3")
        return Response(str(vr), media_type="application/xml")

    # From here on, use `session` instead of indexing session_memory[call_sid]
    unique_id = session.get("unique_id")
    file_path = session.get("file_path")

    # 🔒 2) If ElevenLabs step never populated these, handle gracefully
    if not unique_id or not file_path:
        print(f"❌ Missing unique_id or file_path in session_memory for {call_sid}")
        vr = VoiceResponse()
        vr.redirect("/3")
        return Response(str(vr), media_type="application/xml")

    # ✅ Redis-only read for ffmpeg_audio_fetch_started (do NOT read from session_memory)
    ffmpeg_audio_fetch_started = False
    if redis_client is not None:
        try:
            raw = await redis_client.hget(call_sid, "ffmpeg_audio_fetch_started")
            if raw is not None:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="ignore")
                ffmpeg_audio_fetch_started = bool(json.loads(raw))
        except Exception as e:
            log(f"❌ Redis hget ffmpeg_audio_fetch_started failed for {call_sid}: {e}")
    else:
        log("⚠️ redis_client is None — treating ffmpeg_audio_fetch_started as False")

    # 🔒 3) Kick off FFmpeg only once (Redis-gated)
    if not ffmpeg_audio_fetch_started:
        # ✅ Redis-only write (do NOT write ffmpeg_audio_fetch_started to session_memory)
        if redis_client is not None:
            try:
                start_redis = time.perf_counter()
                await redis_client.hset(call_sid, mapping={"ffmpeg_audio_fetch_started": json.dumps(True)})
                elapsed_ms = (time.perf_counter() - start_redis) * 1000.0
                log(f"🚩 Redis flag set: ffmpeg_audio_fetch_started=True for {call_sid} ({elapsed_ms:.2f} ms)")
            except Exception as e:
                log(f"❌ Redis hset ffmpeg_audio_fetch_started failed for {call_sid}: {e}")

        asyncio.create_task(convert_audio_ulaw(call_sid, file_path, unique_id))
        print("🚀 Started FFmpeg task in background")

        session["11labs_audio_fetch_started"] = False
        print(f"🚩 Flag set: 11labs_audio_fetch_started = False for session {call_sid}")
        session["11labs_audio_ready"] = False
        print(f"🚩 Flag set: 11labs_audio_ready = False for session {call_sid}")

    vr = VoiceResponse()

    # ✅ Redis-only read for ffmpeg_audio_ready (do NOT read from session_memory)
    ffmpeg_audio_ready = False
    if redis_client is not None:
        try:
            raw = await redis_client.hget(call_sid, "ffmpeg_audio_ready")
            if raw is not None:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="ignore")
                ffmpeg_audio_ready = bool(json.loads(raw))
        except Exception as e:
            log(f"❌ Redis hget ffmpeg_audio_ready failed for {call_sid}: {e}")
    else:
        log("⚠️ redis_client is None — treating ffmpeg_audio_ready as False")

    # 🔒 4) Only redirect to /5 once FFmpeg says the audio is ready
    if ffmpeg_audio_ready:
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

    # ✅ Redis-only write (do NOT write ffmpeg_audio_ready to session_memory)
    if redis_client is not None:
        try:
            start_redis = time.perf_counter()
            await redis_client.hset(call_sid, mapping={"ffmpeg_audio_ready": json.dumps(False)})
            elapsed_ms = (time.perf_counter() - start_redis) * 1000.0
            log(f"🚩 Redis flag set: ffmpeg_audio_ready=False for {call_sid} ({elapsed_ms:.2f} ms)")
        except Exception as e:
            log(f"❌ Redis hset ffmpeg_audio_ready failed for {call_sid}: {e}")
    else:
        log("⚠️ redis_client is None — ffmpeg_audio_ready flag was NOT set to False")

    print(f"🚩 Flag set: ffmpeg_audio_ready = False for session {call_sid}")

    # ✅ Redis-only reset (do NOT write ffmpeg_audio_fetch_started to session_memory)
    if redis_client is not None:
        try:
            start_redis = time.perf_counter()
            await redis_client.hset(call_sid, mapping={"ffmpeg_audio_fetch_started": json.dumps(False)})
            elapsed_ms = (time.perf_counter() - start_redis) * 1000.0
            log(f"🚩 Redis flag set: ffmpeg_audio_fetch_started=False for {call_sid} ({elapsed_ms:.2f} ms)")
        except Exception as e:
            log(f"❌ Redis hset ffmpeg_audio_fetch_started failed for {call_sid}: {e}")
    else:
        log("⚠️ redis_client is None — ffmpeg_audio_fetch_started flag was NOT set to False")

    print(f"🚩 Flag set: ffmpeg_audio_fetch_started = False for session {call_sid}")

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
        current_path = await get_last_audio_for_call(call_sid)
        log(f"🔁 Checking session memory for {call_sid} → {current_path}")
        if current_path and os.path.exists(current_path):
            audio_path = current_path
            break
        await asyncio.sleep(1)

    if audio_path:
        ulaw_filename = os.path.basename(audio_path)

        block_start_time = time.time()
        session_memory.setdefault(call_sid, {})["block_start_time"] = block_start_time
        print(f"✅ Set block_start_time: {block_start_time}")

        # ✅ Redis-only write for ai_is_speaking=True (do NOT write to session_memory)
        if redis_client is not None:
            try:
                start_redis = time.perf_counter()
                await redis_client.hset(call_sid, mapping={"ai_is_speaking": json.dumps(True)})
                elapsed_ms = (time.perf_counter() - start_redis) * 1000.0
                log(f"🚩 Redis flag set: ai_is_speaking=True for {call_sid} ({elapsed_ms:.2f} ms)")
            except Exception as e:
                log(f"❌ Redis hset ai_is_speaking failed for {call_sid}: {e}")
        else:
            log("⚠️ redis_client is None — ai_is_speaking flag was NOT set to True")

        print(f"🚩 Flag set: ai_is_speaking = True for session {call_sid} at {time.time()}")

        logger.info(f"🟥 [User Input] Processing complete — unblocking writes for {call_sid}")

        # ✅ user_response_processing is Redis-only: set False via Redis (do NOT write to session_memory)
        if redis_client is not None:
            try:
                start_redis = time.perf_counter()
                await redis_client.hset(call_sid, mapping={"user_response_processing": json.dumps(False)})
                elapsed_ms = (time.perf_counter() - start_redis) * 1000.0
                log(f"🚩 Redis flag set: user_response_processing=False for {call_sid} ({elapsed_ms:.2f} ms)")
            except Exception as e:
                log(f"❌ Redis hset user_response_processing failed for {call_sid}: {e}")
        else:
            log("⚠️ redis_client is None — user_response_processing flag was NOT set to False")

        vr.play(f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}")
        print("🔗 Final playback URL:", f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}")
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
        # Local cache (leave as-is EXCEPT ai_is_speaking)
        session = session_memory.setdefault(call_sid, {})
        session["block_start_time"] = block_start_time
        session["user_response_processing"] = False  # (leave as-is per your request)

        print(f"✅ Set block_start_time: {block_start_time}")
        print(f"🚩 Flag set: ai_is_speaking = True (Redis-only) for session {call_sid} at {time.time()}")

        logger.info(f"🟥 [User Input] Processing complete — unblocking writes for {call_sid}")

        # 🔁 Mirror flags into Redis (ai_is_speaking is Redis-only now)
        if redis_client is not None:
            try:
                await redis_client.hset(
                    call_sid,
                    mapping={
                        "block_start_time": block_start_time,
                        "ai_is_speaking": json.dumps(True),          # ✅ Redis-only source of truth
                        "user_response_processing": False,           # leave as-is per your request
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
            def _parse_boolish(raw, default: bool = False) -> bool:
                if raw is None:
                    return default

                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="ignore")

                if isinstance(raw, bool):
                    return raw

                s = str(raw).strip().lower()
                if s in {"1", "true", "yes", "y", "t"}:
                    return True
                if s in {"0", "false", "no", "n", "f"}:
                    return False

                # handles json.dumps(True/False) => "true"/"false"
                try:
                    return bool(json.loads(s))
                except Exception:
                    return default

            while True:
                await asyncio.sleep(0.02)
                sid = call_sid_holder.get("sid")
                if not sid:
                    continue

                # ✅ Redis-only read for close_requested (no session_memory fallback)
                close_requested = False
                if redis_client is not None:
                    try:
                        raw = await redis_client.hget(sid, "close_requested")
                        close_requested = _parse_boolish(raw, default=False)
                    except Exception as e:
                        print(f"⚠️ Redis hget close_requested failed for {sid}: {e}")
                else:
                    close_requested = False  # Redis disabled => treat as not requested

                if close_requested:
                    print(f"🛑 Closing Deepgram for {sid}")
                    try:
                        dg_connection.finish()
                    except Exception as e:
                        print(f"⚠️ Error closing Deepgram for {sid}: {e}")

                    if ws_state["closed"]:
                        print(f"ℹ️ deepgram_close_watchdog: ws already closed for {sid}, skipping ws.close()")
                        return

                    # ✅ Redis-only write for clean_websocket_close=True (NO session_memory fallback)
                    if redis_client is not None:
                        try:
                            await redis_client.hset(sid, mapping={"clean_websocket_close": json.dumps(True)})
                            print(f"🧼 Redis clean_websocket_close=True for {sid} (deepgram_close_watchdog)")
                        except Exception as e:
                            print(f"⚠️ Redis hset clean_websocket_close failed for {sid}: {e}")
                    else:
                        print("⚠️ redis_client is None — clean_websocket_close NOT set (Redis-only flag)")

                    # ✅ Mark closed *before* calling ws.close(), so other loops can see it
                    ws_state["closed"] = True

                    try:
                        print(f"🔻 deepgram_close_watchdog: calling ws.close() for {sid}")
                        await ws.close()
                    except Exception as e:
                        print(f"⚠️ Error closing WebSocket in deepgram_close_watchdog: {e}")

                    return

        loop.create_task(deepgram_close_watchdog())

        async def deepgram_is_final_watchdog():
            def _parse_boolish(raw, default: bool = False) -> bool:
                if raw is None:
                    return default
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="ignore")
                if isinstance(raw, bool):
                    return raw

                s = str(raw).strip().lower()
                if s in {"1", "true", "yes", "y", "t"}:
                    return True
                if s in {"0", "false", "no", "n", "f"}:
                    return False

                # handles json.dumps(True/False) => "true"/"false"
                try:
                    return bool(json.loads(s))
                except Exception:
                    return default

            while True:
                await asyncio.sleep(0.02)

                sid = call_sid_holder.get("sid")
                if not sid:
                    continue

                # make sure session exists (still used for other fields, as requested)
                session = session_memory.setdefault(sid, {})

                # ✅ Redis-only read for warned (do NOT initialize/use session["warned"])
                warned = False
                if redis_client is not None:
                    try:
                        raw = await redis_client.hget(sid, "warned")
                        if raw is not None:
                            if isinstance(raw, (bytes, bytearray)):
                                raw = raw.decode("utf-8", errors="ignore")
                            warned = bool(json.loads(raw))
                    except Exception as e:
                        log(f"❌ Redis hget warned failed for {sid}: {e}")
                else:
                    log("⚠️ redis_client is None — treating warned as False")

                # ✅ Redis-only read for user_response_processing (do NOT read from session_memory)
                user_response_processing = False
                if redis_client is not None:
                    try:
                        raw = await redis_client.hget(sid, "user_response_processing")
                        if raw is not None:
                            if isinstance(raw, (bytes, bytearray)):
                                raw = raw.decode("utf-8", errors="ignore")
                            user_response_processing = bool(json.loads(raw))
                    except Exception as e:
                        log(f"❌ Redis hget user_response_processing failed for {sid}: {e}")
                else:
                    log("⚠️ redis_client is None — treating user_response_processing as False")

                # ✅ Redis-only read for ai_is_speaking (do NOT read from session_memory)
                ai_is_speaking = False
                if redis_client is not None:
                    try:
                        raw = await redis_client.hget(sid, "ai_is_speaking")
                        if raw is not None:
                            if isinstance(raw, (bytes, bytearray)):
                                raw = raw.decode("utf-8", errors="ignore")

                            s = str(raw).strip().lower()
                            if s in {"1", "true", "yes", "y", "t"}:
                                ai_is_speaking = True
                            elif s in {"0", "false", "no", "n", "f"}:
                                ai_is_speaking = False
                            else:
                                # fallback if you stored json.dumps(true/false)
                                ai_is_speaking = bool(json.loads(raw))
                    except Exception as e:
                        log(f"❌ Redis hget ai_is_speaking failed for {sid}: {e}")
                else:
                    log("⚠️ redis_client is None — treating ai_is_speaking as False")

                # ✅ Redis-only read for close_requested (NO session_memory fallback)
                close_requested = False
                if redis_client is not None:
                    try:
                        raw = await redis_client.hget(sid, "close_requested")
                        close_requested = _parse_boolish(raw, default=False)
                    except Exception as e:
                        log(f"❌ Redis hget close_requested failed for {sid}: {e}")
                        close_requested = False
                else:
                    close_requested = False  # Redis disabled => treat as not requested

                last_time = session.get("last_is_final_time")
                if not last_time:
                    continue  # no is_final seen yet

                elapsed = time.time() - last_time

                if (
                    elapsed > 2.5
                    and not warned
                    and close_requested is False          # ✅ Redis-only gate
                    and ai_is_speaking is False           # ✅ Redis-only gate
                    and user_response_processing is False # ✅ Redis-only gate
                ):

                    print(f"⚠️ No is_final received in {elapsed:.2f}s for {sid}")

                    # ✅ Redis-only write for warned=True (do NOT write to session_memory)
                    if redis_client is not None:
                        try:
                            start_redis = time.perf_counter()
                            await redis_client.hset(sid, mapping={"warned": json.dumps(True)})
                            elapsed_ms = (time.perf_counter() - start_redis) * 1000.0
                            log(f"🚩 Redis flag set: warned=True for {sid} ({elapsed_ms:.2f} ms)")
                        except Exception as e:
                            log(f"❌ Redis hset warned failed for {sid}: {e}")
                    else:
                        log("⚠️ redis_client is None — warned flag was NOT set")

                    print(f"🚩 Flag set: warned = True for session {sid}")

                    # ✅ Redis-only write (do NOT write zombie_detected to session_memory)
                    if redis_client is not None:
                        try:
                            start_redis = time.perf_counter()
                            await redis_client.hset(sid, mapping={"zombie_detected": json.dumps(True)})
                            elapsed_ms = (time.perf_counter() - start_redis) * 1000.0
                            log(f"🧟 Redis flag set: zombie_detected=True for {sid} ({elapsed_ms:.2f} ms)")
                        except Exception as e:
                            log(f"❌ Redis hset zombie_detected failed for {sid}: {e}")
                    else:
                        log("⚠️ redis_client is None — zombie_detected flag was NOT set")

                    print(f"🧟 Detected Deepgram zombie stream for {sid}, reconnecting...")

        loop.create_task(deepgram_is_final_watchdog())
        
        async def deepgram_error_reconnection():
            nonlocal dg_connection  # so we can replace the shared connection

            while True:
                await asyncio.sleep(1)  # check every second

                sid = call_sid_holder.get("sid")
                if not sid:
                    continue

                session = session_memory.setdefault(sid, {})

                # ✅ Redis-only read for zombie_detected (do NOT read from session_memory)
                zombie_detected = False
                if redis_client is not None:
                    try:
                        raw = await redis_client.hget(sid, "zombie_detected")
                        if raw is not None:
                            if isinstance(raw, (bytes, bytearray)):
                                raw = raw.decode("utf-8", errors="ignore")
                            zombie_detected = bool(json.loads(raw))
                    except Exception as e:
                        log(f"❌ Redis hget zombie_detected failed for {sid}: {e}")
                else:
                    log("⚠️ redis_client is None — treating zombie_detected as False")

                if not zombie_detected:
                    continue  # nothing to do

                print(f"💀 Zombie detected for sid={sid} — reconnecting Deepgram")

                # ✅ Reset zombie_detected via Redis ONLY
                if redis_client is not None:
                    try:
                        start_redis = time.perf_counter()
                        await redis_client.hset(sid, mapping={"zombie_detected": json.dumps(False)})
                        elapsed_ms = (time.perf_counter() - start_redis) * 1000.0
                        log(f"🧟 Redis flag reset: zombie_detected=False for {sid} ({elapsed_ms:.2f} ms)")
                    except Exception as e:
                        log(f"❌ Redis hset zombie_detected reset failed for {sid}: {e}")
                else:
                    log("⚠️ redis_client is None — zombie_detected flag was NOT reset")

                # ✅ warned is Redis-only: reset warned via Redis (do NOT write session['warned'])
                if redis_client is not None:
                    try:
                        start_redis = time.perf_counter()
                        await redis_client.hset(sid, mapping={"warned": json.dumps(False)})
                        elapsed_ms = (time.perf_counter() - start_redis) * 1000.0
                        log(f"🚩 Redis flag reset: warned=False for {sid} ({elapsed_ms:.2f} ms)")
                    except Exception as e:
                        log(f"❌ Redis hset warned reset failed for {sid}: {e}")
                else:
                    log("⚠️ redis_client is None — warned flag was NOT reset")

                # Reset other session flags for this sid (leave as-is)
                session["last_is_final_time"] = None

                try:
                    # 🔌 Close old connection if present
                    try:
                        if dg_connection is not None:
                            print("🔌 Finishing old Deepgram connection before reconnect…")
                            dg_connection.finish()
                    except Exception as e:
                        print(f"⚠️ Error finishing old Deepgram connection: {e}")

                    # 🔁 Create a new connection just like at startup
                    live_client = deepgram.listen.live
                    new_conn = await asyncio.to_thread(live_client.v, "1")

                    # Reattach handlers
                    new_conn.on(LiveTranscriptionEvents.Transcript, on_transcript)
                    new_conn.on(
                        LiveTranscriptionEvents.Error,
                        lambda err: print(f"🔴 Deepgram error (reconnected): {err}")
                    )
                    new_conn.on(
                        LiveTranscriptionEvents.Close,
                        lambda: print("🔴 Deepgram WebSocket closed (reconnected)")
                    )

                    # Start streaming with the same options you used originally
                    new_conn.start(options)

                    # Swap global connection reference so keepalives use the new one
                    dg_connection = new_conn

                    print("🔁 Deepgram reconnected successfully")

                    # ⏪ Flush buffered audio so Deepgram "catches up"
                    buffer = session.get("audio_buffer", None)
                    if buffer:
                        try:
                            new_conn.send(bytes(buffer))
                            print(f"⏪ Sent {len(buffer)} bytes of buffered audio after reconnect for {sid}")
                            # Optional: reset buffer or keep a short tail
                            session["audio_buffer"] = bytearray()
                        except Exception as e:
                            print(f"⚠️ Error sending buffered audio after reconnect: {e}")

                except Exception as e:
                    print(f"❌ Failed to reconnect Deepgram: {e}")

        loop.create_task(deepgram_error_reconnection())

        def _parse_boolish(raw, default: bool = False) -> bool:
            if raw is None:
                return default

            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="ignore")

            if isinstance(raw, bool):
                return raw

            s = str(raw).strip().lower()
            if s in {"1", "true", "yes", "y", "t"}:
                return True
            if s in {"0", "false", "no", "n", "f"}:
                return False

            # Try JSON ("true"/"false")
            try:
                return bool(json.loads(s))
            except Exception:
                return default


        async def redis_get_bool(hash_key: str, field: str, default: bool = False) -> bool:
            if redis_client is None:
                return default
            try:
                raw = await redis_client.hget(hash_key, field)
                return _parse_boolish(raw, default=default)
            except Exception as e:
                log(f"❌ Redis hget {field} failed for {hash_key}: {e}")
                return default

        async def redis_set_bool(hash_key: str, field: str, value: bool) -> None:
            if redis_client is None:
                log(f"⚠️ redis_client is None — did NOT set {field} for {hash_key}")
                return
            try:
                # Store as JSON boolean ("true"/"false")
                await redis_client.hset(hash_key, mapping={field: json.dumps(bool(value))})
            except Exception as e:
                log(f"❌ Redis hset {field} failed for {hash_key}: {e}")

        def _schedule_on_loop(coro) -> None:
            """
            Thread-safe scheduling: Deepgram callbacks are often not running on your asyncio loop thread.
            """
            try:
                asyncio.run_coroutine_threadsafe(coro, loop)
            except Exception as e:
                # If loop isn't running or something odd happened, log it so you see it.
                log(f"❌ Failed to schedule coroutine on loop: {e}")

        # ---------------------------------------------------------
        # Async handler for speech_final (Redis flag lives here)
        # ---------------------------------------------------------
        async def handle_speech_final(sid: str, full_transcript: str) -> None:
            # ✅ Redis-only STOP gate for user_response_processing
            if await redis_get_bool(sid, "user_response_processing", default=False):
                print(f"🚫 Ignoring transcript — already processing user response for {sid}")
                return

            if not full_transcript:
                log("⚠️ Skipping save — full_transcript is empty")
                return

            # Everything else stays session_memory-based (as you requested)
            session = session_memory.setdefault(sid, {})

            # --------
            # Redis-only helpers for ai_is_speaking (LOOSE parsing)
            # --------
            async def redis_get_bool_loose(hash_key: str, field: str, default: bool = False) -> bool:
                if redis_client is None:
                    return default
                try:
                    raw = await redis_client.hget(hash_key, field)
                    if raw is None:
                        return default
                    if isinstance(raw, (bytes, bytearray)):
                        raw = raw.decode("utf-8", errors="ignore")

                    s = str(raw).strip().lower()
                    if s in {"1", "true", "yes", "y", "t"}:
                        return True
                    if s in {"0", "false", "no", "n", "f"}:
                        return False

                    # If stored as json.dumps(True/False) => "true"/"false" (already handled),
                    # but if something else valid JSON shows up, try it:
                    return bool(json.loads(raw))
                except Exception as e:
                    log(f"❌ Redis hget {field} failed for {hash_key}: {e}")
                    return default

            async def redis_set_bool_json(hash_key: str, field: str, value: bool) -> None:
                if redis_client is None:
                    log(f"⚠️ redis_client is None — did NOT set {field} for {hash_key}")
                    return
                try:
                    # Store as valid JSON boolean ("true"/"false") for consistency
                    await redis_client.hset(hash_key, mapping={field: json.dumps(value)})
                except Exception as e:
                    log(f"❌ Redis hset {field} failed for {hash_key}: {e}")

            # Flip ai_is_speaking off if block_start_time + audio_duration passed
            block_start_time = session.get("block_start_time")
            audio_duration = session.get("audio_duration")
            if (
                block_start_time is not None
                and audio_duration is not None
                and time.time() > (block_start_time + audio_duration)
            ):
                # ✅ Redis-only write (do NOT write ai_is_speaking to session_memory)
                await redis_set_bool_json(sid, "ai_is_speaking", False)
                log(f"🏁 [{sid}] AI finished speaking. (Redis ai_is_speaking=False)")

            # ✅ Main save gate:
            # - user_response_processing is Redis-only (already)
            # - ai_is_speaking is now Redis-only (changed)
            user_processing = await redis_get_bool(sid, "user_response_processing", default=False)
            ai_is_speaking = await redis_get_bool_loose(sid, "ai_is_speaking", default=False)

            if (ai_is_speaking is False) and (user_processing is False):
                # ✅ Redis-only write for close_requested=True (do NOT write to session_memory)
                await redis_set_bool_json(sid, "close_requested", True)

                print(f"🛑 Requested Deepgram close for {sid} (accepted transcript)")

                session["user_transcript"] = full_transcript
                session["ready"] = True

                # ✅ Redis-only write for user_response_processing=True (do NOT write to session_memory)
                await redis_set_bool(sid, "user_response_processing", True)

                log(f"✍️ [{sid}] user_transcript saved at {time.time()}")
                loop.create_task(save_transcript(sid, user_transcript=full_transcript))
                logger.info(f"🟩 [User Input] Processing started — blocking writes for {sid}")

                # ✅ Clear after successful save (same behavior you had)
                final_transcripts.clear()
                last_transcript["text"] = ""
                last_transcript["confidence"] = 0.0
                last_transcript["is_final"] = False
            else:
                log(
                    f"🚫 [{sid}] Save skipped — "
                    f"ai_is_speaking={ai_is_speaking}, user_processing={user_processing}"
                )

                # 🧹 Clear junk to avoid stale input (same behavior you had)
                final_transcripts.clear()
                last_transcript["text"] = ""
                last_transcript["confidence"] = 0.0
                last_transcript["is_final"] = False

        # --------------------------------------
        # Deepgram transcript callback (SYNC)
        # --------------------------------------
        def on_transcript(*args, **kwargs):
            try:
                print("📥 RAW transcript event:")
                result = kwargs.get("result") or (args[0] if args else None)

                if result is None:
                    print("⚠️ No result received.")
                    return

                if not hasattr(result, "to_dict"):
                    print("⚠️ Result has no to_dict(); skipping")
                    return

                payload = result.to_dict()
                # Optional debug:
                # print(json.dumps(payload, indent=2))

                sid = call_sid_holder.get("sid")
                if not sid:
                    return

                speech_final = payload.get("speech_final", False)

                try:
                    alt = payload["channel"]["alternatives"][0]
                    sentence = alt.get("transcript", "")
                    confidence = alt.get("confidence", 0.0)
                    is_final = payload.get("is_final", False)

                    state["is_final"] = is_final
                    state["sentence"] = sentence
                    state["confidence"] = confidence

                    if is_final and sentence.strip() and confidence >= 0.6:
                        print(f"✅ Final transcript received: \"{sentence}\" (confidence: {confidence})")

                        # leave this in session_memory (you didn't ask to move it)
                        session_memory.setdefault(sid, {})["last_is_final_time"] = time.time()

                        last_input_time["ts"] = time.time()
                        last_transcript["text"] = sentence
                        last_transcript["confidence"] = confidence
                        last_transcript["is_final"] = True

                        final_transcripts.append(sentence)

                        if speech_final:
                            print("🧠 speech_final received — concatenating full transcript")
                            full_transcript = " ".join(final_transcripts)
                            log(f"🧪 [DEBUG] full_transcript after join: {repr(full_transcript)}")

                            # ✅ IMPORTANT: schedule async Redis work (NO await here)
                            _schedule_on_loop(handle_speech_final(sid, full_transcript))

                    elif is_final:
                        print(f"⚠️ Final transcript was too unclear: \"{sentence}\" (confidence: {confidence})")

                except KeyError as e:
                    print(f"⚠️ Missing expected key in payload: {e}")
                except Exception as inner_e:
                    print(f"⚠️ Could not extract transcript sentence: {inner_e}")

            except Exception as e:
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
            def _parse_boolish(raw, default: bool = False) -> bool:
                if raw is None:
                    return default

                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="ignore")

                if isinstance(raw, bool):
                    return raw

                s = str(raw).strip().lower()
                if s in {"1", "true", "yes", "y", "t"}:
                    return True
                if s in {"0", "false", "no", "n", "f"}:
                    return False

                # handles json.dumps(True/False) => "true"/"false"
                try:
                    return bool(json.loads(s))
                except Exception:
                    return default

            counter = 0
            while True:
                await asyncio.sleep(0.02)  # run every 20ms

                sid = call_sid_holder.get("sid")

                # ✅ Redis-only read for clean_websocket_close (NO session_memory fallback)
                clean_websocket_close = False
                if sid and redis_client is not None:
                    try:
                        raw = await redis_client.hget(sid, "clean_websocket_close")
                        clean_websocket_close = _parse_boolish(raw, default=False)
                    except Exception as e:
                        print(f"⚠️ Redis hget clean_websocket_close failed for {sid}: {e}")
                        clean_websocket_close = False

                # 🔍 Debug: is keepalive still running? (throttled)
                if counter % 50 == 0:  # ~once per second
                    print(
                        f"📡 keepalive still running for sid={sid}, "
                        f"clean_websocket_close={clean_websocket_close if sid else None}"
                    )
                counter += 1

                if sid and clean_websocket_close:
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
            def _parse_boolish(raw, default: bool = False) -> bool:
                if raw is None:
                    return default

                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="ignore")

                if isinstance(raw, bool):
                    return raw

                s = str(raw).strip().lower()
                if s in {"1", "true", "yes", "y", "t"}:
                    return True
                if s in {"0", "false", "no", "n", "f"}:
                    return False

                # handles json.dumps(True/False) => "true"/"false"
                try:
                    return bool(json.loads(s))
                except Exception:
                    return default

            while True:
                await asyncio.sleep(5)  # Send every 5 seconds

                sid = call_sid_holder.get("sid")

                # ✅ Redis-only read for clean_websocket_close (NO session_memory fallback)
                clean_websocket_close = False
                if sid and redis_client is not None:
                    try:
                        raw = await redis_client.hget(sid, "clean_websocket_close")
                        clean_websocket_close = _parse_boolish(raw, default=False)
                    except Exception as e:
                        print(f"⚠️ Redis hget clean_websocket_close failed for {sid}: {e}")
                        clean_websocket_close = False

                if sid and clean_websocket_close:
                    print(f"🧼 Stopping deepgram_text_keepalive for {sid} (clean_websocket_close=True)")
                    break

                try:
                    dg_connection.send(json.dumps({"type": "KeepAlive"}))
                    print(f"📡 Used .send for Silence Fram in deepgram_text_keepalive")
                    # print(f"📨 Sent text KeepAlive at {time.time()}")

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
                    print(f"✅ User finished speaking (elapsed: {elapsed:.1f}s, confidence: {last_transcript['confidence']})")
                    finished["done"] = True

                    print("⏳ Waiting for POST to handle GPT + TTS...")
                    for _ in range(40):  # up to 4 seconds
                        audio_path = session_memory.get(call_sid_holder["sid"], {}).get("audio_path")
                        if audio_path and os.path.exists(audio_path):
                            print(f"✅ POST-generated audio is ready: {audio_path}")
                            break
                        await asyncio.sleep(0.1)
                    else:
                        print("❌ Timed out waiting for POST to generate GPT audio.")

        loop.create_task(monitor_user_done())

        async def sender():
            send_counter = 0
            last_recv_log = 0.0

            while True:
                if ws_state["closed"]:
                    print("ℹ️ sender(): ws_state.closed=True, exiting sender loop")
                    break

                try:
                    raw = await ws.receive_text()

                    now = time.time()
                    if now - last_recv_log >= 0.5:
                        print("📡 Used ws.receive_text in sender")
                        last_recv_log = now

                except WebSocketDisconnect:
                    print("✖️ Twilio WebSocket disconnected (sender)")
                    ws_state["closed"] = True
                    break

                except Exception as e:
                    msg = str(e)
                    if "not connected" in msg or 'Need to call "accept" first' in msg:
                        print(f"ℹ️ sender(): WebSocket not connected anymore ({e}), exiting loop")
                        ws_state["closed"] = True
                        break

                    print(f"⚠️ Unexpected error receiving message: {e}")
                    ws_state["closed"] = True
                    break

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as e:
                    print(f"⚠️ JSON decode error: {e}")
                    continue

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

                    if redis_client is not None:
                        try:
                            start_redis = time.perf_counter()
                            await redis_client.hset(sid, mapping={"close_requested": json.dumps(False)})
                            elapsed_ms = (time.perf_counter() - start_redis) * 1000.0
                            log(f"🚩 Redis flag reset: close_requested=False for {sid} ({elapsed_ms:.2f} ms)")
                        except Exception as e:
                            log(f"❌ Redis hset close_requested reset failed for {sid}: {e}")
                    else:
                        log("⚠️ redis_client is None — close_requested was NOT reset (Redis-only flag)")

                    if redis_client is not None:
                        try:
                            start_redis = time.perf_counter()
                            await redis_client.hset(sid, mapping={"warned": json.dumps(False)})
                            elapsed_ms = (time.perf_counter() - start_redis) * 1000.0
                            log(f"🚩 Redis flag reset: warned=False for {sid} ({elapsed_ms:.2f} ms)")
                        except Exception as e:
                            log(f"❌ Redis hset warned reset failed for {sid}: {e}")
                    else:
                        log("⚠️ redis_client is None — warned flag was NOT reset")

                    session["last_is_final_time"] = None
                    session["audio_buffer"] = bytearray()

                    # ✅ clean_websocket_close is Redis-only (do NOT write to session_memory)
                    if redis_client is not None:
                        try:
                            start_redis = time.perf_counter()
                            await redis_client.hset(sid, mapping={"clean_websocket_close": json.dumps(False)})
                            elapsed_ms = (time.perf_counter() - start_redis) * 1000.0
                            log(f"🧼 Redis flag reset: clean_websocket_close=False for {sid} ({elapsed_ms:.2f} ms)")
                        except Exception as e:
                            log(f"❌ Redis hset clean_websocket_close reset failed for {sid}: {e}")
                    else:
                        log("⚠️ redis_client is None — clean_websocket_close was NOT reset (Redis-only flag)")

                    print(f"🧺 Initialized audio_buffer for {sid}")
                    print("🧼 clean_websocket_close = False (Redis)")
                    print(f"📞 Stream started for {sid}")

                elif event == "media":
                    try:
                        payload = base64.b64decode(msg["media"]["payload"])
                        dg_connection.last_media_time = time.time()

                        sid = call_sid_holder.get("sid")
                        if sid:
                            session = session_memory.setdefault(sid, {})
                            buf = session.setdefault("audio_buffer", bytearray())
                            buf.extend(payload)
                            if len(buf) > MAX_BUFFER_BYTES:
                                session["audio_buffer"] = buf[-MAX_BUFFER_BYTES:]

                        try:
                            dg_connection.send(payload)
                            if send_counter % 50 == 0:
                                print(f"📡 Used .send for payload in sender (count={send_counter})")
                            send_counter += 1
                        except Exception as e:
                            print(f"⚠️ Error sending to Deepgram (live): {e}")

                    except Exception as e:
                        print(f"⚠️ Error processing Twilio media: {e}")

                elif event == "stop":
                    print("⏹ Stream stopped by Twilio")
                    ws_state["closed"] = True
                    break
