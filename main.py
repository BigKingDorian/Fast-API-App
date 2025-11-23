import os
import json
import base64
import asyncio
import time
import uuid
import subprocess
import requests  # ✅ ElevenLabs API
import logging
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

# ⚙️ FastAPI app
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

def save_transcript(call_sid, user_transcript=None, audio_path=None, gpt_response=None):
    if call_sid not in session_memory:
        session_memory[call_sid] = {}

    if user_transcript:
        session_memory[call_sid]["user_transcript"] = user_transcript
        session_memory[call_sid]["transcript_version"] = time.time()  # 👈 Add this line

        # 🧪 Add log here to inspect the transcript
        log(f"📝 save_transcript helper Saved user_transcript for {call_sid}: {repr(user_transcript)}")
    else:
        # Optional: log when nothing is saved
        log(f"⚠️ save_transcript helper No user_transcript provided to save for {call_sid}")

    if gpt_response:
        session_memory[call_sid]["gpt_response"] = gpt_response
    if audio_path:
        session_memory[call_sid]["audio_path"] = audio_path

def get_last_audio_for_call(call_sid):
    data = session_memory.get(call_sid)

    if data and "audio_path" in data:
        log(f"🎧 Retrieved audio path for {call_sid}: {data['audio_path']}")
        return data["audio_path"]
    else:
        logging.error(f"❌ No audio path found for {call_sid} in session memory.")
        return None

