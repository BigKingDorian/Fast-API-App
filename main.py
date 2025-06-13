import os
import json
import base64
import asyncio
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
        "https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",  # ← Replace with your voice ID
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json"
        },
        json={
            "text": response,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75
            }
        }
    )

    print("🛰️ ElevenLabs Status Code:", elevenlabs_response.status_code)
    print("🛰️ ElevenLabs Content-Type:", elevenlabs_response.headers.get("Content-Type"))
    print("🛰️ ElevenLabs Response Length:", len(elevenlabs_response.content), "bytes")
    print("🛰️ ElevenLabs Content (first 500 bytes):", elevenlabs_response.content[:500])

    # Step 3: Save audio to file
    audio_bytes = elevenlabs_response.content
    print(f"🔊 Audio file size: {len(audio_bytes)} bytes")

    audio_bytes = audio_response.content
    print(f"🎧 Got {len(audio_bytes)} audio bytes from ElevenLabs")

    # ✅ Save audio to static path for Twilio
    os.makedirs("static/audio", exist_ok=True)
    with open("static/audio/response.wav", "wb") as f:
        f.write(audio_bytes)

# ✅ Create FastAPI app and mount static audio folder
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.post("/")
async def twilio_voice_webhook(_: Request):
    # Step 1: Get GPT response
    gpt_text = await get_gpt_response("Hello, what can I help you with?")
    print(f"🤖 GPT: {gpt_text}")

    # Step 2: Send GPT response to ElevenLabs
    elevenlabs_response = requests.post(
        "https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",  # ← Replace this
        headers={
            "xi-api-key": os.getenv("ELEVENLABS_API_KEY"),
            "Content-Type": "application/json"
        },
        json={
            "text": gpt_text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75
            }
        }
    )

    # Step 3: Save audio to file
    audio_bytes = elevenlabs_response.content
    file_path = "static/audio/response.wav"
    with open(file_path, "wb") as f:
        f.write(audio_bytes)
    print(f"💾 Saved audio to {file_path}")

    # Step 4 & 5: Return TwiML with <Play> tag
    vr = VoiceResponse()

    # Stream Twilio audio to Deepgram
    start = Start()
    start.stream(
        url="wss://silent-sound-1030.fly.dev/media",
        content_type="audio/x-mulaw;rate=8000"
    )
    vr.append(start)

    # Intro speech
    vr.say("Hello, this is Lotus. I'm listening.")
    vr.pause(length=30)

    # ✅ Play saved MP3 file from server
    vr.play("https://silent-sound-1030.fly.dev/static/audio/response.wav")

    # Buffer time
    vr.pause(length=60)

    return Response(content=str(vr), media_type="application/xml")

@app.websocket("/media")
async def media_stream(ws: WebSocket):
    await ws.accept()
    print("★ Twilio WebSocket connected")

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
                            try:
                                async def gpt_and_audio_pipeline(text):
                                    response = await get_gpt_response(text)
                                    print(f"🤖 GPT: {response}")

                                    try:
                                        import requests
                                        audio_response = requests.post(
                                            "https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
                                            headers={
                                                "xi-api-key": os.getenv("ELEVENLABS_API_KEY"),
                                                "Content-Type": "application/json"
                                            },
                                            json={
                                                "text": response,
                                                "model_id": "eleven_multilingual_v2",
                                                "voice_settings": {
                                                    "stability": 0.5,
                                                    "similarity_boost": 0.75
                                                }
                                            }
                                        )
                                        audio_bytes = audio_response.content
                                        print(f"🎧 Got {len(audio_bytes)} audio bytes from ElevenLabs")

                                        with open("static/audio/response.wav", "wb") as f:
                                            f.write(audio_bytes)
                                            print("✅ Audio saved to static/audio/response.wav")
                                    except Exception as audio_e:
                                        print(f"⚠️ Error with ElevenLabs request or saving file: {audio_e}")

                                loop.create_task(gpt_and_audio_pipeline(sentence))
                            except Exception as gpt_e:
                                print(f"⚠️ GPT handler error: {gpt_e}")
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
                except json.JSONDecodeError as e:
                    print(f"⚠️ JSON decode error: {e}")
                    continue

                event = msg.get("event")

                if event == "start":
                    print("▶️ Stream started (StreamSid:", msg["start"].get("streamSid"), ")")

                elif event == "media":
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
