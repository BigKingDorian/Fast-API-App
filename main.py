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
    
    # ── 2. PULL LAST TRANSCRIPT (if any) ───────────────────────────────────────
    audio_path = get_last_audio_for_call(call_sid)
    if audio_path and os.path.exists(audio_path):
        print("✅ Found existing audio — skipping GPT + TTS.")
    else:
        print("⚠️ No stored audio found. Using default greeting.")
        gpt_text = "Hello, how can I help you today?"
    
        # 🔁 Do ElevenLabs TTS just once as fallback
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
        audio_bytes = elevenlabs_response.content
        ...
        save_transcript(call_sid, audio_path=converted_path)
        
    # ✅ Ensure call_sid exists in session_memory (for saving later)
    session_memory.setdefault(call_sid, {})

    # ── 3. TEXT-TO-SPEECH WITH ELEVENLABS ──────────────────────────────────────
    MAX_RETRIES = 2
    for attempt in range(MAX_RETRIES):
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
        print(f"🎙️ ElevenLabs status {elevenlabs_response.status_code}, "
            f"bytes {len(elevenlabs_response.content)}")

        audio_bytes = elevenlabs_response.content

        await asyncio.sleep(0.5)

        # ✅ Failure check with print statements
        if not audio_bytes or elevenlabs_response.status_code != 200:
            print("❌ ElevenLabs failed or returned empty audio!")
            print("🔁 GPT Text:", gpt_text)
            print("🛑 Status:", elevenlabs_response.status_code)
            print("📜 Response:", elevenlabs_response.text)
            return Response("Audio generation failed.", status_code=500)

        unique_id = uuid.uuid4().hex
        file_path = f"static/audio/response_{unique_id}.wav"

        with open(file_path, "wb") as f:
            f.write(audio_bytes)
        print(f"💾 Saved original WAV → {file_path}")

    # ✅ Save the audio path to session_memory
    session_memory.setdefault(call_sid, {})  # Ensure the dict exists
    print(f"📝 Saving audio_path for {call_sid}")
    session_memory.setdefault(call_sid, {})  # Ensure dict
    session_memory[call_sid]["audio_path"] = file_path
    log(f"🧠 Session memory updated with audio path for {call_sid}: {file_path}")
    log(f"🧠 session_memory now: {json.dumps(session_memory.get(call_sid), indent=2)}")

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
    # ✅ Only save if audio is a reasonable size (avoid silent/broken audio)
    if len(audio_bytes) > 2000:
        save_transcript(call_sid, audio_path=converted_path)
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
        print(f"🔎 Full session_memory[{call_sid}] = {json.dumps(session_memory.get(call_sid), indent=2)}")
        
        if current_path and os.path.exists(current_path):
            audio_path = current_path
            break
        await asyncio.sleep(0.3)

    if audio_path:
        ulaw_filename = os.path.basename(audio_path)
        vr.play(f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}")
        print("🔗 Final playback URL:", f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}")
        print(f"✅ Queued audio for playback: {ulaw_filename}")
    else:
        print("❌ Audio not found after retry loop")
        vr.say("Sorry, something went wrong.")

    vr.pause(length=7)
    vr.redirect("/")

    await asyncio.sleep(1)

    print("📝 Returning TwiML to Twilio (with redirect).")
    return Response(content=str(vr), media_type="application/xml")
    
