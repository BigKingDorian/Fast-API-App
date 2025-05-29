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
async def twilio_voice_webhook(_: Request) -> Response:
    vr = VoiceResponse()
    start = Start()
    start.stream(url="wss://silent-sound-1030.fly.dev/media")
    vr.append(start)
    vr.say("Hello, this is Lotus. I'm listening.")
    vr.pause(length=60)
    return Response(content=str(vr), media_type="application/xml")

@app.websocket("/media")
async def media_stream(ws: WebSocket) -> None:
    await ws.accept()
    print("★ Twilio WebSocket connected")

    dg = Deepgram(DEEPGRAM_API_KEY)
    try:
        dg_conn = await dg.transcription.live({
            "encoding": "mulaw",
            "sample_rate": 8000,
            "language": "en-US",
            "punctuate": True,
            "interim_results": False,
        })
    except Exception as e:
        print(f"⛔ Deepgram connection error: {e}")
        await ws.close()
        return

    def _on_transcript(data):
        text = (
            data.get("channel", {})
            .get("alternatives", [{}])[0]
            .get("transcript", "")
            .strip()
        )
        if text:
            print(f"📝 {text}")

    dg_conn.register_handler("transcript_received", _on_transcript)

    async def _dg_drain():
        try:
            async for _ in dg_conn.receiver():
                pass
        except Exception as e:
            print(f"⛔ Deepgram recv error: {e}")

    asyncio.create_task(_dg_drain())

    try:
        while True:
            try:
                raw = await ws.receive_text()
            except WebSocketDisconnect:
                print("✖️  Twilio WebSocket disconnected")
                break

            msg = json.loads(raw)
            event = msg.get("event")

            if event == "start":
                print("▶️  Stream started (StreamSid:", msg['start'].get('streamSid'), ")")

            elif event == "media":
                b64 = msg["media"]["payload"]
                audio_bytes = base64.b64decode(b64)
                await dg_conn.send(audio_bytes)

            elif event == "stop":
                print("⏹  Stream stopped by Twilio")
                break

    except Exception as e:
        print(f"⛔ WS loop error: {e}")

    finally:
        try:
            await dg_conn.finish()
        except Exception:
            pass
        await ws.close()
        print("★ Connection closed")