async def convert_audio_ulaw(call_sid: str, file_path: str, unique_id: str):
    converted_path = f"static/audio/response_{unique_id}_ulaw.wav"

    # ── FFmpeg conversion ─────────────────────────────────────────────────────
    start = time.time()
    try:
        subprocess.run([
            "/usr/bin/ffmpeg", "-y", "-i", file_path,
            "-ar", "8000", "-ac", "1", "-c:a", "pcm_mulaw", converted_path
        ], check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ FFmpeg failed: {e}")
        return None
    end = time.time()

    print(f"⏱️ FFmpeg subprocess.run() took {end - start:.4f} seconds")
    print("🧭 Checking absolute path:", os.path.abspath(converted_path))

    # ── Wait for file to appear (race condition guard) ────────────────────────
    for i in range(40):
        if os.path.isfile(converted_path):
            print(f"✅ Found converted file after {i * 0.1:.1f}s")
            break
        await asyncio.sleep(0.1)
    else:
        print("❌ Converted file never appeared — aborting")
        return None

    print(f"🎛️ Converted WAV (8 kHz μ-law) → {converted_path}")
    log("✅ Audio file saved at %s", converted_path)

    # ── Measure duration with ffprobe ─────────────────────────────────────────
    try:
        duration = float(subprocess.check_output([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            converted_path
        ]))
        print(f"⏱️ Duration of audio file: {duration:.2f} seconds")
        session_memory[call_sid]["audio_duration"] = duration
    except Exception as e:
        print(f"⚠️ Failed to measure audio duration: {e}")

    # ── Save transcript and audio ─────────────────────────────────────────────
    audio_bytes = session_memory[call_sid].get("audio_bytes")
    gpt_text = session_memory[call_sid].get("gpt_text")

    if not audio_bytes:
        print("❌ audio_bytes missing in session_memory")
        return None

    if len(audio_bytes) > 2000:
        save_transcript(call_sid, audio_path=converted_path, gpt_response=gpt_text)
    else:
        print("⚠️ Skipping transcript/audio save due to likely blank response.")

    # ── Final flag ────────────────────────────────────────────────────────────
    session_memory[call_sid]["ffmpeg_audio_ready"] = True
    print(f"🚩 Flag set: ffmpeg_audio_ready = True for session {call_sid}")

    return converted_path

async def get_11labs_audio(call_sid):
    if call_sid not in session_memory:
        session_memory[call_sid] = {}

    # Reset GPT flags so next question works
    session_memory[call_sid]["gpt_response_ready"] = False
    session_memory[call_sid]["get_gpt_response_started"] = False
    session_memory[call_sid]["user_transcript"] = None

    # ✅ Retrieve GPT output saved in get_gpt_response()
    gpt_text = session_memory[call_sid].get("gpt_text", "[Missing GPT Output]")
    print(f"🧠 GPT returned text: {gpt_text}")

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
    print(f"🎙️ ElevenLabs status {elevenlabs_response.status_code}, "
            f"bytes {len(elevenlabs_response.content)}")

    audio_bytes = elevenlabs_response.content
    unique_id = uuid.uuid4().hex
    file_path = f"static/audio/response_{unique_id}.wav"

    # Save everything needed for later use in /4
    session_memory[call_sid]["unique_id"] = unique_id
    session_memory[call_sid]["file_path"] = file_path
    session_memory[call_sid]["audio_bytes"] = audio_bytes
    session_memory[call_sid]["gpt_text"] = gpt_text   # also required later

    with open(file_path, "wb") as f:
        f.write(audio_bytes)
    print(f"💾 Saved original WAV → {file_path}")

    session_memory[call_sid]["11labs_audio_ready"] = True
    print(f"🚩 Flag set: 11labs_audio_ready = True for session {call_sid}")

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
        gpt_input = session_memory[call_sid]["user_transcript"]
        safe_text = gpt_input.strip() if gpt_input else "Hello, how can I help you today?"

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a helpful AI assistant named Lotus. Keep your responses clear and concise."},
                {"role": "user", "content": safe_text}
            ]
        )

        gpt_text = response.choices[0].message.content or "[GPT returned empty message]"

        # ✅ Save to memory and flip the flag
        session_memory[call_sid]["gpt_text"] = gpt_text
        session_memory[call_sid]["gpt_response_ready"] = True
        print(f"🚩 Flag set: gpt_response_ready = True for session {call_sid}")

    except Exception as e:
        print(f"⚠️ GPT Error: {e}")
        session_memory[call_sid]["gpt_response_ready"] = True
        session_memory[call_sid]["gpt_text"] = "[GPT failed to respond]"

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
    print(f"🧠 Current session_memory keys: {list(session_memory.keys())}")

    # 🧠 Pull or initialize session for this call
    session = session_memory.get(call_sid, {})
    
    # 🚦 Check if greeting has already been played
    if not session.get("greeting_played"):
        session["greeting_played"] = True
        session_memory[call_sid] = session  # ✅ Important: persist the update
        vr = VoiceResponse()
        vr.redirect("/greeting")
        print("👋 First-time caller — redirecting to greeting handler.")
        return Response(content=str(vr), media_type="application/xml")

    # Where the old await used to be
    last_known_version = session_memory.get(call_sid, {}).get("transcript_version", 0)

    data = session_memory.get(call_sid, {})
    user_transcript = data.get("user_transcript")
    version = data.get("transcript_version", 0)

    # Fix: get the version we last responded to
    last_responded_version = data.get("last_responded_version", 0)

    if user_transcript and version > last_responded_version:
        gpt_input = user_transcript
        new_version = version
        # Mark this version as responded to
        session_memory[call_sid]["last_responded_version"] = new_version
        print(f"✅ Transcript ready v{new_version}: {gpt_input!r}")

    elif user_transcript:  # user_transcript exists but version not newer
        log(f"⛔ Skipped GPT input for {call_sid}: user_transcript={repr(user_transcript)}, "
            f"version={version}, last_responded={last_responded_version}")
        return Response(content="No new transcript yet", media_type="application/xml")

    else:  # truly no transcript — redirect
        vr = VoiceResponse()
        vr.redirect("/wait")
        print("⏳ No new transcript — redirecting to /wait")
        return Response(content=str(vr), media_type="application/xml")
        
    print(f"📝 GPT input candidate: \"{gpt_input}\"")
    session_memory[call_sid]["debug_gpt_input_logged_at"] = time.time()

    vr = VoiceResponse()
    vr.redirect("/2")  # ✅ First redirect
    print("👋 Redirecting to /2")
    return Response(str(vr), media_type="application/xml")

