import logging
import os
import json
import base64
import asyncio
import time
import uuid
import subprocess
import requests  # ✅ Added for ElevenLabs API
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles  # ✅ Added for serving audio
from twilio.twiml.voice_response import VoiceResponse, Start, Stream
from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents
from openai import OpenAI
from dotenv import load_dotenv
load_dotenv("/root/Fast-API-App/.env")

# Detect which VM / container you’re on
INSTANCE = (
    os.getenv("FLY_ALLOC_ID")      # Fly.io VM ID (present in production)
    or os.getenv("HOSTNAME")       # Docker / Kubernetes fallback
    or os.uname().nodename         # last-resort fallback
)

print(f"🆔 This app instance ID is: {INSTANCE}")

# Configure the root logger
logging.basicConfig(
    level=logging.INFO,
    format=f"[{INSTANCE}] %(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("app").info     # quick alias → use log(...)

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")  # ✅ Also needed
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")

if not DEEPGRAM_API_KEY:
    raise RuntimeError("Missing DEEPGRAM_API_KEY in environment")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY in environment")
if not ELEVENLABS_API_KEY:
    raise RuntimeError("Missing ELEVENLABS_API_KEY in environment")

# ✅ Create the OpenAI client after loading the env
client = OpenAI(api_key=OPENAI_API_KEY)

# Simple in-memory session store
session_memory = {}

# ✅ Create FastAPI app and mount static audio folder
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

def save_transcript(call_sid, user_transcript=None, audio_path=None, gpt_response=None):
    if call_sid not in session_memory:
        session_memory[call_sid] = {}

    if user_transcript:
        session_memory[call_sid]["user_transcript"] = user_transcript
        session_memory[call_sid]["transcript_version"] = time.time()  # 👈 Add this line

    if gpt_response:
        session_memory[call_sid]["gpt_response"] = gpt_response
    if audio_path:
        session_memory[call_sid]["audio_path"] = audio_path
        
async def get_last_transcript_for_this_call(call_sid, last_known_version=None):
    while True:
        data = session_memory.get(call_sid)
        if data and data.get("user_transcript"):
            version = data.get("transcript_version", 0)
            if last_known_version is None or version > last_known_version:
                return data["user_transcript"], version
        await asyncio.sleep(0.1)

def get_last_audio_for_call(call_sid):
    data = session_memory.get(call_sid)

    if data and "audio_path" in data:
        log(f"🎧 Retrieved audio path for {call_sid}: {data['audio_path']}")
        return data["audio_path"]
    else:
        logging.error(f"❌ No audio path found for {call_sid} in session memory.")
        return None

# ✅ GPT handler function
async def get_gpt_response(user_text: str) -> str:
    try:
        safe_text = "" if user_text is None else str(user_text)
        if not safe_text.strip():
            safe_text = "Hello, how can I help you today?"
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a helpful AI assistant named Lotus. Keep your responses clear and concise."},
                {"role": "user", "content": safe_text}
            ]
        )
        return response.choices[0].message.content or "[GPT returned empty message]"
    except Exception as e:
        print(f"⚠️ GPT Error: {e}")
        return "[GPT failed to respond]"

# ✅ Helper to run GPT in executor from a thread
async def print_gpt_response(sentence: str):
    response = await get_gpt_response(sentence)
    print(f"🤖 GPT: {response}")
    
    # Step 3: Save audio to file
    audio_bytes = audio_response.content
    
    # 👇 Make unique filename with UUID
    unique_id = str(uuid.uuid4())
    filename = f"response_{unique_id}.wav"
    file_path = f"static/audio/{filename}"
    converted_path = f"static/audio/response_{unique_id}_ulaw.wav"

    print(f"🔊 Audio file size: {len(audio_bytes)} bytes")
    print(f"💾 Saving audio to {file_path}")
    
    os.makedirs("static/audio", exist_ok=True)
    with open(file_path, "wb") as f:  # ✅ use dynamic path
        f.write(audio_bytes)
        print("✅ Audio file saved at:", file_path)
        print(f"🎧 Got {len(audio_bytes)} audio bytes from ElevenLabs")
        
    for _ in range(10):  # wait up to 5 seconds
        if os.path.exists(converted_path):
            print("✅ File exists for playback:", converted_path)
            break
        print("⌛ Waiting for file to become available...")
        time.sleep(0.5)
    else:
        print("❌ File still not found after 5 seconds!")
        
class VerboseStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        #Build full URL
        scheme   = scope.get("scheme", "http")
        host     = dict(scope["headers"]).get(b"host", b"-").decode()
        full_url = f"{scheme}://{host}{scope['path']}"

        abs_path = os.path.abspath(os.path.join(self.directory, path))
        exists   = os.path.exists(abs_path)
        readable = os.access(abs_path, os.R_OK)

        log(
            f"📂 Static GET {path!r} → exists={exists} "
            f"readable={readable} size={os.path.getsize(abs_path) if exists else '—'}"
        )

        if not exists:
            try:
                parent = os.path.dirname(abs_path)
                log("📑 Dir listing: %s", os.listdir(parent))
            except Exception as e:
                log("⚠️ Could not list directory: %s", e)

        return await super().get_response(path, scope)
        
@app.post("/")
async def twilio_voice_webhook(request: Request):
    print("\n📞 ── [POST] Twilio webhook hit ───────────────────────────────────")
    form_data = await request.form()
    call_sid = form_data.get("CallSid") or str(uuid.uuid4())
    print(f"🆔 Call SID: {call_sid}")
    print(f"🧠 Current session_memory keys: {list(session_memory.keys())}")

    # 🧠 Pull or initialize session for this call
    session = session_memory.get(call_sid, {})
    
    # 🚦 Check if greeting has already been played
    if not session.get("greeting_played"):
        session["greeting_played"] = True
        session_memory[call_sid] = session  # ✅ Important: persist the update
        vr = VoiceResponse()
        vr.redirect("/greeting")
        print("👋 First-time caller — redirecting to greeting handler.")
        return Response(content=str(vr), media_type="application/xml")

    # ── 2. PULL LAST TRANSCRIPT (if any) ───────────────────────────────────────
    # Before waiting for new transcript
    last_known_version = session_memory.get(call_sid, {}).get("transcript_version", 0)
    # Wait for a newer one
    gpt_input, new_version = await get_last_transcript_for_this_call(call_sid, last_known_version)
    print(f"📝 GPT input candidate: \"{gpt_input}\"")

    # Simple transcript quality check
    if not gpt_input or len(gpt_input.strip()) < 4:
        print("⚠️ Transcript too short or missing — asking user to repeat")
        gpt_text = "Sorry, I didn't catch that. Could you please repeat yourself?"
    else:
        gpt_text = await get_gpt_response(gpt_input)

    # 🧼 Clear the transcript to avoid reuse in next round
    session_memory[call_sid]["user_transcript"] = None
    session_memory[call_sid]["transcript_version"] = 0

    # ── 3. TEXT-TO-SPEECH WITH ELEVENLABS ──────────────────────────────────────
    elevenlabs_response = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
        headers={
            "xi-api-key": os.getenv("ELEVENLABS_API_KEY"),
            "Content-Type": "application/json"
        },
        json={
            "text": gpt_text,
            "model_id": "eleven_flash_v2_5",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
        }
    )
    
    print("🧪 ElevenLabs status:", elevenlabs_response.status_code)
    print("🧪 ElevenLabs content type:", elevenlabs_response.headers.get("Content-Type")) 
    print("🛰️ ElevenLabs Status Code:", elevenlabs_response.status_code)
    print("🛰️ ElevenLabs Content-Type:", elevenlabs_response.headers.get("Content-Type"))
    print("🛰️ ElevenLabs Response Length:", len(elevenlabs_response.content), "bytes")
    print("🛰️ ElevenLabs Content (first 500 bytes):", elevenlabs_response.content[:500])
    print(f"🎙️ ElevenLabs status {elevenlabs_response.status_code}, "
          f"bytes {len(elevenlabs_response.content)}")

    audio_bytes = elevenlabs_response.content
    unique_id = uuid.uuid4().hex
    file_path = f"static/audio/response_{unique_id}.wav"

    with open(file_path, "wb") as f:
        f.write(audio_bytes)
    print(f"💾 Saved original WAV → {file_path}")

    await asyncio.sleep(1)

    # ✅ Failure check with print statements
    if not audio_bytes or elevenlabs_response.status_code != 200:
        print("❌ ElevenLabs failed or returned empty audio!")
        print("🔁 GPT Text:", gpt_text)
        print("🛑 Status:", elevenlabs_response.status_code)
        print("📜 Response:", elevenlabs_response.text)
        return Response("Audio generation failed.", status_code=500)
        
    # ── 4. CONVERT TO μ-LAW 8 kHz ──────────────────────────────────────────────
    converted_path = f"static/audio/response_{unique_id}_ulaw.wav"
    try:
        subprocess.run([
            "/usr/bin/ffmpeg", "-y", "-i", file_path,
            "-ar", "8000", "-ac", "1", "-c:a", "pcm_mulaw", converted_path
        ], check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ FFmpeg failed: {e}")
        return Response("Audio conversion failed", status_code=500)
    print("🧭 Checking absolute path:", os.path.abspath(converted_path))
    # ✅ Wait for file to become available (race condition guard)
    for i in range(40):
        if os.path.isfile(converted_path):
            print(f"✅ Found converted file after {i * 0.1:.1f}s")
            break
        await asyncio.sleep(0.1)
    else:
        print("❌ Converted file never appeared — aborting")
        return Response("Converted audio not available", status_code=500)
    print(f"🎛️ Converted WAV (8 kHz μ-law) → {converted_path}")
    log("✅ Audio file saved at %s", converted_path)
    # ✅ Only save if audio is a reasonable size (avoid silent/broken audio)
    if len(audio_bytes) > 2000:
        save_transcript(call_sid, audio_path=converted_path, gpt_response=gpt_text)
        print(f"🧠 Session updated AFTER save: {session_memory.get(call_sid)}")
    else:
        print("⚠️ Skipping transcript/audio save due to likely blank response.")

    # ── 5. BUILD TWIML ─────────────────────────────────────────────────────────
    vr = VoiceResponse()

    # Start Deepgram stream
    start = Start()
    start.stream(
        url="wss://silent-sound-1030.fly.dev/media",
        content_type="audio/x-mulaw;rate=8000"
    )
    vr.append(start)

    log("📡 Starting Deepgram stream to WebSocket endpoint")

    # Try to retrieve the most recent converted file with retries
    audio_path = None
    for _ in range(10):
        current_path = get_last_audio_for_call(call_sid)
        log(f"🔁 Checking session memory for {call_sid} → {current_path}")
        print(f"🔎 Full session_memory[{call_sid}] = {json.dumps(session_memory.get(call_sid), indent=2)}")
        if current_path and os.path.exists(current_path):
            audio_path = current_path
            break
        await asyncio.sleep(1)

    if audio_path:
        ulaw_filename = os.path.basename(audio_path)
        vr.play(f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}")
        print("🔗 Final playback URL:", f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}")
        print(f"✅ Queued audio for playback: {ulaw_filename}")
    else:
        print("❌ Audio not found after retry loop")
        vr.say("Sorry, something went wrong.")
        
    # ✅ Replace hangup with redirect back to self
    vr.redirect("/")
    print("📝 Returning TwiML to Twilio (with redirect).")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/greeting")
async def greeting_rout(request: Request):
    print("\n📞 ── [POST] Greeting handler hit ───────────────────────────────────")
    form_data = await request.form()
    call_sid = form_data.get("CallSid") or str(uuid.uuid4())
    print(f"🆔 Call SID: {call_sid}")
    print(f"🧠 Current session_memory keys: {list(session_memory.keys())}")

    # ── 2. 1 TIME GREETING ───────────────────────────────────────
    gpt_text = "Hello my name is Lotus, how can I help you today?"        
    print(f"✅ GPT greeting: \"{gpt_text}\"")

    # ── 3. TEXT-TO-SPEECH WITH ELEVENLABS ──────────────────────────────────────
    elevenlabs_response = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
        headers={
            "xi-api-key": os.getenv("ELEVENLABS_API_KEY"),
            "Content-Type": "application/json"
        },
        json={
            "text": gpt_text,
            "model_id": "eleven_flash_v2_5",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
        }
    )
    
    print("🧪 ElevenLabs status:", elevenlabs_response.status_code)
    print("🧪 ElevenLabs content type:", elevenlabs_response.headers.get("Content-Type")) 
    print("🛰️ ElevenLabs Status Code:", elevenlabs_response.status_code)
    print("🛰️ ElevenLabs Content-Type:", elevenlabs_response.headers.get("Content-Type"))
    print("🛰️ ElevenLabs Response Length:", len(elevenlabs_response.content), "bytes")
    print("🛰️ ElevenLabs Content (first 500 bytes):", elevenlabs_response.content[:500])
    print(f"🎙️ ElevenLabs status {elevenlabs_response.status_code}, "
          f"bytes {len(elevenlabs_response.content)}")

    audio_bytes = elevenlabs_response.content
    unique_id = uuid.uuid4().hex
    file_path = f"static/audio/response_{unique_id}.wav"

    with open(file_path, "wb") as f:
        f.write(audio_bytes)
    print(f"💾 Saved original WAV → {file_path}")

    await asyncio.sleep(1)

    # ✅ Failure check with print statements
    if not audio_bytes or elevenlabs_response.status_code != 200:
        print("❌ ElevenLabs failed or returned empty audio!")
        print("🔁 GPT Text:", gpt_text)
        print("🛑 Status:", elevenlabs_response.status_code)
        print("📜 Response:", elevenlabs_response.text)
        return Response("Audio generation failed.", status_code=500)
        
    # ── 4. CONVERT TO μ-LAW 8 kHz ──────────────────────────────────────────────
    converted_path = f"static/audio/response_{unique_id}_ulaw.wav"
    try:
        subprocess.run([
            "/usr/bin/ffmpeg", "-y", "-i", file_path,
            "-ar", "8000", "-ac", "1", "-c:a", "pcm_mulaw", converted_path
        ], check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ FFmpeg failed: {e}")
        return Response("Audio conversion failed", status_code=500)
    print("🧭 Checking absolute path:", os.path.abspath(converted_path))
    # ✅ Wait for file to become available (race condition guard)
    for i in range(40):
        if os.path.isfile(converted_path):
            print(f"✅ Found converted file after {i * 0.1:.1f}s")
            break
        await asyncio.sleep(0.1)
    else:
        print("❌ Converted file never appeared — aborting")
        return Response("Converted audio not available", status_code=500)
    print(f"🎛️ Converted WAV (8 kHz μ-law) → {converted_path}")
    log("✅ Audio file saved at %s", converted_path)
    # ✅ Only save if audio is a reasonable size (avoid silent/broken audio)
    if len(audio_bytes) > 2000:
        save_transcript(call_sid, audio_path=converted_path, gpt_response=gpt_text)
        print(f"🧠 Session updated AFTER save: {session_memory.get(call_sid)}")
    else:
        print("⚠️ Skipping transcript/audio save due to likely blank response.")

    # ── 5. BUILD TWIML ─────────────────────────────────────────────────────────
    vr = VoiceResponse()

    # Start Deepgram stream
    start = Start()
    start.stream(
        url="wss://silent-sound-1030.fly.dev/media",
        content_type="audio/x-mulaw;rate=8000"
    )
    vr.append(start)

    log("📡 Starting Deepgram stream to WebSocket endpoint")

    # Try to retrieve the most recent converted file with retries
    audio_path = None
    for _ in range(10):
        current_path = get_last_audio_for_call(call_sid)
        log(f"🔁 Checking session memory for {call_sid} → {current_path}")
        print(f"🔎 Full session_memory[{call_sid}] = {json.dumps(session_memory.get(call_sid), indent=2)}")
        if current_path and os.path.exists(current_path):
            audio_path = current_path
            break
        await asyncio.sleep(1)

    if audio_path:
        ulaw_filename = os.path.basename(audio_path)
        vr.play(f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}")
        print("🔗 Final playback URL:", f"https://silent-sound-1030.fly.dev/static/audio/{ulaw_filename}")
        print(f"✅ Queued audio for playback: {ulaw_filename}")
    else:
        print("❌ Audio not found after retry loop")
        vr.say("Sorry, something went wrong.")
        
    # ✅ Replace hangup with redirect back to self
    vr.redirect("/")
    print("📝 Returning TwiML to Twilio (with redirect).")
    return Response(content=str(vr), media_type="application/xml")

@app.websocket("/media")
async def media_stream(ws: WebSocket):
    await ws.accept()
    print("★ Twilio WebSocket connected")

    call_sid_holder = {"sid": None}
    last_input_time = {"ts": time.time()}
    last_transcript = {"text": "", "confidence": 0.5, "is_final": False}
    finished = {"done": False}
    turn_locked = {"locked": False}  # 🔐 New lock flag

    final_transcripts = []
    
    loop = asyncio.get_running_loop()
    deepgram = DeepgramClient(DEEPGRAM_API_KEY)
    dg_connection = None
    
    try:
        print("⚙️ Connecting to Deepgram live transcription...")

        try:
            live_client = deepgram.listen.live

            deepgram_options = {
                "punctuate": True,
                "interim_results": True,
                "endpointing": 2000  # 🟢 Wait 2000ms of silence before finalizing
                }
            
            dg_connection = await asyncio.to_thread(live_client.v, "1")
        except Exception as e:
            print(f"⛔ Failed to create Deepgram connection: {e}")
            await ws.close()
            return

        def on_transcript(*args, **kwargs):
            try:
                print("📥 RAW transcript event:")
                result = kwargs.get("result") or (args[0] if args else None)
                metadata = kwargs.get("metadata")

                if result is None:
                    print("⚠️ No result received.")
                    return

                print("📂 Type of result:", type(result))

                if hasattr(result, "to_dict"):
                    payload = result.to_dict()
                    print(json.dumps(payload, indent=2))

                    speech_final = payload.get("speech_final", False)

                    try:
                        alt = payload["channel"]["alternatives"][0]
                        sentence = alt.get("transcript", "")
                        confidence = alt.get("confidence", 0.0)
                        is_final = payload["is_final"] if "is_final" in payload else False
                        
                        if is_final and sentence.strip() and confidence >= 0.6:
                            print(f"✅ Final transcript received: \"{sentence}\" (confidence: {confidence})")

                            last_input_time["ts"] = time.time()
                            last_transcript["text"] = sentence
                            last_transcript["confidence"] = confidence
                            last_transcript["is_final"] = True

                            final_transcripts.append(sentence)

                            if speech_final and not turn_locked["locked"]:
                                print("🧠 speech_final received — concatenating full transcript")
                                full_transcript = " ".join(final_transcripts)

                                if call_sid_holder["sid"]:
                                    sid = call_sid_holder["sid"]
                                    session_memory.setdefault(sid, {})
                                    session_memory[sid]["user_transcript"] = full_transcript
                                    session_memory[sid]["ready"] = True
                                    session_memory[sid]["transcript_version"] = time.time()

                                    save_transcript(sid, user_transcript=full_transcript)

                                # 🔐 Lock the turn so nothing else gets processed until reset
                                turn_locked["locked"] = True

                                # ✅ Reset transcript state for next turn
                                final_transcripts.clear()
                                last_transcript["text"] = ""
                                last_transcript["confidence"] = 0.0
                                last_transcript["is_final"] = False
                            else:
                                if speech_final and turn_locked["locked"]:
                                    print("🔒 Ignored speech_final — turn is locked.")

                        elif is_final:
                            print(f"⚠️ Final transcript was too unclear: \"{sentence}\" (confidence: {confidence})")

                    except KeyError as e:
                        print(f"⚠️ Missing expected key in payload: {e}")
                    except Exception as inner_e:
                        print(f"⚠️ Could not extract transcript sentence: {inner_e}")
            except Exception as e:  # ← This closes the OUTER try
                print(f"⚠️ Error handling transcript: {e}")
                
        dg_connection.on(LiveTranscriptionEvents.Transcript, on_transcript)

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
        
        async def monitor_user_done():
            while not finished["done"]:
                await asyncio.sleep(0.5)
                elapsed = time.time() - last_input_time["ts"]
        
                if (
                    elapsed > 2.0 and
                    last_transcript["confidence"] >= 0.5 and
                    last_transcript.get("is_final", False)
                ):
                    print(f"✅ User finished speaking (elapsed: {elapsed:.1f}s, confidence: {last_transcript['confidence']})")
                    finished["done"] = True
                    
                    print("⏳ Waiting for POST to handle GPT + TTS...")
                    for _ in range(40):  # up to 4 seconds
                        audio_path = session_memory.get(call_sid_holder["sid"], {}).get("audio_path")
                        if audio_path and os.path.exists(audio_path):
                            print(f"✅ POST-generated audio is ready: {audio_path}")
                                turn_locked["locked"] = False  # 🔓
                                finished["done"] = False       # 🔄
                                print("🔓 Turn unlocked — ready for next user input")
                                break
                        await asyncio.sleep(0.1)
                    else:
                        print("❌ Timed out waiting for POST to generate GPT audio.")
                        
        loop.create_task(monitor_user_done())
        
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
                    print("📩 Incoming message:", msg.get("event"))
                except json.JSONDecodeError as e:
                    print(f"⚠️ JSON decode error: {e}")
                    continue

                event = msg.get("event")

                if event == "start":
                    print("▶️ Stream started (StreamSid:", msg["start"].get("streamSid"), ")")

                    # Debug print to inspect what Twilio actually sent
                    print("🧾 Twilio start event data:", json.dumps(msg["start"], indent=2))

                    # Try all possible keys Twilio might send
                    sid = (
                        msg["start"].get("callSid") or
                        msg["start"].get("CallSid") or
                        msg["start"].get("callerSid") or
                        msg["start"].get("CallerSid")
                    )

                    call_sid_holder["sid"] = sid
                    print(f"📞 [WebSocket] call_sid_holder['sid']: {call_sid_holder['sid']}")
                    
                elif event == "media":
                    print("📡 Media event received")
                    try:
                        payload = base64.b64decode(msg["media"]["payload"])
                        dg_connection.send(payload)
                        last_input_time["ts"] = time.time()
                        print(f"📦 Sent {len(payload)} bytes to Deepgram (event: media)")
                    except Exception as e:
                        print(f"⚠️ Error sending to Deepgram: {e}")

                elif event == "stop":
                    print("⏹ Stream stopped by Twilio")
                    break

        await sender()

    except Exception as e:
        print(f"⛔ Deepgram error: {e}")

    finally:
        if dg_connection:
            try:
                dg_connection.finish()
            except Exception as e:
                print(f"⚠️ Error closing Deepgram connection: {e}")
        try:
            await ws.close()
        except Exception as e:
            print(f"⚠️ Error closing WebSocket: {e}")
        print("✅ Connection closed")
        