@app.websocket("/media")
async def media_stream(ws: WebSocket):
    await ws.accept()
    print("★ Twilio WebSocket connected")

    call_sid_holder = {"sid": None}
    dg_connection_started = False

    # ✅ Deepgram client setup
    try:
        deepgram = DeepgramClient(DEEPGRAM_API_KEY)
        print("🧠 Deepgram client initialized")
        live_client = deepgram.listen.live
        dg_connection = live_client.v("1")
        print("✅ Deepgram v1 stream ready")
    except Exception as e:
        print(f"⛔ Deepgram connection error: {e}")
        await ws.close()
        return

    # ✅ GPT + TTS Pipeline
    async def generate_and_store_response(sid, user_text):
        try:
            print(f"💬 Sending to GPT: {user_text}")
            response = await get_gpt_response(user_text)
            print(f"🤖 GPT Response: {response}")

            # Save to session memory
            session_memory.setdefault(sid, {})
            session_memory[sid]["gpt_response"] = response

            audio_bytes = elevenlabs_tts(response)
            if not audio_bytes or len(audio_bytes) < 2000:
                print("⚠️ Skipping TTS: empty or too short")
                return

            # Save raw WAV file
            unique_id = str(uuid.uuid4())[:8]
            file_path = f"static/audio/gpt_response_{unique_id}.wav"
            with open(file_path, "wb") as f:
                f.write(audio_bytes)
            print(f"💾 Saved raw WAV to {file_path}")

            # Convert to 8kHz μ-law
            converted_path = f"static/audio/response_{unique_id}_ulaw.wav"
            subprocess.run([
                "/usr/bin/ffmpeg", "-y", "-i", file_path,
                "-ar", "8000", "-ac", "1", "-c:a", "pcm_mulaw", converted_path
            ], check=True)
            print(f"🎛️ Converted to μ-law: {converted_path}")

            # Save path to session memory
            session_memory[sid]["audio_path"] = converted_path
            print(f"🧠 Updated session_memory[{sid}] with audio_path")

        except Exception as e:
            print(f"❌ Error in GPT→TTS pipeline: {e}")

    # ✅ Transcript handler
    def on_transcript(*args, **kwargs):
        try:
            result = kwargs.get("result") or (args[0] if args else None)
            if not result:
                print("⚠️ No result object in transcript")
                return

            payload = result.to_dict()
            alt = payload["channel"]["alternatives"][0]
            sentence = alt.get("transcript", "")
            confidence = alt.get("confidence", 0.0)

            print(f"🗣️ Transcript: \"{sentence}\" (Confidence: {confidence})")

            sid = call_sid_holder.get("sid")
            if sid and sentence and confidence > 0.6:
                session_memory.setdefault(sid, {})
                session_memory[sid]["user_transcript"] = sentence
                print(f"💾 Saved transcript for {sid}: {sentence}")
                # Call GPT + TTS async
                asyncio.create_task(generate_and_store_response(sid, sentence))
            else:
                print(f"⚠️ Ignored low-confidence or empty transcript: \"{sentence}\"")

        except Exception as e:
            print(f"❌ Error in on_transcript: {e}")

    # ✅ Bind Deepgram event
    dg_connection.on(LiveTranscriptionEvents.Transcript, on_transcript)
    print("🔗 Transcript callback bound")

    # ✅ Handle incoming messages
    while True:
        try:
            raw = await ws.receive_text()
        except WebSocketDisconnect:
            print("✖️ Twilio WebSocket disconnected")
            break
        except Exception as e:
            print(f"❌ Error receiving message: {e}")
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
            print(f"📞 WebSocket start event — call_sid: {call_sid}")

        elif event == "media":
            print("📡 Media event received")
            try:
                payload = base64.b64decode(msg["media"]["payload"])

                if not dg_connection_started:
                    dg_connection.start({
                        "punctuate": True,
                        "interim_results": False,
                    })
                    dg_connection_started = True
                    print("✅ Deepgram stream officially started")

                dg_connection.send(payload)
                print(f"📦 Sent {len(payload)} bytes to Deepgram")
            except Exception as e:
                print(f"❌ Error sending to Deepgram: {e}")

        elif event == "stop":
            print("⏹️ Twilio stream stopped")
            break

    # ✅ Clean up
    try:
        dg_connection.finish()
        print("🔚 Deepgram stream finished cleanly")
    except Exception as e:
        print(f"⚠️ Error finishing Deepgram stream: {e}")
        
