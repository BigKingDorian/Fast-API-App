import os, io, json, base64
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from dotenv import load_dotenv
import requests
import audioop
from pydub import AudioSegment
# (Assuming Deepgram SDK or websocket is imported and configured elsewhere)

load_dotenv()

# Load required API keys and config from environment
DEEPGRAM_API_KEY = os.getenv('DEEPGRAM_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
ELEVENLABS_API_KEY = os.getenv('ELEVENLABS_API_KEY')
ELEVENLABS_VOICE_ID = os.getenv('ELEVENLABS_VOICE_ID')  # Voice ID for ElevenLabs TTS
XI_MODEL_ID = os.getenv('XI_MODEL_ID')  # (Optional) ElevenLabs model ID for TTS
SYSTEM_MESSAGE = os.getenv('SYSTEM_MESSAGE', "You are a helpful AI assistant.")  # AI persona

# Ensure essential keys are present
if not OPENAI_API_KEY:
    raise ValueError("Missing the OpenAI API key. Please set it in the environment.")
# Set OpenAI API key for the openai client
import openai
openai.api_key = OPENAI_API_KEY

app = FastAPI()

@app.post("/incoming-call")
async def incoming_call(request: Request):
    """Twilio Voice webhook for incoming calls: returns TwiML that starts the media stream."""
    host = request.url.hostname
    response = VoiceResponse()
    # Removed Twilio <Say> initial greeting – the AI will handle greeting via the WebSocket
    connect = Connect()
    connect.stream(url=f"wss://{host}/media-stream")
    response.append(connect)
    return HTMLResponse(str(response), status_code=200)

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    """
    Handle the bidirectional media stream with Twilio.
    Receives audio from Twilio (caller -> AI) and sends audio back (AI -> caller).
    """
    await websocket.accept()
    # (Optional) Initialize Deepgram live transcription connection if using Deepgram SDK
    dg_socket = None
    try:
        # Example: using Deepgram SDK to start live transcription (punctuate, etc.)
        # from deepgram import Deepgram
        # deepgram = Deepgram(DEEPGRAM_API_KEY)
        # dg_socket = await deepgram.transcription.live({'punctuate': True, 'interim_results': False})
        # if dg_socket: 
        #     dg_socket.register_handler(..., handle_transcript)
        pass
    except Exception as e:
        app.logger.error(f"Deepgram connection error: {e}")

    # Conversation state for OpenAI
    conversation = [ {"role": "system", "content": SYSTEM_MESSAGE} ]
    stream_sid = None
    sequence_number = 0
    outbound_chunk_count = 0

    # Define a handler for finalized transcripts from Deepgram
    async def handle_transcript(transcript: dict):
        """Handle final transcriptions from Deepgram by querying OpenAI and streaming the response."""
        nonlocal sequence_number, outbound_chunk_count
        if not transcript.get("is_final", False):
            return  # ignore interim transcripts if any
        user_text = transcript.get("text", "").strip()
        if not user_text:
            return
        app.logger.info(f"Caller said: {user_text}")
        # Append user message to conversation history
        conversation.append({"role": "user", "content": user_text})
        # Get response from OpenAI
        try:
            ai_resp = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=conversation
            )
            assistant_text = ai_resp.choices[0].message.content.strip()
        except Exception as e:
            app.logger.error(f"OpenAI API error: {e}")
            # Fallback to a default response on error
            assistant_text = "I'm sorry, I encountered an error."
        # Append assistant response to conversation history
        conversation.append({"role": "assistant", "content": assistant_text})
        app.logger.info(f"AI response: {assistant_text}")
        # Convert assistant_text to speech using ElevenLabs (direct μ-law if possible)
        audio_data_ulaw = b''
        try:
            tts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream?output_format=ulaw_8000&optimize_streaming_latency=3"
            tts_headers = {
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/wav"
            }
            tts_body = {"text": assistant_text}
            if XI_MODEL_ID:
                tts_body["model_id"] = XI_MODEL_ID
            tts_res = requests.post(tts_url, headers=tts_headers, json=tts_body)
            if tts_res.status_code == 200:
                audio_data_ulaw = tts_res.content
                # Strip WAV header if present
                if audio_data_ulaw[:4] == b'RIFF':
                    audio_data_ulaw = audio_data_ulaw[44:]
            else:
                app.logger.error(f"ElevenLabs TTS HTTP {tts_res.status_code}: {tts_res.text}")
        except Exception as e:
            app.logger.error(f"ElevenLabs TTS (ulaw_8000) error: {e}")
        # Fallback to MP3 conversion if direct ulaw failed
        if not audio_data_ulaw:
            try:
                fb_url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
                fb_headers = {
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json"
                }
                fb_body = {"text": assistant_text}
                if XI_MODEL_ID:
                    fb_body["model_id"] = XI_MODEL_ID
                fb_res = requests.post(fb_url, headers=fb_headers, json=fb_body)
                fb_res.raise_for_status()
                audio_mp3 = fb_res.content
                # Convert MP3 to 8kHz mono PCM and then to μ-law
                audio_segment = AudioSegment.from_file(io.BytesIO(audio_mp3), format="mp3")
                audio_segment = audio_segment.set_frame_rate(8000).set_channels(1).set_sample_width(2)
                pcm_data = audio_segment.raw_data  # 16-bit PCM audio
                audio_data_ulaw = audioop.lin2ulaw(pcm_data, 2)
            except Exception as e:
                app.logger.error(f"ElevenLabs MP3 fallback error: {e}")
                audio_data_ulaw = b''
        # Stream the audio data back to Twilio in chunks
        if audio_data_ulaw:
            chunk_size = 160  # 160 bytes = 20ms of 8kHz μ-law audio
            for i in range(0, len(audio_data_ulaw), chunk_size):
                chunk = audio_data_ulaw[i:i+chunk_size]
                if len(chunk) < chunk_size:
                    # Pad last chunk with silence (μ-law 0xFF) if needed
                    chunk = chunk.ljust(chunk_size, b'\xFF')
                outbound_chunk_count += 1
                sequence_number += 1
                payload_b64 = base64.b64encode(chunk).decode('ascii')
                outbound_msg = {
                    "event": "media",
                    "streamSid": stream_sid,
                    "sequenceNumber": str(sequence_number),
                    "media": {
                        "track": "outbound",
                        "chunk": str(outbound_chunk_count),
                        "timestamp": str(int((outbound_chunk_count - 1) * 20)),  # in milliseconds
                        "payload": payload_b64
                    }
                }
                try:
                    await websocket.send_text(json.dumps(outbound_msg))
                except Exception as e:
                    app.logger.error(f"Error sending audio chunk: {e}")
        else:
            app.logger.error("No audio data to stream back to Twilio (assistant_text was empty or TTS failed).")

    # Main loop: receive media stream messages from Twilio and feed to Deepgram
    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event = data.get("event")
            if event == "start":
                # Connection started – store Stream SID and initial sequence
                stream_sid = data['start']['streamSid']
                sequence_number = int(data.get("sequenceNumber", 0))
                app.logger.info(f"Media stream started (Stream SID: {stream_sid})")
                # Generate initial greeting from OpenAI
                try:
                    # Prompt the assistant to produce a greeting
                    conversation.append({"role": "user", "content": "Hello"})
                    ai_greet = openai.ChatCompletion.create(
                        model="gpt-3.5-turbo",
                        messages=conversation
                    )
                    greet_text = ai_greet.choices[0].message.content.strip()
                except Exception as e:
                    app.logger.error(f"OpenAI greeting error: {e}")
                    greet_text = "Hello! How can I assist you today?"
                # Add the assistant's greeting to conversation history
                conversation.append({"role": "assistant", "content": greet_text})
                app.logger.info(f"AI initial greeting: {greet_text}")
                # Synthesize greeting to speech via ElevenLabs and stream it
                audio_data = b''
                try:
                    tts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream?output_format=ulaw_8000&optimize_streaming_latency=3"
                    tts_headers = {
                        "xi-api-key": ELEVENLABS_API_KEY,
                        "Content-Type": "application/json",
                        "Accept": "audio/wav"
                    }
                    tts_body = {"text": greet_text}
                    if XI_MODEL_ID:
                        tts_body["model_id"] = XI_MODEL_ID
                    res = requests.post(tts_url, headers=tts_headers, json=tts_body)
                    if res.status_code == 200:
                        audio_data = res.content
                        if audio_data[:4] == b'RIFF':  # strip WAV header if present
                            audio_data = audio_data[44:]
                    else:
                        app.logger.error(f"ElevenLabs greeting TTS HTTP {res.status_code}: {res.text}")
                except Exception as e:
                    app.logger.error(f"ElevenLabs greeting TTS error: {e}")
                if not audio_data:
                    # Fallback to MP3 if direct ulaw stream failed
                    try:
                        fb_res = requests.post(
                            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
                            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                            json={"text": greet_text, **({"model_id": XI_MODEL_ID} if XI_MODEL_ID else {})}
                        )
                        fb_res.raise_for_status()
                        audio_mp3 = fb_res.content
                        # Convert to 8kHz PCM and then μ-law
                        seg = AudioSegment.from_file(io.BytesIO(audio_mp3), format="mp3")
                        seg = seg.set_frame_rate(8000).set_channels(1).set_sample_width(2)
                        pcm = seg.raw_data
                        audio_data = audioop.lin2ulaw(pcm, 2)
                    except Exception as e:
                        app.logger.error(f"ElevenLabs greeting MP3 fallback error: {e}")
                        audio_data = b''
                # Stream greeting audio to caller via Twilio WS
                if audio_data:
                    chunk_size = 160
                    outbound_chunk_count = 0  # reset chunk counter for new sequence of audio
                    for i in range(0, len(audio_data), chunk_size):
                        chunk = audio_data[i:i+chunk_size]
                        if len(chunk) < chunk_size:
                            chunk = chunk.ljust(chunk_size, b'\xFF')
                        outbound_chunk_count += 1
                        sequence_number += 1
                        payload = base64.b64encode(chunk).decode('ascii')
                        outbound_msg = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "sequenceNumber": str(sequence_number),
                            "media": {
                                "track": "outbound",
                                "chunk": str(outbound_chunk_count),
                                "timestamp": str(int((outbound_chunk_count - 1) * 20)),
                                "payload": payload
                            }
                        }
                        try:
                            await websocket.send_text(json.dumps(outbound_msg))
                        except Exception as e:
                            app.logger.error(f"Error sending greeting chunk: {e}")
                else:
                    app.logger.error("Failed to generate audio for greeting; no audio sent.")
            elif event == "media":
                # Inbound audio from Twilio (caller speaking) – forward to Deepgram for transcription
                sequence_number = int(data.get("sequenceNumber", sequence_number))
                payload = data['media']['payload']
                audio_chunk = base64.b64decode(payload)  # this is 20ms of μ-law audio
                # If using Deepgram, send the raw audio (after μ-law to PCM conversion if needed)
                try:
                    # Example: if Deepgram expects linear PCM, convert:
                    linear_chunk = audioop.ulaw2lin(audio_chunk, 2)
                    # Send to Deepgram's socket
                    if dg_socket:
                        dg_socket.send(linear_chunk)
                except Exception as e:
                    app.logger.error(f"Deepgram send error: {e}")
                # (If not using Deepgram SDK, alternative STT handling would go here)
            elif event == "closed" or event == "stop":
                app.logger.info("Media stream closed by Twilio")
                break
    except WebSocketDisconnect:
        app.logger.info("WebSocket disconnected")
    finally:
        # Cleanup: close Deepgram socket if open
        try:
            if dg_socket:
                await dg_socket.close()
        except Exception as e:
            app.logger.error(f"Error closing Deepgram socket: {e}")
