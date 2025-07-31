import logging
import os
import json
import base64
import asyncio
import time
import uuid
import subprocess
import requests  # ✅ Added for ElevenLabs API
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles  # ✅ Added for serving audio
from twilio.twiml.voice_response import VoiceResponse, Start, Stream

# Detect which VM / container you’re on
INSTANCE = (
    os.getenv("FLY_ALLOC_ID")      # Fly.io VM ID (present in production)
    or os.getenv("HOSTNAME")       # Docker / Kubernetes fallback
    or os.uname().nodename         # last-resort fallback
)

print(f"🆔 This app instance ID is: {INSTANCE}")

# Configure the root logger
logging.basicConfig(
    level=logging.INFO,
    format=f"[{INSTANCE}] %(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("app").info     # quick alias → use log(...)

# ✅ Load .env before any getenv calls
from dotenv import load_dotenv
load_dotenv("/root/Fast-API-App/.env")

# ✅ Deepgram setup
from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents

# ✅ OpenAI setup
from openai import OpenAI

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")  # ✅ Also needed
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")

# Simple in-memory session store
session_memory = {}

def save_transcript(call_sid, user_transcript=None, audio_path=None):
    if call_sid not in session_memory:
        session_memory[call_sid] = {}
        log(f"🆕 Initialized session_memory for call {call_sid}")

    if user_transcript:
        session_memory[call_sid]["user_transcript"] = user_transcript
        log(f"💾 User Transcript saved for {call_sid}: \"{user_transcript}\"")

    if audio_path:
        session_memory[call_sid]["audio_path"] = audio_path
        log(f"🎧 Audio path saved for {call_sid}: {audio_path}")

def get_last_transcript_for_this_call(call_sid):
    data = session_memory.get(call_sid)
    if data and "user_transcript" in data:
        log(f"📤 Retrieved transcript for {call_sid}: \"{data['user_transcript']}\"")
        return data["user_transcript"]
    else:
        log(f"⚠️ No transcript found for {call_sid} — returning default greeting.")
        return "Hello, what can I help you with?"

def get_last_audio_for_call(call_sid):
    data = session_memory.get(call_sid)

    if data and "audio_path" in data:
        log(f"🎧 Retrieved audio path for {call_sid}: {data['audio_path']}")
        return data["audio_path"]
    else:
        logging.error(f"❌ No audio path found for {call_sid} in session memory.")
        return None

if not DEEPGRAM_API_KEY:
    raise RuntimeError("Missing DEEPGRAM_API_KEY in environment")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY in environment")
if not ELEVENLABS_API_KEY:
    raise RuntimeError("Missing ELEVENLABS_API_KEY in environment")

def elevenlabs_tts(text: str) -> bytes:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
    }
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code != 200:
        print(f"⛔ ElevenLabs TTS failed: {response.status_code} - {response.text}")
        return b""
    return response.content
    
# ✅ Create the OpenAI client after loading the env
client = OpenAI(api_key=OPENAI_API_KEY)

# ✅ GPT handler function
async def get_gpt_response(user_text: str) -> str:
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a helpful AI assistant. Keep your responses clear and concise."},
                {"role": "user", "content": user_text}
            ]
        )
        return response.choices[0].message.content

    except Exception as e:
        print(f"⚠️ GPT Error: {e}")
        return "[GPT failed to respond]"

# ✅ Helper to run GPT in executor from a thread
async def print_gpt_response(sentence: str):
    response = await get_gpt_response(sentence)
    print(f"🤖 GPT: {response}")

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

# ✅ Create FastAPI app and mount static audio folder
app = FastAPI()
app.mount("/static", VerboseStaticFiles(directory="static"), name="static")

