import os, io, json, base64, logging
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from dotenv import load_dotenv
import requests
import openai

load_dotenv()

# Set up logger
logger = logging.getLogger("uvicorn.error")

# Load API keys
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
ELEVENLABS_API_KEY = os.getenv('ELEVENLABS_API_KEY')
ELEVENLABS_VOICE_ID = os.getenv('ELEVENLABS_VOICE_ID')
SYSTEM_MESSAGE = os.getenv('SYSTEM_MESSAGE', "You are a helpful AI assistant.")

if not OPENAI_API_KEY or not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
    raise ValueError("Missing required API keys in environment.")

openai.api_key = OPENAI_API_KEY
app = FastAPI()

@app.post("/")
async def incoming_call(request: Request):
    """Returns TwiML that starts the Twilio media stream."""
    host = request.url.hostname
    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=f"wss://{host}/media-stream")
    response.append(connect)
    return HTMLResponse(str(response), status_code=200)

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()

    conversation = [{"role": "system", "content": SYSTEM_MESSAGE}]
    stream_sid = None
    sequence_number = 0
    outbound_chunk_count = 0

    async def stream_audio_to_twilio(text: str):
        nonlocal sequence_number, outbound_chunk_count
        try:
            res = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream?output_format=ulaw_8000&optimize_streaming_latency=3",
                headers={
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json",
                    "Accept": "audio/wav"
                },
                json={"text": text}
            )
            if res.status_code != 200:
                logger.error(f"ElevenLabs error {res.status_code}: {res.text}")
                return

            audio_data = res.content
            if audio_data[:4] == b'RIFF':
                audio_data = audio_data[44:]

            chunk_size = 160  # 20ms
            for i in range(0, len(audio_data), chunk_size):
                chunk = audio_data[i:i+chunk_size].ljust(chunk_size, b'\xFF')
                payload = base64.b64encode(chunk).decode('ascii')
                outbound_chunk_count += 1
                sequence_number += 1

                msg = {
                    "event": "media",
                    "streamSid": stream_sid,
                    "sequenceNumber": str(sequence_number),
                    "media": {
                        "track": "outbound",
                        "chunk": str(outbound_chunk_count),
                        "timestamp": str((outbound_chunk_count - 1) * 20),
                        "payload": payload
                    }
                }
                await websocket.send_text(json.dumps(msg))

        except Exception as e:
            logger.error(f"Error sending audio to Twilio: {e}")

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event = data.get("event")

            if event == "start":
                stream_sid = data['start']['streamSid']
                sequence_number = int(data.get("sequenceNumber", 0))
                logger.info(f"Media stream started (SID: {stream_sid})")

                # Trigger greeting
                conversation.append({"role": "user", "content": "Hello"})
                try:
                    completion = openai.ChatCompletion.create(
                        model="gpt-3.5-turbo",
                        messages=conversation
                    )
                    greeting = completion.choices[0].message.content.strip()
                except Exception as e:
                    logger.error(f"OpenAI greeting error: {e}")
                    greeting = "Hello, how can I assist you today?"
                conversation.append({"role": "assistant", "content": greeting})
                await stream_audio_to_twilio(greeting)

            elif event == "media":
                sequence_number = int(data.get("sequenceNumber", sequence_number))
                payload = base64.b64decode(data['media']['payload'])

                # Convert audio to text (you would send this to Deepgram here)
                # Simulating Deepgram with dummy logic
                # Replace this with real transcription pipeline
                dummy_transcript = "pretend this was transcribed text"
                conversation.append({"role": "user", "content": dummy_transcript})

                try:
                    completion = openai.ChatCompletion.create(
                        model="gpt-3.5-turbo",
                        messages=conversation
                    )
                    reply = completion.choices[0].message.content.strip()
                    conversation.append({"role": "assistant", "content": reply})
                    await stream_audio_to_twilio(reply)
                except Exception as e:
                    logger.error(f"OpenAI response error: {e}")

            elif event in ["stop", "closed"]:
                logger.info("Call ended by Twilio.")
                break

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
