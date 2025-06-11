import os
import json
import base64
import asyncio
import httpx  # ✅ Required for ElevenLabs API
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Start, Stream

from dotenv import load_dotenv
load_dotenv()

# ✅ Deepgram setup
from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents

# ✅ OpenAI setup
from openai import OpenAI

# ✅ Audio conversion
from pydub import AudioSegment
import io

# ✅ ElevenLabs setup
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY")
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID")  # e.g. "JBFqnCBsd6RMkjVDRZzb"

async def text_to_speech_elevenlabs(text: str) -> bytes:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    headers = {
        "Content-Type": "application/json",
        "xi-api-key": ELEVEN_API_KEY,
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.8,
            "style": 0,
            "use_speaker_boost": True
        }
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        return response.content  # MP3 bytes

def convert_mp3_to_mulaw_8k(mp3_data: bytes) -> bytes:
    audio = AudioSegment.from_file(io.BytesIO(mp3_data), format="mp3")
    audio = audio.set_channels(1).set_frame_rate(8000).set_sample_width(1)
    out_io = io.BytesIO()
    audio.export(out_io, format="mulaw")
    return out_io.getvalue()

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not DEEPGRAM_API_KEY:
    raise RuntimeError("Missing DEEPGRAM_API_KEY in environment")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY in environment")

client = OpenAI(api_key=OPENAI_API_KEY)

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

async def print_gpt_response(sentence: str):
    response = await get_gpt_response(sentence)
    print(f"🤖 GPT: {response}")

app = FastAPI()

@app.post("/")
async def twilio_voice_webhook(_: Request):
    vr = VoiceResponse()
    start = Start()
    start.stream(
        url="wss://silent-sound-1030.fly.dev/media",
        content_type="audio/x-mulaw;rate=8000"
    )
    vr.append(start)
    vr.say("Hello, this is Lotus. I'm listening.")
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

        async def process_and_respond(text: str):
            try:
                gpt_response = await get_gpt_response(text)
                print(f"🤖 GPT: {gpt_response}")
                audio_data = await text_to_speech_elevenlabs(gpt_response)
                mulaw_audio = convert_mp3_to_mulaw_8k(audio_data)
                await ws.send_bytes(mulaw_audio)
                print("🔊 Sent converted audio to Twilio")
            except Exception as e:
                print(f"⚠️ GPT/ElevenLabs/Twilio error: {e}")

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
                            asyncio.create_task(process_and_respond(sentence))
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
