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

def save_transcript(call_sid, transcript, audio_path=None):
    session_memory[call_sid] = {
        "transcript": transcript,
        "audio_path": audio_path
    }

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

    print("🧪 ElevenLabs status:", audio_response.status_code)
    print("🧪 ElevenLabs content type:", audio_response.headers.get("Content-Type")) 
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
        
# ✅ Create FastAPI app and mount static audio folder
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.post("/")
async def twilio_voice_webhook(request: Request):
    # ✅ Get the real CallSid from the form data (Twilio sends it this way)
    form_data = await request.form()
    call_sid = form_data.get("CallSid") or str(uuid.uuid4())
    
    print(f"📞 [POST] Incoming Call SID: {call_sid}")
    
    # ✅ Use per-call session memory
    gpt_input = get_last_transcript_for_this_call(call_sid)
    
    # ✅ Get GPT response
    gpt_text = await get_gpt_response(gpt_input)
    print(f"🤖 GPT: {gpt_text}")

    elevenlabs_response = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
        headers={
            "xi-api-key": os.getenv("ELEVENLABS_API_KEY"),
            "Content-Type": "application/json"
        },
        json={
            "text": gpt_text,
            "model_id": "eleven_flash_v2_5",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75
            }
        }
    )

    audio_bytes = elevenlabs_response.content

    unique_id = uuid.uuid4().hex
    filename = f"response_{unique_id}.wav"
    file_path = f"static/audio/{filename}"

    with open(file_path, "wb") as f:
        f.write(audio_bytes)

    print(f"💾 Saved audio to {file_path}")

    # ✅ Convert to 8kHz μ-law using ffmpeg
    converted_path = f"static/audio/response_{unique_id}_ulaw.wav"
    subprocess.run([
        "/usr/bin/ffmpeg",
        "-y",
        "-i", file_path,
        "-ar", "8000",
        "-ac", "1",
        "-c:a", "pcm_mulaw",
    converted_path
], check=True)
    print(f"🎛️ Converted audio saved at: {converted_path}")
    save_transcript(call_sid, gpt_text, converted_path)
    print(f"✅ [POST] Saved transcript for: {call_sid} → {converted_path}")
    
    await asyncio.sleep(1)  # Let file be available
    
    # ✅ Return TwiML
    vr = VoiceResponse()
    
    # ✅ Start Deepgram stream FIRST
    start = Start()
    start.stream(
        url="wss://silent-sound-1030.fly.dev/media",
        content_type="audio/x-mulaw;rate=8000"
    )
    vr.append(start)
    
    print("🧠 session_memory snapshot:")
    print(json.dumps(session_memory, indent=2))
    
    audio_path = None
    
    # 🕵️ Print full session memory for debugging
    print("📂 Full session_memory keys:", list(session_memory.keys()))
    print("📂 Full session_memory dump:", json.dumps(session_memory, indent=2))
    
    # ⏳ Retry up to 10 times, waiting for WebSocket to generate the audio
    for _ in range(10):
        current_path = get_last_audio_for_call(call_sid)
        print(f"🔁 Checking session memory for {call_sid} → {current_path}")
        if current_path and os.path.exists(current_path):
            audio_path = current_path
            break
        await asyncio.sleep(0.3)
        
    if audio_path:
        ulaw_filename = os.path.basename(audio_path)
        print(f"✅ Playing audio: {ulaw_filename}")
        vr.play(f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}")
    else:
        print("❌ Audio not found after retry loop")
        vr.say("Sorry, something went wrong.")
        
    # ✅ Add pause after audio to allow caller to respond
    vr.pause(length=10)

    # Supposed to complete loop in Twiml
    vr.hangup()
    
    # ✅ Return TwiML
    return Response(content=str(vr), media_type="application/xml")
    
@app.websocket("/media")
async def media_stream(ws: WebSocket):
    await ws.accept()
    print("★ Twilio WebSocket connected")

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
        dg_connection.start(options)
        print("✅ Deepgram connection started")

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
                    print("📩 Incoming message:", msg.get("event"))
                except json.JSONDecodeError as e:
                    print(f"⚠️ JSON decode error: {e}")
                    continue

                event = msg.get("event")

                if event == "start":
                    print("▶️ Stream started (StreamSid:", msg["start"].get("streamSid"), ")")

                    # Debug print to inspect what Twilio actually sent
                    print("🧾 Twilio start event data:", json.dumps(msg["start"], indent=2))

                    # Try all possible keys Twilio might send
                    sid = (
                        msg["start"].get("callSid") or
                        msg["start"].get("CallSid") or
                        msg["start"].get("callerSid") or
                        msg["start"].get("CallerSid")
                    )

                    call_sid_holder["sid"] = sid
                    print(f"📞 [WebSocket] call_sid_holder['sid']: {call_sid_holder['sid']}")

                elif event == "media":
                    print("📡 Media event received")
                    try:
                        payload = base64.b64decode(msg["media"]["payload"])
                        dg_connection.send(payload)
                        print(f"📦 Sent {len(payload)} bytes to Deepgram (event: media)")
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

