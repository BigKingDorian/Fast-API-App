"""
main.py  – FastAPI + Twilio Media Streams -> Deepgram live transcription
-----------------------------------------------------------------------
• POST  /            → returns TwiML that starts a one-way media stream
• WS    /media       → receives audio from Twilio, pipes it to Deepgram,
                       and prints every transcript line to the server log
-----------------------------------------------------------------------
Run locally:   uvicorn main:app --reload
Run on Fly.io: uvicorn main:app --host 0.0.0.0 --port 8080
"""

import os
import json
import base64
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Start, Stream, Pause
from deepgram import Deepgram                    # pip install deepgram-sdk
from dotenv import load_dotenv

# ── Load environment ──────────────────────────────────────────────────────────
load_dotenv()
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
if not DEEPGRAM_API_KEY:
    raise RuntimeError("Missing DEEPGRAM_API_KEY in environment")

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI()


# ── 1) Voice webhook: returns valid TwiML and keeps the call open ─────────────
@app.post("/")
async def twilio_voice_webhook(_: Request) -> Response:
    """
    Twilio hits this endpoint when the call arrives.
    We start a <Start><Stream> (unidirectional media fork) and then
    pause for 60 s so the call stays alive while we stream.
    """
    vr = VoiceResponse()

    # <Start><Stream>
    start = Start()
    start.stream(url="wss://silent-sound-1030.fly.dev/media")
    vr.append(start)

    # Greet caller (optional) then keep the call open
    vr.say("Hello, this is Lotus. I'm listening.")
    vr.pause(length=60)                       # 60-second silence placeholder

    # Return XML TwiML
    return Response(content=str(vr), media_type="application/xml")


# ── 2) /media WebSocket: Twilio -> Deepgram pipe ──────────────────────────────
@app.websocket("/media")
async def media_stream(ws: WebSocket) -> None:
    await ws.accept()
    print("★ Twilio WebSocket connected")

    # Connect to Deepgram real-time transcription
    dg = Deepgram(DEEPGRAM_API_KEY)
    try:
        dg_conn = await dg.transcription.live(
            {
                "encoding": "mulaw",
                "sample_rate": 8000,
                "language": "en-US",
                "punctuate": True,
                "interim_results": False,
            }
        )
    except Exception as e:
        print(f"⛔ Deepgram connection error: {e}")
        await ws.close()
        return

    # Print every transcript line Deepgram returns
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

    # Background task to drain Deepgram messages (errors, etc.)
    async def _dg_drain():
        try:
            async for _ in dg_conn.receiver():
                pass
        except Exception as e:
            print(f"⛔ Deepgram recv error: {e}")

    asyncio.create_task(_dg_drain())

    # Main loop: read Twilio JSON frames, send audio to Deepgram
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
                await dg_conn.send(audio_bytes)      # forward to Deepgram

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
