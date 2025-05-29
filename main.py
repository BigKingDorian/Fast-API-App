import base64
import os
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from deepgram import Deepgram
from dotenv import load_dotenv

load_dotenv()

# Load Deepgram API key
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
dg_client = Deepgram(DEEPGRAM_API_KEY)

app = FastAPI()

# ✅ This route sends TwiML to Twilio to start MediaStream
@app.post("/twiml")
async def twiml_response():
    twiml = """
    <Response>
        <Start>
            <Stream url="wss://silent-sound-1030.fly.dev/media" />
        </Start>
        <Say>Start talking. I'm listening.</Say>
        <Pause length="60"/>
    </Response>
    """
    return Response(content=twiml.strip(), media_type="application/xml")

# ✅ This route receives audio stream from Twilio MediaStream
@app.websocket("/media")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket connected")

    audio_queue = asyncio.Queue()

    async def stream_to_deepgram():
        try:
            deepgram_connection = await dg_client.transcription.live({
                "punctuate": True,
                "interim_results": False,
                "language": "en-US",
                "encoding": "mulaw",
                "sample_rate": 8000,
            })

            def on_transcript(data):
                transcript = data.get("channel", {}).get("alternatives", [{}])[0].get("transcript")
                if transcript:
                    print("Transcript:", transcript)

            deepgram_connection.register_handler("transcript_received", on_transcript)

            while True:
                chunk = await audio_queue.get()
                if chunk is None:
                    break
                await deepgram_connection.send(chunk)

        except Exception as e:
            print(f"Deepgram streaming error: {e}")

    asyncio.create_task(stream_to_deepgram())

    try:
        while True:
            message = await websocket.receive_json()
            event = message.get("event")

            if event == "start":
                print("MediaStream started")
            elif event == "media":
                payload = message["media"]["payload"]
                audio_data = base64.b64decode(payload)
                await audio_queue.put(audio_data)
            elif event == "stop":
                print("MediaStream stopped")
                break

    except WebSocketDisconnect:
        print("WebSocket disconnected")

    finally:
        await audio_queue.put(None)
