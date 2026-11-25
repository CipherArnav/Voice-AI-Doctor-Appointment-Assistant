# MedCare — Voice AI Doctor Appointment Assistant

A real-time voice-based doctor appointment booking assistant built with **Pipecat**, **ElevenLabs STT & TTS**, **Google Gemini**, **Silero VAD**, **FastAPI**, and **SQLite**, paired with a responsive clinical scheduling dashboard and pop-up phone call interface.

**Live Demo:** https://medcare-voice-appointment-ai.onrender.com *(free-tier Render instance; allow ~30–50s to wake if idle)*

## Tech Stack

- **Voice Pipeline Framework:** Pipecat (`pipecat-ai` 1.17.0)
- **Voice Activity Detection (Bonus):** Silero VAD (`SileroVADAnalyzer`) for speech boundary detection and barge-in interruption handling
- **Speech-to-Text (STT):** ElevenLabs Realtime STT (`ElevenLabsRealtimeSTTService`, model `scribe_v2_realtime`, manual commit driven by Silero VAD)
- **LLM:** Google Gemini (`GoogleLLMService`, default model `gemini-3.6-flash`, configurable via `GEMINI_MODEL`)
- **Text-to-Speech (TTS):** ElevenLabs WebSocket TTS (`ElevenLabsTTSService`, model `eleven_flash_v2_5`, voice `EXAVITQu4vr4xnSDxMaL` / Sarah)
- **Backend & Database:** FastAPI, Uvicorn, SQLite (`data/appointments.db`)
- **Frontend Dashboard:** Responsive HTML, CSS, and Web Audio / WebSocket JavaScript served directly by FastAPI

---

## Architecture & Pipeline Flow

```text
Browser Microphone (16 kHz PCM)
  │  WebSocket binary frames (/ws/voice)
  ▼
FastAPIWebsocketTransport + BrowserAudioSerializer
  │
  ▼
ElevenLabsRealtimeSTTService (scribe_v2_realtime)
  │
  ▼
LLMUserAggregator (SileroVADAnalyzer)
  │  Commits user turn & triggers interruption on speech start
  ▼
GoogleLLMService (gemini-3.6-flash)
  │  Calls registered tools in backend/tools.py:
  │   ├─ check_doctor_availability
  │   ├─ book_appointment
  │   └─ cancel_appointment
  ▼
ElevenLabsTTSService (eleven_flash_v2_5)
  │
  ▼
FastAPIWebsocketTransport ──► Browser Audio Playback + Live UI Events
  │
  └─► /ws/events ──► Live Dashboard Table & Slot Highlights
```

### Strict Call Behavior
- **Phone-only caller identification:** Callers are identified exclusively by the phone number they speak out loud—no login, OTP, or caller-ID is required.
- **Read-back confirmation:** The assistant reads the spoken phone number back to the caller and waits for confirmation before calling `book_appointment` or `cancel_appointment`.
- **Single-call end-to-end flow:** Checking availability, booking, and cancelling all work within one continuous conversation.

---

## Quick Start

### 1. Create and Activate Virtual Environment

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Copy `.env.example` to `.env` and fill in your API keys (`.env` is gitignored and must not be committed):

**Windows (PowerShell):**
```powershell
Copy-Item .env.example .env
```

**macOS / Linux:**
```bash
cp .env.example .env
```

Edit `.env`:
```env
ELEVENLABS_API_KEY=your_elevenlabs_api_key
GEMINI_API_KEY=your_google_gemini_api_key
```

Optional overrides:
- `GEMINI_MODEL` (defaults to `gemini-3.6-flash`)
- `ELEVENLABS_VOICE_ID` (defaults to `EXAVITQu4vr4xnSDxMaL`)

### 4. Run the Application (Voice Pipeline + Frontend Dashboard)

FastAPI serves both the Pipecat voice WebSocket (`/ws/voice`), live dashboard broadcast WebSocket (`/ws/events`), REST endpoints (`/api/health`, `/api/dashboard`), and the frontend dashboard (`/`):

```bash
uvicorn backend.server:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000** in Chrome or Edge, allow microphone access, and click **Try Call**.

---

## Deploying to Render (Single Service)

This project includes [`render.yaml`](render.yaml) so both the frontend dashboard and Pipecat WebSocket voice pipeline deploy together on Render:

1. Push this repository to GitHub (`.env` is gitignored; only `.env.example` is committed).
2. In Render, choose **New + → Web Service** (or **Blueprint**) and connect your GitHub repository.
3. Render uses:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn backend.server:app --host 0.0.0.0 --port $PORT`
4. Add `ELEVENLABS_API_KEY` and `GEMINI_API_KEY` under **Environment Variables** in Render and deploy.

---

## Running Tests

Verify the SQLite tools, custom WebSocket frame serializer, Pipecat pipeline setup, and FastAPI routes:

```bash
python -m unittest discover -s tests -v
```

---

## Project Structure

```text
├── backend/
│   ├── __init__.py
│   ├── database.py       # SQLite schema, initial clinic seeding, dashboard queries
│   ├── tools.py          # check_doctor_availability, book_appointment, cancel_appointment
│   ├── serializer.py     # Bidirectional 16 kHz PCM + JSON WebSocket frame serializer
│   ├── pipeline.py       # Pipecat pipeline (Silero VAD, ElevenLabs STT/TTS, Gemini LLM)
│   └── server.py         # FastAPI server hosting REST APIs, /ws/voice, /ws/events, and static UI
├── frontend/
│   ├── index.html        # Clinic dashboard & pop-up phone call screen
│   ├── styles.css        # Responsive clinical UI, phone bezel, transitions & row highlights
│   └── app.js            # Mic capture, PCM playback, Web Audio waveform/SFX, live sync
├── tests/
│   ├── test_tools.py     # Database & tool unit tests
│   └── test_pipeline.py  # Pipeline, serializer, and FastAPI endpoint tests
├── .env.example          # Blank environment variable template
├── .gitignore
├── requirements.txt
└── README.md
```