@app.post("/")
async def twilio_voice_webhook(request: Request):
    print("\n📞 ── [POST] Twilio webhook hit ───────────────────────────────────")
    form_data = await request.form()
    print(f"FORM DATA: {form_data}")

    call_sid = form_data.get("CallSid") or str(uuid.uuid4())
    print(f"🆔 Call SID: {call_sid}")
    print(f"🧠 Current session_memory keys: {list(session_memory.keys())}")

    session_memory.setdefault(call_sid, {})
    
    # ── 1. Use GPT response if available ───────────────────────────────────────
    gpt_text = session_memory[call_sid].get("gpt_response")
    if not gpt_text:
        print(f"⚠️ No GPT response found for {call_sid}, using fallback.")
        gpt_text = "Hello, how can I help you today?"

    print(f"🤖 GPT Text to TTS: \"{gpt_text}\"")

    # ── 2. TEXT-TO-SPEECH WITH ELEVENLABS ──────────────────────────────────────
    MAX_RETRIES = 2
    for attempt in range(MAX_RETRIES):
        elevenlabs_response = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "text": gpt_text,
                "model_id": "eleven_flash_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
            }
        )

        print(f"🎙️ ElevenLabs status {elevenlabs_response.status_code}, bytes {len(elevenlabs_response.content)}")
        audio_bytes = elevenlabs_response.content

        await asyncio.sleep(0.5)

        if not audio_bytes or elevenlabs_response.status_code != 200:
            print("❌ ElevenLabs failed or returned empty audio!")
            print("🛑 Status:", elevenlabs_response.status_code)
            print("📜 Response:", elevenlabs_response.text)
            return Response("Audio generation failed.", status_code=500)

        unique_id = uuid.uuid4().hex
        file_path = f"static/audio/response_{unique_id}.wav"
        with open(file_path, "wb") as f:
            f.write(audio_bytes)
        print(f"💾 Saved original WAV → {file_path}")
        break

    # ── 3. CONVERT TO μ-LAW 8 kHz ──────────────────────────────────────────────
    converted_path = f"static/audio/response_{unique_id}_ulaw.wav"
    try:
        subprocess.run([
            "/usr/bin/ffmpeg", "-y", "-i", file_path,
            "-ar", "8000", "-ac", "1", "-c:a", "pcm_mulaw", converted_path
        ], check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ FFmpeg failed: {e}")
        return Response("Audio conversion failed", status_code=500)

    for i in range(40):
        if os.path.isfile(converted_path):
            print(f"✅ Found converted file after {i * 0.1:.1f}s")
            break
        await asyncio.sleep(0.1)
    else:
        print("❌ Converted file never appeared — aborting")
        return Response("Converted audio not available", status_code=500)

    print(f"🎛️ Converted WAV (8 kHz μ-law) → {converted_path}")

    # ✅ Save the audio path to session_memory
    if len(audio_bytes) > 2000:
        session_memory[call_sid]["audio_path"] = converted_path
        print(f"🧠 Session memory updated with audio path for {call_sid}")
    else:
        print("⚠️ Skipping transcript/audio save due to likely blank response.")

    # ── 4. BUILD TWIML ─────────────────────────────────────────────────────────
    vr = VoiceResponse()

    # Start Deepgram stream
    start = Start()
    start.stream(
        url="wss://silent-sound-1030.fly.dev/media",
        content_type="audio/x-mulaw;rate=8000"
    )
    vr.append(start)
    print("📡 Starting Deepgram stream")

    # Try to retrieve audio path
    for _ in range(10):
        current_path = session_memory[call_sid].get("audio_path")
        print(f"🔁 Checking session memory for {call_sid} → {current_path}")
        if current_path and os.path.exists(current_path):
            break
        await asyncio.sleep(0.3)
    else:
        print("❌ Audio path not found in session memory")
        vr.say("Sorry, something went wrong.")
        return Response(str(vr), media_type="application/xml")

    ulaw_filename = os.path.basename(current_path)
    vr.play(f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}")
    print(f"✅ Queued audio for playback: {ulaw_filename}")

    vr.pause(length=7)
    vr.redirect("/")
    print("📝 Returning TwiML to Twilio (with redirect).")
    return Response(content=str(vr), media_type="application/xml")

@app.websocket("/media")
async def media_stream(ws: WebSocket):
    await ws.accept()
    print("★ Twilio WebSocket connected")
    call_sid_holder = {"sid": None}
    dg_connection_started = False
    # ✅ Initialize Deepgram
    try:
        deepgram = DeepgramClient(DEEPGRAM_API_KEY)
        print("🧠 Deepgram client initialized")
        live_client = deepgram.listen.live
        dg_connection = live_client.v("1")
        print("✅ Deepgram v1 stream ready")
    except Exception as e:
        print(f"⛔ Deepgram init error: {e}")
        await ws.close()
        return
    # ✅ GPT + TTS Pipeline
    async def generate_and_store_response(sid, user_text):
        try:
            print(f"💬 GPT Input: \"{user_text}\"")
            gpt_response = await get_gpt_response(user_text)
            print(f"🤖 GPT Response: \"{gpt_response}\"")
            session_memory.setdefault(sid, {})
            session_memory[sid]["gpt_response"] = gpt_response
            audio_bytes = elevenlabs_tts(gpt_response)
            if not audio_bytes or len(audio_bytes) < 2000:
                print("⚠️ ElevenLabs audio too short or empty")
                return
            # Save raw WAV
            unique_id = str(uuid.uuid4())[:8]
            raw_path = f"static/audio/gpt_response_{unique_id}.wav"
            with open(raw_path, "wb") as f:
                f.write(audio_bytes)
            print(f"💾 Saved WAV: {raw_path}")
            # Convert to 8kHz μ-law
            converted_path = f"static/audio/response_{unique_id}_ulaw.wav"
            subprocess.run([
                "/usr/bin/ffmpeg", "-y", "-i", raw_path,
                "-ar", "8000", "-ac", "1", "-c:a", "pcm_mulaw", converted_path
            ], check=True)
            print(f"🎛️ Converted μ-law file: {converted_path}")
            session_memory[sid]["audio_path"] = converted_path
            print(f"🧠 session_memory[{sid}]['audio_path'] = {converted_path}")
        except Exception as e:
            print(f"❌ GPT→TTS pipeline error: {e}")
    # ✅ Transcript Callback
    def on_transcript(*args, **kwargs):
        try:
            result = kwargs.get("result") or (args[0] if args else None)
            if not result or not hasattr(result, "to_dict"):
                print("⚠️ No result object or missing .to_dict()")
                return
            payload = result.to_dict()
            alt = payload["channel"]["alternatives"][0]
            sentence = alt.get("transcript", "")
            confidence = alt.get("confidence", 0.0)
            print(f"🗣️ Transcript: \"{sentence}\" (Confidence: {confidence:.2f})")
            sid = call_sid_holder.get("sid")
            if sid and sentence and confidence > 0.6:
                session_memory.setdefault(sid, {})
                session_memory[sid]["user_transcript"] = sentence
                print(f"💾 Stored transcript for {sid}: \"{sentence}\"")
                asyncio.create_task(generate_and_store_response(sid, sentence))
            else:
                print(f"⚠️ Ignored low-confidence or empty transcript")
        except Exception as e:
            print(f"❌ Error in on_transcript: {e}")
    # ✅ Register callback
    dg_connection.on(LiveTranscriptionEvents.Transcript, on_transcript)
    print("🔗 Bound transcript callback")
    # ✅ Receive media from Twilio
    while True:
        try:
            raw = await ws.receive_text()
        except WebSocketDisconnect:
            print("✖️ WebSocket disconnected by client")
            break
        except Exception as e:
            print(f"❌ WebSocket receive error: {e}")
            break
        try:
            msg = json.loads(raw)
            event = msg.get("event")
        except json.JSONDecodeError as e:
            print(f"⚠️ JSON decode error: {e}")
            continue
        if event == "start":
            call_sid = msg.get("start", {}).get("callSid")
            call_sid_holder["sid"] = call_sid
            print(f"📞 'start' event — Call SID: {call_sid}")
        elif event == "media":
            try:
                payload = base64.b64decode(msg["media"]["payload"])
                if not dg_connection_started:
                    dg_connection.start({
                        "punctuate": True,
                        "interim_results": False
                    })
                    dg_connection_started = True
                    print("✅ Deepgram stream officially started")
                dg_connection.send(payload)
                print(f"📦 Media sent: {len(payload)} bytes")
            except Exception as e:
                print(f"❌ Media decode/send error: {e}")
        elif event == "stop":
            print("⏹️ 'stop' event received — ending stream")
            break
    # ✅ Cleanup
    try:
        dg_connection.finish()
        print("🔚 Deepgram stream finished")
    except Exception as e:
        print(f"⚠️ Deepgram finish error: {e}")
        
