import os
import json
import base64
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Start, Stream
from deepgram import Deepgram
from dotenv import load_dotenv
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
if not DEEPGRAM_API_KEY:
    raise RuntimeError("Missing DEEPGRAM_API_KEY in environment")

app = FastAPI()

@app.post("/voice")
async def twilio_voice_webhook(request: Request):
    """Handle incoming Twilio calls and return TwiML to stream audio."""
    vr = VoiceResponse()
    start = Start()
    # Use the Fly.io app's host or fallback to the deployed app name
    host = request.headers.get("Host", "silent-sound-1030.fly.dev")
    ws_url = f"wss://{host}/media"
    start.stream(url=ws_url)
    vr.append(start)
    vr.say("Hello, this is Lotus. I'm listening.")
    vr.pause(length=60)  # Allow 60 seconds of audio
    logger.info("TwiML response generated")
    return Response(content=str(vr), media_type="application/xml")

@app.websocket("/media")
async def media_stream(ws: WebSocket):
    """Handle WebSocket connection for Twilio media streaming and Deepgram transcription."""
    await ws.accept()
    logger.info("Twilio WebSocket connected")

    deepgram = Deepgram(DEEPGRAM_API_KEY)
    dg_connection = None

    try:
        logger.info("Connecting to Deepgram live transcription...")
        dg_connection = await deepgram.transcription.live({
            "model": "nova-2",  # Use supported model
            "language": "en-US",
            "encoding": "mulaw",
            "sample_rate": 8000,
            "punctuate": True,
            "smart_format": True,
            "interim_results": False
        })
        logger.info("Deepgram connection started")

        async def receiver():
            """Receive and log transcriptions from Deepgram."""
            async for msg in dg_connection:
                if "channel" in msg and msg["channel"]["alternatives"][0]["transcript"]:
                    transcript = msg["channel"]["alternatives"][0]["transcript"]
                    logger.info(f"Transcript: {transcript}")

        async def sender():
            """Receive Twilio media stream and send to Deepgram."""
            while True:
                try:
                    raw = await ws.receive_text()
                    msg = json.loads(raw)
                    event = msg.get("event")

                    if event == "start":
                        logger.info(f"Stream started (StreamSid: {msg['start'].get('streamSid')})")
                    elif event == "media":
                        payload = base64.b64decode(msg["media"]["payload"])
                        await dg_connection.send(payload)
                    elif event == "stop":
                        logger.info("Stream stopped by Twilio")
                        break

                except WebSocketDisconnect:
                    logger.info("Twilio WebSocket disconnected")
                    break
                except Exception as e:
                    logger.error(f"Error in WebSocket sender: {e}")
                    break

        await asyncio.gather(sender(), receiver())

    except Exception as e:
        logger.error(f"Deepgram error: {e}")
    finally:
        if dg_connection:
            await dg_connection.finish()
        await ws.close()
        logger.info("Connection closed")

if __name__ == "__main__":
    import uvicorn
    # Run on port 8080 for Fly.io compatibility
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
