import os
import json
import base64
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Start, Stream
from deepgram.sdk import DeepgramClient, LiveTranscriptionEvents, LiveOptions
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

    dg_client = DeepgramClient(DEEPGRAM_API_KEY)
    dg_connection = None

    try:
        options = LiveOptions(
            language="en-US",
            encoding="mulaw",
            sample_rate=8000,
            punctuate=True
        )

        dg_connection = dg_client.listen.live(options)

        async def on_transcript(data, *_):
            transcript = data.get('channel', {}).get('alternatives', [{}])[0].get('transcript', '').strip()
            if transcript:
                print(f"📝 {transcript}")

        dg_connection.on(LiveTranscriptionEvents.Transcript, on_transcript)
        await dg_connection.start()

    except Exception as e:
        print(f"⛔ Deepgram connection error: {e}")
        await ws.close()
        return

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
                print("▶️ Stream started (StreamSid:", msg["start"].get("streamSid"), ")")

            elif event == "media":
                payload = base64.b64decode(msg["media"]["payload"])
                await dg_connection.send(payload)

            elif event == "stop":
                print("⏹ Stream stopped by Twilio")
                break

    except Exception as e:
        print(f"⛔ WS loop error: {e}")

    finally:
        if dg_connection:
            await dg_connection.finish()
        await ws.close()
        print("★ Connection closed")
