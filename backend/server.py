"""FastAPI server hosting the Pipecat voice WebSocket and dashboard APIs."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger

from backend.database import get_dashboard_state, init_db
from backend.pipeline import DEFAULT_ELEVENLABS_VOICE_ID, DEFAULT_GEMINI_MODEL, run_voice_session

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"

load_dotenv()


class DashboardBroadcastHub:
    """Manage live WebSocket subscribers for real-time dashboard state updates."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self._connections.discard(ws)


hub = DashboardBroadcastHub()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="MedCare Voice AI Doctor Appointment Assistant",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Return service health and pipeline provider configuration status."""
    return {
        "status": "ok",
        "pipeline": {
            "framework": "pipecat-ai",
            "vad": "silero",
            "stt": "elevenlabs-realtime",
            "llm": f"google-gemini ({os.getenv('GEMINI_MODEL', DEFAULT_GEMINI_MODEL)})",
            "tts": f"elevenlabs ({os.getenv('ELEVENLABS_VOICE_ID', DEFAULT_ELEVENLABS_VOICE_ID)})",
        },
        "keys_configured": {
            "elevenlabs": bool((os.getenv("ELEVENLABS_API_KEY") or "").strip()),
            "gemini": bool((os.getenv("GEMINI_API_KEY") or "").strip()),
        },
    }


@app.get("/api/dashboard")
async def dashboard_state() -> dict[str, Any]:
    """Return doctors, available/booked slots, and appointments."""
    return get_dashboard_state()


@app.websocket("/ws/events")
async def dashboard_events_ws(websocket: WebSocket) -> None:
    """Push real-time booking and cancellation updates to dashboard clients."""
    await hub.connect(websocket)
    try:
        await websocket.send_json(
            {"type": "dashboard_snapshot", "dashboard": get_dashboard_state()}
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(websocket)
    except Exception:
        hub.disconnect(websocket)


@app.websocket("/ws/voice")
async def voice_call_ws(websocket: WebSocket) -> None:
    """Accept a browser WebSocket voice call and run the Pipecat pipeline."""
    await websocket.accept()
    try:
        await run_voice_session(websocket, on_dashboard_update=hub.broadcast)
    except WebSocketDisconnect:
        logger.info("WebSocket /ws/voice disconnected.")
    except Exception as exc:
        logger.exception(f"Voice session error: {exc}")
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
            await websocket.close(code=1011)
        except Exception:
            pass


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
