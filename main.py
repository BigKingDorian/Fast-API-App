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

def save_transcript(call_sid, transcript=None, audio_path=None):
    if call_sid not in session_memory:
        session_memory[call_sid] = {}
    if transcript:
        session_memory[call_sid]["transcript"] = transcript
    if audio_path:
        session_memory[call_sid]["audio_path"] = audio_path
        
def get_last_transcript_for_this_call(call_sid):
    data = session_memory.get(call_sid)
    return data["transcript"] if data else "Hello, what can I help you with?"

def get_last_audio_for_call(call_sid):
    data = session_memory.get(call_sid)
    return data["audio_path"] if data and "audio_path" in data else None

if not DEEPGRAM_API_KEY:
    raise RuntimeError("Missing DEEPGRAM_API_KEY in environment")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY in environment")
if not ELEVENLABS_API_KEY:
    raise RuntimeError("Missing ELEVENLABS_API_KEY in environment")

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

    # ✅ Send GPT response to ElevenLabs
    audio_response = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",  # ✅ Fixed: use f-string
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json"
        },
        json={
            "text": response,
            "model_id": "eleven_flash_v2_5",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75
            }
        }
    )

    print("🛰️ ElevenLabs Status Code:", audio_response.status_code)
    print("🛰️ ElevenLabs Content-Type:", audio_response.headers.get("Content-Type"))
    print("🛰️ ElevenLabs Response Length:", len(audio_response.content), "bytes")
    print("🛰️ ElevenLabs Content (first 500 bytes):", audio_response.content[:500])
    
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

        abs_path = self.full_path(path)
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
    call_sid = form_data.get("CallSid") or str(uuid.uuid4())
    print(f"🆔 Call SID: {call_sid}")

    # ── 2. PULL LAST TRANSCRIPT (if any) ───────────────────────────────────────
    gpt_input = get_last_transcript_for_this_call(call_sid)
    print(f"🗄️ Session snapshot BEFORE GPT: {session_memory.get(call_sid)}")
    print(f"📝 GPT input candidate: \"{gpt_input}\"")

    fallback_phrases = {
        "", "hello", "hi",
        "hello, what can i help you with?",
        "[gpt failed to respond]",
    }
    if not gpt_input or gpt_input.strip().lower() in fallback_phrases:
        print("🚫 No real transcript yet ➜ using default greeting.")
        gpt_text = "Hello, how can I help you today?"
    else:
        gpt_text = await get_gpt_response(gpt_input)
        print(f"✅ GPT response: \"{gpt_text}\"")

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
    print(f"🎙️ ElevenLabs status {elevenlabs_response.status_code}, "
          f"bytes {len(elevenlabs_response.content)}")

    audio_bytes = elevenlabs_response.content
    unique_id = uuid.uuid4().hex
    file_path = f"static/audio/response_{unique_id}.wav"

    with open(file_path, "wb") as f:
        f.write(audio_bytes)
    print(f"💾 Saved original WAV → {file_path}")

    # ── 4. CONVERT TO μ-LAW 8 kHz ──────────────────────────────────────────────
    converted_path = f"static/audio/response_{unique_id}_ulaw.wav"
    subprocess.run([
        "/usr/bin/ffmpeg", "-y", "-i", file_path,
        "-ar", "8000", "-ac", "1", "-c:a", "pcm_mulaw", converted_path
    ], check=True)
    print(f"🎛️ Converted WAV (8 kHz μ-law) → {converted_path}")

    log("✅ Audio file saved at %s", converted_path)          # ← NEW tagged line

    save_transcript(call_sid, gpt_text, converted_path)
    print(f"🧠 Session updated AFTER save: {session_memory.get(call_sid)}")

    # ✅ Small delay for file availability on disk
    await asyncio.sleep(1)

    # ── 5. BUILD TWIML ─────────────────────────────────────────────────────────
    vr = VoiceResponse()

    # Start Deepgram stream
    start = Start()
    start.stream(
        url="wss://silent-sound-1030.fly.dev/media",
        content_type="audio/x-mulaw;rate=8000"
    )
    vr.append(start)

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
        vr.play(f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}")
        print(f"✅ Queued audio for playback: {ulaw_filename}")
    else:
        print("❌ Audio not found after retry loop")
        vr.say("Sorry, something went wrong.")
        
    vr.pause(length=10)
    # ✅ Replace hangup with redirect back to self
    vr.redirect("/")
    print("📝 Returning TwiML to Twilio (with redirect).")
    return Response(content=str(vr), media_type="application/xml")
    
