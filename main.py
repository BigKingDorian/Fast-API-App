import os, json, base64, asyncio, logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Start, Stream
from deepgram import Deepgram

log = logging.getLogger(__name__)
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
if not DEEPGRAM_API_KEY:
    log.warning("DEEPGRAM_API_KEY not found – Deepgram disabled")

app = FastAPI()

@app.post("/")
async def twilio_voice(_: Request):
    vr = VoiceResponse()
    start = Start()
    start.stream(url="wss://silent-sound-1030.fly.dev/media")
    vr.append(start)
    vr.say("Hello, this is Lotus. I'm listening.")
    vr.pause(length=60)
    return Response(str(vr), media_type="application/xml")

@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()
    log.info("★ Twilio WS connected")

    # Skip Deepgram if the key is missing so the endpoint still stays up
    if not DEEPGRAM_API_KEY:
        await ws.close()
        return

    dg = Deepgram(DEEPGRAM_API_KEY)
    live = dg.transcription.live()
    dg_conn = await live.start({
        "model": "nova-3",
        "language": "en-US",
        "encoding": "mulaw",
        "sample_rate": 8000,
        "punctuate": True,
    })

    async def receiver():
        async for msg in dg_conn:
            if (txt := msg.get("channel", {}).get("alternatives", [{}])[0].get("transcript")):
                log.info("📝 %s", txt)

    async def sender():
        async for raw in ws.iter_text():
            m = json.loads(raw)
            if m["event"] == "media":
                await dg_conn.send(base64.b64decode(m["media"]["payload"]))
            elif m["event"] == "stop":
                log.info("⏹ Stream stopped by Twilio")
                await dg_conn.finish()        # <── tell Deepgram we’re done
                break

    recv_task = asyncio.create_task(receiver())
    send_task = asyncio.create_task(sender())

    done, pending = await asyncio.wait(
        [recv_task, send_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:           # whoever finished first cancels the other
        task.cancel()

    await ws.close()
    log.info("Connection closed")

        print(f"⛔ Deepgram error: {e}")
    finally:
        if dg_connection:
            await dg_connection.finish()
        await ws.close()
        print("Connection closed")
