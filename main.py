import os
import json
import base64
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Start, Stream
from dotenv import load_dotenv

# ✅ NEW: Updated Deepgram imports for SDK v3
from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents

load_dotenv()
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
if not DEEPGRAM_API_KEY:
    raise RuntimeError("Missing DEEPGRAM_API_KEY in environment")

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

    deepgram = DeepgramClient(DEEPGRAM_API_KEY)
    dg_connection = None

    try:
        print("⚙️ Connecting to Deepgram live transcription...")

        # ✅ Create live transcription connection using async-compatible workaround
        try:
            live_client = deepgram.listen.live
            dg_connection = await asyncio.to_thread(live_client.v, "1")
        except Exception as e:
            print(f"⛔ Failed to create Deepgram connection: {e}")
            await ws.close()
            return

        # ✅ Transcript event handler
        def on_transcript(result, metadata=None):
            try:
                print("📥 RAW transcript event:")
                print("📂 Type of data:", type(result))

                if hasattr(result, "to_dict"):
                    payload = result.to_dict()
                    import json
                    print(json.dumps(payload, indent=2))

                    # Try to extract sentence
                    try:
                        sentence = payload["channel"]["alternatives"][0]["transcript"]
                        if sentence:
                            print(f"📝 {sentence}")
                    except Exception as inner_e:
                        print(f"⚠️ Could not extract transcript sentence: {inner_e}")
                else:
                    # Fallback logging
                    print("🔍 Available attributes:", dir(result))
                    print("⚠️ This object cannot be serialized directly. Trying .__dict__...")
                    print(result.__dict__)

            except Exception as e:
                print(f"⚠️ Error handling transcript: {e}")

        # ✅ Connect transcript handler to Deepgram
        dg_connection.on(LiveTranscriptionEvents.Transcript, on_transcript)

        # ✅ Start Deepgram stream
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

        # 🎧 Receive media from Twilio and forward to Deepgram
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
                        dg_connection.send(payload)  # ✅ v3: no await
                        print(f"📦 Sent {len(payload)} bytes to Deepgram (event: media)")
                    except Exception as e:
                        print(f"⚠️ Error sending to Deepgram: {e}")

                elif event == "stop":
                    print("⏹ Stream stopped by Twilio")
                    break

        await sender()  # ✅ Only sender needed — receiver handled via events

    except Exception as e:
        print(f"⛔ Deepgram error: {e}")
    finally:
        if dg_connection:
            try:
                dg_connection.finish()  # ✅ v3: no await
            except Exception as e:
                print(f"⚠️ Error closing Deepgram connection: {e}")
        try:
            await ws.close()
        except Exception as e:
            print(f"⚠️ Error closing WebSocket: {e}")
        print("✅ Connection closed")