@app.websocket("/media")
async def media_stream(ws: WebSocket):
    await ws.accept()
    print("★ Twilio WebSocket connected")

    async def sender():
        dg_connection_started = False
        try:
            while: True
            raw = await ws.receive_text()

        except Exception as e:
              log("❌ sender loop crashed: %s", e, exc_info=True)
            await ws.close(code=1011)
    await sender()           

    call_sid_holder = {"sid": None}
    
    loop = asyncio.get_running_loop()
    deepgram = DeepgramClient(DEEPGRAM_API_KEY)
    dg_connection = None

    try:
        print("⚙️ Connecting to Deepgram live transcription...")

        try:
            live_client = deepgram.listen.live
            dg_connection = await asyncio.to_thread(live_client.v, "1")
        except Exception as e:
            print(f"⛔ Failed to create Deepgram connection: {e}")
            await ws.close()
            return

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

                    try:
                        sentence = payload["channel"]["alternatives"][0]["transcript"]
                        if sentence:
                            print(f"📝 {sentence}")
                            if call_sid_holder["sid"]:
                                save_transcript(call_sid_holder["sid"], sentence)

                            async def gpt_and_audio_pipeline(text):
                                response = await get_gpt_response(text)
                                print(f"🤖 GPT: {response}")

                                try:
                                    audio_response = requests.post(
                                        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
                                        headers={
                                            "xi-api-key": os.getenv("ELEVENLABS_API_KEY"),
                                            "Content-Type": "application/json"
                                        },
                                        json={
                                            "text": response,
                                            "model_id": "eleven_flash_v2_5",
                                            "voice_settings": {
                                                "stability": 0.5,
                                                "similarity_boost": 0.75
                                            }
                                        }
                                    )
                                    audio_bytes = audio_response.content
                                    print(f"🎧 Got {len(audio_bytes)} audio bytes from ElevenLabs")

                                    unique_id = uuid.uuid4().hex
                                    filename = f"response_{unique_id}.wav"
                                    file_path = f"static/audio/{filename}"
                                    with open(file_path, "wb") as f:
                                        f.write(audio_bytes)
                                        print(f"✅ Audio saved to {file_path}")

                                    converted_path = f"static/audio/{filename.replace('.wav', '_ulaw.wav')}"
                                    subprocess.run([
                                        "/usr/bin/ffmpeg",
                                        "-y",
                                        "-i", file_path,
                                        "-ar", "8000",
                                        "-ac", "1",
                                        "-c:a", "pcm_mulaw",
                                        converted_path
                                    ], check=True)
                                    
                                    print(f"🧠 File exists immediately after conversion: {os.path.exists(converted_path)}")

                                    print(f"🎛️ Converted audio saved at: {converted_path}")
                                    save_transcript(call_sid_holder["sid"], sentence, converted_path)
                                    print(f"✅ [WS] Saved transcript for: {call_sid_holder['sid']} → {converted_path}")
         
                                except Exception as audio_e:
                                    print(f"⚠️ Error with ElevenLabs request or saving file: {audio_e}")

                            loop.create_task(gpt_and_audio_pipeline(sentence))

                    except Exception as inner_e:
                        print(f"⚠️ Could not extract transcript sentence: {inner_e}")
                else:
                    print("🔍 Available attributes:", dir(result))
                    print("⚠️ This object cannot be serialized directly. Trying .__dict__...")
                    print(result.__dict__)

            except Exception as e:
                print(f"⚠️ Error handling transcript: {e}")

        dg_connection.on(LiveTranscriptionEvents.Transcript, on_transcript)

        options = LiveOptions(
            model="nova-3",
            language="en-US",
            encoding="mulaw",
            sample_rate=8000,
            punctuate=True,
        )
        print("✏️ LiveOptions being sent:", options.__dict__)

               # -------------------------------------------------
        # 3.  SENDER LOOP  (Twilio → Deepgram passthrough)
        # -------------------------------------------------
        async def sender():
            dg_connection_started = False          # NEW flag

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
                    print("📩 Incoming message:", msg.get("event"))
                except json.JSONDecodeError as e:
                    print(f"⚠️ JSON decode error: {e}")
                    continue

                event = msg.get("event")

                if event == "start":
                    ...
                    # (unchanged ‘start’ handling code)
                    ...

                elif event == "media":
                    print("📡 Media event received")
                    try:
                        payload = base64.b64decode(msg["media"]["payload"])

                        # ---------- LAZY-START ----------
                        if not dg_connection_started:
                            dg_connection.start(options)
                            dg_connection_started = True
                            print("✅ Deepgram stream officially started "
                                  "after receiving media.")
                        # ---------------------------------

                        dg_connection.send(payload)
                        print(f"📦 Sent {len(payload)} bytes to Deepgram "
                              "(event: media)")
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
