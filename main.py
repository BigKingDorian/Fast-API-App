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

def save_transcript(call_sid, transcript):
    session_memory[call_sid] = transcript

def get_last_transcript_for_this_call(call_sid):
    return session_memory.get(call_sid, "Hello, what can I help you with?")

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
        response = client.responses.create(
            model="gpt-4o",
            instructions="You are a helpful assistant who responds clearly and concisely.",
            input=user_text
        )
        return response.output_text
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
    call_sid = request.headers.get("X-Twilio-CallSid") or "default"
    gpt_input = get_last_transcript_for_this_call(call_sid)
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
    
    # ✅ Then play the AI-generated audio
    ulaw_filename = f"response_{unique_id}_ulaw.wav"
    vr.play(f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}")
    
    # ✅ Add pause after audio to allow caller to respond
    vr.pause(length=10)

    # Supposed to complete loop in Twiml
    vr.redirect("/", method="POST")
    
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
                    call_sid_holder["sid"] = msg["start"].get("callerSid") or msg["start"].get("CallSid")

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

