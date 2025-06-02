import os, json, base64, asyncio, logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Start
from deepgram import Deepgram
from dotenv import load_dotenv
load_dotenv()
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
log = logging.getLogger("lotus")
logging.basicConfig(level=logging.INFO)
app = FastAPI()
@app.post("/")
async def twilio_voice(_: Request):
    vr = VoiceResponse()
    start = Start()
    start.stream(url="wss://silent-sound-1030.fly.dev/media")
    vr.append(start)
    vr.say("Hello, this is Lotus. I'm listening.")
    vr.pause(length=60)     # keep call alive
    return Response(str(vr), media_type="application/xml")
@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()
    log.info("★ Twilio WS connected")
    if not DEEPGRAM_API_KEY:
        log.warning("No DEEPGRAM_API_KEY set – closing socket")
        await ws.close()
        return
    dg = Deepgram(DEEPGRAM_API_KEY)
    try:
        live = dg.transcription.live()
        dg_conn = await live.start(
            {
                "model": "nova-3",
                "language": "en-US",
                "encoding": "mulaw",
                "sample_rate": 8000,
                "punctuate": True,
            }
        )
        log.info("✅ Deepgram live connection ready")
        async def sender():
            async for raw in ws.iter_text():
                msg = json.loads(raw)
                event = msg.get("event")
                if event == "media":
                    await dg_conn.send(base64.b64decode(msg["media"]["payload"]))
                elif event == "stop":
                    log.info("⏹ Twilio sent <Stop>")
                    await dg_conn.finish()
                    break
        async def receiver():
            async for result in dg_conn:
                text = (
                    result.get("channel", {})
                    .get("alternatives", [{}])[0]
                    .get("transcript")
                )
                if text:
                    log.info("📝 %s", text)
        # run both tasks until one finishes
        done, pending = await asyncio.wait(
            {asyncio.create_task(sender()), asyncio.create_task(receiver())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    except WebSocketDisconnect:
        log.info("✖️  Client hung up")
    except Exception as e:                           # <-- yes, try/except still here
        log.error("⛔ Deepgram error: %s", e)
    finally:
        if "dg_conn" in locals():
            try:
                await dg_conn.finish()
            except Exception:
                pass
        await ws.close()
        log.info("Connection closed")


        print(f"⛔ Deepgram error: {e}")
    finally:
        if dg_connection:
            await dg_connection.finish()
        await ws.close()
        print("Connection closed")