@app.post("/2")
async def post2(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid")

    # ✅ Retrieve transcript
    gpt_input = session_memory[call_sid].get("user_transcript")

    # ✅ If no transcript or unclear, just go back to WAIT2 loops
    if not gpt_input or len(gpt_input.strip()) < 4:
        # No point starting GPT here — just keep waiting for speech
        vr = VoiceResponse()
        vr.redirect("/wait2")
        print("⚠️ No valid transcript — redirecting to /wait2")
        return Response(str(vr), media_type="application/xml")

    # ✅ If GPT isn’t started yet, start it **once**
    if not session_memory[call_sid].get("get_gpt_response_started"):
        session_memory[call_sid]["get_gpt_response_started"] = True
        asyncio.create_task(get_gpt_response(call_sid))
        print("🚀 Started GPT task in background")

    vr = VoiceResponse()

    # ✅ If GPT finished, move to /3
    if session_memory[call_sid].get("gpt_response_ready"):
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

    unique_id = session_memory[call_sid].get("unique_id")
    file_path = session_memory[call_sid].get("file_path")

    if not unique_id or not file_path:
        print("❌ Missing unique_id or file_path in session_memory")
        return Response("Server error", status_code=500)
        
    if not session_memory[call_sid].get("ffmpeg_audio_fetch_started"):
        session_memory[call_sid]["ffmpeg_audio_fetch_started"] = True
        asyncio.create_task(convert_audio_ulaw(call_sid, file_path, unique_id))
        print("🚀 Started FFmpeg task in background")
        session_memory[call_sid]["11labs_audio_fetch_started"] = False
        print(f"🚩 Flag set: 11labs_audio_fetch_started = False for session {call_sid}")
        session_memory[call_sid]["11labs_audio_ready"] = False
        print(f"🚩 Flag set: 11labs_audio_ready = False for session {call_sid}")

    vr = VoiceResponse()
        
    if session_memory[call_sid].get("ffmpeg_audio_ready"):
        print("✅ FFmpeg audio is ready — redirecting to /5")
        vr.redirect("/5")
        return Response(str(vr), media_type="application/xml")  # ✅ FIX
    else:
        vr.redirect("/wait4")
        print("👋 Redirecting to /wait4")
        return Response(str(vr), media_type="application/xml")

@app.post("/5")
async def post5(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid")

    session_memory[call_sid]["ffmpeg_audio_ready"] = False
    print(f"🚩 Flag set: ffmpeg_audio_ready = False for session {call_sid}")
    
    session_memory[call_sid]["ffmpeg_audio_fetch_started"] = False
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
        current_path = get_last_audio_for_call(call_sid)
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

        # Set ai_is_speaking flag to True right before the file is played in POST
        session_memory[call_sid]["ai_is_speaking"] = True
        print(f"🚩 Flag set: ai_is_speaking = {session_memory[call_sid]['ai_is_speaking']} for session {call_sid} at {time.time()}")

        logger.info(f"🟥 [User Input] Processing complete — unblocking writes for {call_sid}")
        session_memory[call_sid]['user_response_processing'] = False
        
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
    print(f"🎙️ ElevenLabs status {elevenlabs_response.status_code}, "
          f"bytes {len(elevenlabs_response.content)}")

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
        session_memory[call_sid]["audio_duration"] = duration  # 🔒 Store for later
    except Exception as e:
        print(f"⚠️ Failed to measure audio duration: {e}")
        duration = 0.0
    
    # ✅ Only save if audio is a reasonable size (avoid silent/broken audio)
    if len(audio_bytes) > 2000:
        save_transcript(call_sid, audio_path=converted_path, gpt_response=gpt_text)
        print(f"🧠 Session updated AFTER save: {session_memory.get(call_sid)}")
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
        current_path = get_last_audio_for_call(call_sid)
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

        # Set ai_is_speaking flag to True right before the file is played in Greeting
        session_memory[call_sid]["ai_is_speaking"] = True
        print(f"🚩 Flag set: ai_is_speaking = {session_memory[call_sid]['ai_is_speaking']} for session {call_sid} at {time.time()}")

        logger.info(f"🟥 [User Input] Processing complete — unblocking writes for {call_sid}")
        session_memory[call_sid]['user_response_processing'] = False
        
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

@app.post("/wait")
async def greeting_rout(request: Request):
    print("\n📞 ── [POST] WAIT handler hit ───────────────────────────────────")
    form_data = await request.form()
    call_sid = form_data.get("CallSid") or str(uuid.uuid4())
    print(f"🆔 Call SID: {call_sid}")
    print(f"🧠 Current session_memory keys: {list(session_memory.keys())}")
    
    # Pause success Tested 9-25-25
    vr = VoiceResponse()
    vr.pause(length=1)
    print("✅ Heartbeat sent: <Pause length='1'/>")
    await asyncio.sleep(1)

    # ✅ Redirect to POST after /wait
    vr.redirect("/")
    print("📝 Returning TwiML to Twilio (with redirect).")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/wait2")
async def greeting_rout(request: Request):
    print("\n📞 ── [POST] WAIT2 handler hit ───────────────────────────────────")
    form_data = await request.form()
    call_sid = form_data.get("CallSid") or str(uuid.uuid4())
    print(f"🆔 Call SID: {call_sid}")
    print(f"🧠 Current session_memory keys: {list(session_memory.keys())}")
    
    # Pause success Tested 9-25-25
    vr = VoiceResponse()
    vr.pause(length=1)
    print("✅ Heartbeat sent: <Pause length='1'/>")
    await asyncio.sleep(1)

    # ✅ Redirect to POST after /wait
    vr.redirect("/2")
    print("📝 Returning TwiML to Twilio (with redirect).")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/wait3")
async def greeting_rout(request: Request):
    print("\n📞 ── [POST] WAIT3 handler hit ───────────────────────────────────")
    form_data = await request.form()
    call_sid = form_data.get("CallSid") or str(uuid.uuid4())
    print(f"🆔 Call SID: {call_sid}")
    print(f"🧠 Current session_memory keys: {list(session_memory.keys())}")
    
    # Pause success Tested 9-25-25
    vr = VoiceResponse()
    vr.pause(length=1)
    print("✅ Heartbeat sent: <Pause length='1'/>")
    await asyncio.sleep(1)

    # ✅ Redirect to POST after /wait
    vr.redirect("/3")
    print("📝 Returning TwiML to Twilio (with redirect).")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/wait4")
async def greeting_rout(request: Request):
    print("\n📞 ── [POST] WAIT4 handler hit ───────────────────────────────────")
    form_data = await request.form()
    call_sid = form_data.get("CallSid") or str(uuid.uuid4())
    print(f"🆔 Call SID: {call_sid}")
    print(f"🧠 Current session_memory keys: {list(session_memory.keys())}")
    
    # Pause success Tested 9-25-25
    vr = VoiceResponse()
    vr.pause(length=1)
    print("✅ Heartbeat sent: <Pause length='1'/>")
    await asyncio.sleep(1)

    # ✅ Redirect to POST after /wait
    vr.redirect("/4")
    print("📝 Returning TwiML to Twilio (with redirect).")
    return Response(content=str(vr), media_type="application/xml")

@app.websocket("/media")
async def media_stream(ws: WebSocket):
    await ws.accept()
    print("★ Twilio WebSocket connected")

    call_sid_holder = {"sid": None}
    last_input_time = {"ts": time.time()}
    last_transcript = {"text": "", "confidence": 0.5, "is_final": False}
    finished = {"done": False}

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

                if session_memory.get(sid, {}).get("close_requested"):
                    print(f"🛑 Closing Deepgram for {sid}")

                    session_memory[sid]["dg_closed_on_purpose"] = True
                    print("🟢 dg_closed_on_purpose = True (intentional close)")

                    try:
                        dg_connection.finish()
                        print("🔚 dg_connection.finish() called")
                    except Exception as e:
                        print(f"⚠️ Error closing Deepgram for {sid}: {e}")

                    return

        async def handle_dg_close():
            sid = call_sid_holder.get("sid")
            intentional = session_memory.get(sid, {}).get("dg_closed_on_purpose")

            if intentional:
                print("🟢 Deepgram closed intentionally")
            else:
                print("🚨 UNEXPECTED Deepgram close detected!")

            await ws.close()
            
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
                        
                        if is_final and sentence.strip() and confidence >= 0.6:
                            print(f"✅ Final transcript received: \"{sentence}\" (confidence: {confidence})")

                            last_input_time["ts"] = time.time()
                            last_transcript["text"] = sentence
                            last_transcript["confidence"] = confidence
                            last_transcript["is_final"] = True

                            final_transcripts.append(sentence)

                            if speech_final:
                                sid = call_sid_holder.get("sid")
                                session_memory.setdefault(sid, {})
                                session_memory[sid]["close_requested"] = True
                                print(f"🛑 Requested Deepgram close for {sid}")
                                print("🧠 speech_final received — concatenating full transcript")
                                full_transcript = " ".join(final_transcripts)
                                log(f"🧪 [DEBUG] full_transcript after join: {repr(full_transcript)}")

                                # STOP immediately if we already processed a final transcript this turn
                                if session_memory[sid].get("user_response_processing"):
                                    # ignore all future speech_final and final transcripts
                                    print(f"🚫 Ignoring transcript — already processing user response for {sid}")
                                    return

                                if not full_transcript:
                                    log(f"⚠️ Skipping save — full_transcript is empty")
                                    return  # Exit early, skipping the save logic below

                                if call_sid_holder["sid"]:
                                    sid = call_sid_holder["sid"]
                                    session_memory.setdefault(sid, {})

                                    # 🧠 Detect overwrite — compare old vs new transcript
                                    prev_transcript = session_memory.get(sid, {}).get("user_transcript")
                                    new_transcript = full_transcript.strip()

                                    if prev_transcript != new_transcript:
                                        if not new_transcript:
                                            print(f"🚨 [OVERWRITE DETECTED - EMPTY TRANSCRIPT] SID: {sid}")
                                            print(f"     🧠 Previous: {repr(prev_transcript)}")
                                            print(f"     ✏️ New:      {repr(new_transcript)}")
                                        else:
                                            print(f"🔥 [OVERWRITE WARNING] SID: {sid}")
                                            print(f"     🧠 Previous: {repr(prev_transcript)}")
                                            print(f"     ✏️ New:      {repr(new_transcript)}")
                                    else:
                                        print(f"✅ [No Overwrite] SID: {sid} — transcript unchanged")

                                    # ✅ Use full_transcript — it exists here
                                    transcript_to_write = full_transcript
                                    print(f"✍️ [DEBUG] Writing to session_memory[{sid}]['user_transcript']: \"{transcript_to_write}\"")

                                    gpt_logged_at = session_memory.get(sid, {}).get("debug_gpt_input_logged_at")
                                    if gpt_logged_at:
                                        delay = time.time() - gpt_logged_at
                                        if delay > 0:
                                            print(f"🔥 [OVERWRITE WARNING] user_transcript written {delay:.2f}s AFTER GPT input was logged")

                                    block_start_time = session_memory.get(sid, {}).get("block_start_time")
                                    print(f"🧠 Retrieved block_start_time: {block_start_time}")

                                    if time.time() > session_memory[sid]["block_start_time"] + session_memory[sid]["audio_duration"]:
                                        session_memory[sid]["ai_is_speaking"] = False
                                        log(f"🏁 [{sid}] AI finished speaking. Flag flipped OFF.")

                                    # ✅ Main save gate
                                    if (
                                        session_memory[sid].get("ai_is_speaking") is False and
                                        session_memory[sid].get("user_response_processing") is False
                                        ):
                                        # ✅ Proceed with save
                                        session_memory[sid]["user_transcript"] = full_transcript
                                        session_memory[sid]["ready"] = True
                                        session_memory[sid]["transcript_version"] = time.time()

                                        log(f"✍️ [{sid}] user_transcript saved at {time.time()}")

                                        save_transcript(sid, user_transcript=full_transcript)

                                        logger.info(f"🟩 [User Input] Processing started — blocking writes for {sid}")
                                        session_memory[sid]['user_response_processing'] = True

                                        # ✅ Clear after successful save
                                        final_transcripts.clear()
                                        last_transcript["text"] = ""
                                        last_transcript["confidence"] = 0.0
                                        last_transcript["is_final"] = False

                                    else:
                                        log(f"🚫 [{sid}] Save skipped — AI still speaking")

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

        # 👉 INSERT THIS EXACTLY HERE
        dg_connection.on(
            LiveTranscriptionEvents.Close,
            lambda: asyncio.create_task(handle_dg_close())
        )

        # -------------------------------------------------
        # 🟢 REAL Keep-Alive Loop — send SILENT MULAW audio
        # -------------------------------------------------
        SILENCE_FRAME = b"\xff" * 160  # correct mulaw silence (20ms @ 8kHz)

        dg_connection.last_media_time = time.time()  # initialize timestamp

        async def deepgram_keepalive():
            while True:
                await asyncio.sleep(0.02)  # run every 20ms

                try:
                    # If Twilio has been silent for 50ms → send silence
                    if time.time() - dg_connection.last_media_time > 0.05:
                        dg_connection.send(SILENCE_FRAME)
                        #print("📡 Sent 20ms SILENCE frame to Deepgram")

                except Exception as e:
                    print(f"⚠️ KeepAlive error sending silence: {e}")
                    break
                    
        loop.create_task(deepgram_keepalive())

        async def deepgram_text_keepalive():
            while True:
                await asyncio.sleep(5)  # Send every 5 seconds

                try:
                    dg_connection.send(json.dumps({"type": "KeepAlive"}))
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
            while True:
                try:
                    raw = await ws.receive_text()
                except WebSocketDisconnect:
                    print("✖️ Twilio WebSocket disconnected")
                    break
                except Exception as e:
                    print(f"⚠️ Unexpected error receiving message: {e}")
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

                    session_memory.setdefault(sid, {})
                    session_memory[sid]["close_requested"] = False   # ← RESET HERE ONLY

                    print(f"📞 Stream started for {sid}, close_requested=False")

                elif event == "media":
                    try:
                        payload = base64.b64decode(msg["media"]["payload"])
                        dg_connection.last_media_time = time.time()
                        dg_connection.send(payload)
                    except Exception as e:
                        print(f"⚠️ Error sending to Deepgram: {e}")

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
        try:
            await ws.close()
        except Exception as e:
            print(f"⚠️ Error closing WebSocket: {e}")
        print("✅ Connection closed")
      
