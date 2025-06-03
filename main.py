import os
import json
import base64
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Start, Stream
from deepgram import Deepgram
from dotenv import load_dotenv

load_dotenv()
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
if not DEEPGRAM_API_KEY:
    raise RuntimeError("Missing DEEPGRAM_API_KEY in environment")

app = FastAPI()

@app.post("/")
async def twilio_voice_webhook(_: Request):
    vr = VoiceResponse()
    start = Start()
    start.stream(url="wss://silent-sound-1030.fly.dev/media")
    vr.append(start)
    vr.say("Hello, this is Lotus. I'm listening.")
    vr.pause(length=60)
    return Response(content=str(vr), media_type="application/xml")

@app.websocket("/media")
async def media_stream(ws: WebSocket):
    await ws.accept()
    print("★ Twilio WebSocket connected")

    deepgram = Deepgram(DEEPGRAM_API_KEY)
    dg_connection = None

    try:
        print("⚙️ Connecting to Deepgram live transcription...")
        live = await deepgram.transcription.live()  # ← fixed
        dg_connection = await live.start(
            options={
                "model": "nova-3",
                "language": "en-US",
                "encoding": "mulaw",
                "sample_rate": 8000,
                "punctuate": True,
            }
        )
        print("✅ Deepgram connection started")

        async def receiver():
            async for msg in dg_connection:
                if "channel" in msg:
                    transcript = msg["channel"]["alternatives"][0]["transcript"]
                    if transcript:
                        print(f"📝 {transcript}")

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
                        await dg_connection.send(payload)
                        print(f"📦 Sent {len(payload)} bytes to Deepgram")
                    except Exception as e:
                        print(f"⚠️ Error sending to Deepgram: {e}")

                elif event == "stop":
                    print("⏹ Stream stopped by Twilio")
                    break

        await asyncio.gather(sender(), receiver())

    except Exception as e:
        print(f"⛔ Deepgram error: {e}")
    finally:
        if dg_connection:
            try:
                await dg_connection.finish()
            except Exception as e:
                print(f"⚠️ Error closing Deepgram connection: {e}")
        try:
            await ws.close()
        except Exception as e:
            print(f"⚠️ Error closing WebSocket: {e}")
        print("✅ Connection closed")
