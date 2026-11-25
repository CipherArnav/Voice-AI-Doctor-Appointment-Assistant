"""Real-time Pipecat voice pipeline with Silero VAD, ElevenLabs STT/TTS, and Google Gemini LLM."""

from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from dotenv import load_dotenv
from fastapi import WebSocket
from loguru import logger

from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    InterimTranscriptionFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    OutputTransportMessageUrgentFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.elevenlabs.stt import CommitStrategy, ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from backend.database import get_dashboard_state, init_db
from backend.serializer import BrowserAudioSerializer
from backend.tools import (
    book_appointment,
    cancel_appointment,
    check_doctor_availability,
)

DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
# ElevenLabs "Sarah - Mature, Reassuring, Confident" pre-made voice
DEFAULT_ELEVENLABS_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"
DEFAULT_SAMPLE_RATE = 16000


def build_system_instruction(reference_date: date | None = None) -> str:
    """Build strict receptionist system instructions for the Gemini LLM."""
    today = reference_date or date.today()
    tomorrow = today + timedelta(days=1)
    return (
        "You are Aria, a warm, polite, and efficient voice receptionist at MedCare Clinic. "
        f"Today's date is {today.isoformat()} ({today.strftime('%A, %B %d, %Y')}), and tomorrow is "
        f"{tomorrow.isoformat()}.\n\n"
        "You help callers in real time on a phone call to:\n"
        "1. Check doctor availability (`check_doctor_availability`).\n"
        "2. Book an appointment (`book_appointment`).\n"
        "3. Cancel an existing appointment (`cancel_appointment`).\n\n"
        "STRICT RULES YOU MUST ALWAYS FOLLOW:\n"
        "- PHONE-ONLY IDENTIFICATION: Identify the caller exclusively by the phone number they say out loud. "
        "NEVER ask for a login, password, email address, OTP, verification code, patient ID, or caller-ID.\n"
        "- MANDATORY NUMBER READ-BACK CONFIRMATION: Whenever the caller gives a phone number for booking or "
        "cancelling an appointment, you MUST read the phone number back clearly to the caller and ask them to "
        "confirm it (for example: 'Just to confirm, your phone number is 9-8-7-6-5-4-3-2-1-0, is that correct?') "
        "and WAIT for their confirmation BEFORE calling `book_appointment` or `cancel_appointment`.\n"
        "- END-TO-END SINGLE CONVERSATION: Handle availability checks, slot selection, phone confirmation, "
        "booking, and cancellation smoothly in one continuous conversation. Do not end the call until the "
        "caller confirms they have nothing else to do.\n"
        "- CONCISE SPOKEN STYLE: Speak naturally like a real phone receptionist. Keep every response brief "
        "(1 to 3 short sentences), clear, and easy to hear. Never output markdown, bullet lists, asterisks, "
        "hashes, or URLs. When reading times aloud, use natural 12-hour format like '10:30 AM'.\n"
        "- ACCURACY WITH TOOLS: Always call `check_doctor_availability` before suggesting open slots so you "
        "only offer genuinely available times. Never invent doctors, dates, or confirmation numbers."
    )


def build_tools_schema() -> ToolsSchema:
    """Create Pipecat FunctionSchema definitions for the three clinic tools."""
    check_availability_schema = FunctionSchema(
        name="check_doctor_availability",
        description=(
            "Check open appointment slots for a clinic doctor, medical specialty, and/or date. "
            "Call this before offering times to the caller."
        ),
        properties={
            "doctor_name": {
                "type": "string",
                "description": "Optional full or partial doctor name, e.g. 'Dr. Ananya Sharma' or 'Sharma'.",
            },
            "date": {
                "type": "string",
                "description": "Optional date such as 'today', 'tomorrow', or 'YYYY-MM-DD'.",
            },
            "specialty": {
                "type": "string",
                "description": "Optional medical specialty such as 'Cardiology', 'Dermatology', 'Pediatrics', 'Orthopedics', or 'General Medicine'.",
            },
        },
        required=[],
    )

    book_appointment_schema = FunctionSchema(
        name="book_appointment",
        description=(
            "Book an available appointment slot for the caller using ONLY their spoken phone number. "
            "Only call this AFTER reading the phone number back to the caller and receiving their confirmation."
        ),
        properties={
            "phone_number": {
                "type": "string",
                "description": "Caller's confirmed spoken phone number (digits only or formatted).",
            },
            "doctor_name": {
                "type": "string",
                "description": "Doctor's name, e.g. 'Dr. Ananya Sharma' or 'Sharma'.",
            },
            "date": {
                "type": "string",
                "description": "Appointment date ('today', 'tomorrow', or 'YYYY-MM-DD').",
            },
            "time": {
                "type": "string",
                "description": "Selected slot time, e.g. '10:30 AM' or '14:30'.",
            },
            "reason": {
                "type": "string",
                "description": "Optional brief reason for the appointment.",
            },
        },
        required=["phone_number", "doctor_name", "date", "time"],
    )

    cancel_appointment_schema = FunctionSchema(
        name="cancel_appointment",
        description=(
            "Cancel an existing booked appointment using ONLY the caller's spoken phone number. "
            "Only call this AFTER reading the phone number back to the caller and receiving their confirmation."
        ),
        properties={
            "phone_number": {
                "type": "string",
                "description": "Caller's confirmed spoken phone number used when booking.",
            },
            "doctor_name": {
                "type": "string",
                "description": "Optional doctor name to disambiguate if the caller has multiple bookings.",
            },
            "date": {
                "type": "string",
                "description": "Optional appointment date to disambiguate if the caller has multiple bookings.",
            },
        },
        required=["phone_number"],
    )

    return ToolsSchema(
        standard_tools=[
            check_availability_schema,
            book_appointment_schema,
            cancel_appointment_schema,
        ]
    )


class VoiceEventObserver(BaseObserver):
    """Observe pipeline frames to emit live interim transcripts and speaking states to the UI."""

    def __init__(self, emit_event: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        super().__init__()
        self._emit_event = emit_event
        self._last_state: str | None = None

    async def _set_state(self, state: str) -> None:
        if state != self._last_state:
            self._last_state = state
            await self._emit_event({"type": "status", "state": state})

    async def on_push_frame(self, data: FramePushed) -> None:
        if not data.first_push:
            return

        frame = data.frame
        if isinstance(frame, InterimTranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                await self._emit_event(
                    {
                        "type": "transcript",
                        "role": "user",
                        "text": text,
                        "final": False,
                    }
                )
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            await self._set_state("listening")
        elif isinstance(frame, LLMFullResponseStartFrame):
            await self._set_state("thinking")
        elif isinstance(frame, BotStartedSpeakingFrame):
            await self._set_state("speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            await self._set_state("listening")
        elif isinstance(frame, ErrorFrame):
            err_text = str(getattr(frame, "error", "") or "Voice service error")
            if "429" in err_text or "RESOURCE_EXHAUSTED" in err_text:
                friendly = "Gemini API rate limit reached. Please wait or switch GEMINI_MODEL."
            else:
                friendly = err_text[:180]
            await self._emit_event({"type": "error", "message": friendly})


def register_appointment_tools(
    llm: GoogleLLMService,
    *,
    db_path: str | Path | None = None,
    emit_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_dashboard_update: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> None:
    """Register the three appointment tool handlers on the Pipecat LLM service."""

    async def _handle_check_availability(params: FunctionCallParams) -> None:
        args = dict(params.arguments or {})
        result = await asyncio.to_thread(
            check_doctor_availability,
            doctor_name=args.get("doctor_name"),
            date=args.get("date"),
            specialty=args.get("specialty"),
            db_path=db_path,
        )
        if emit_event:
            await emit_event(
                {
                    "type": "tool_call",
                    "name": "check_doctor_availability",
                    "arguments": args,
                    "result": result,
                }
            )
        await params.result_callback(result)

    async def _handle_book_appointment(params: FunctionCallParams) -> None:
        args = dict(params.arguments or {})
        result = await asyncio.to_thread(
            book_appointment,
            phone_number=str(args.get("phone_number", "")),
            doctor_name=str(args.get("doctor_name", "")),
            date=str(args.get("date", "")),
            time=str(args.get("time", "")),
            reason=str(args.get("reason", "") or ""),
            db_path=db_path,
        )
        dashboard_state = await asyncio.to_thread(get_dashboard_state, db_path)
        if emit_event:
            await emit_event(
                {
                    "type": "tool_call",
                    "name": "book_appointment",
                    "arguments": args,
                    "result": result,
                }
            )
            if result.get("success"):
                await emit_event(
                    {
                        "type": "db_update",
                        "action": "book_appointment",
                        "appointment": result.get("appointment"),
                        "dashboard": dashboard_state,
                    }
                )
        if result.get("success") and on_dashboard_update:
            await on_dashboard_update(
                {
                    "type": "db_update",
                    "action": "book_appointment",
                    "appointment": result.get("appointment"),
                    "dashboard": dashboard_state,
                }
            )
        await params.result_callback(result)

    async def _handle_cancel_appointment(params: FunctionCallParams) -> None:
        args = dict(params.arguments or {})
        result = await asyncio.to_thread(
            cancel_appointment,
            phone_number=str(args.get("phone_number", "")),
            doctor_name=args.get("doctor_name"),
            date=args.get("date"),
            db_path=db_path,
        )
        dashboard_state = await asyncio.to_thread(get_dashboard_state, db_path)
        if emit_event:
            await emit_event(
                {
                    "type": "tool_call",
                    "name": "cancel_appointment",
                    "arguments": args,
                    "result": result,
                }
            )
            if result.get("success"):
                await emit_event(
                    {
                        "type": "db_update",
                        "action": "cancel_appointment",
                        "appointment": result.get("cancelled_appointment"),
                        "dashboard": dashboard_state,
                    }
                )
        if result.get("success") and on_dashboard_update:
            await on_dashboard_update(
                {
                    "type": "db_update",
                    "action": "cancel_appointment",
                    "appointment": result.get("cancelled_appointment"),
                    "dashboard": dashboard_state,
                }
            )
        await params.result_callback(result)

    llm.register_function("check_doctor_availability", _handle_check_availability)
    llm.register_function("book_appointment", _handle_book_appointment)
    llm.register_function("cancel_appointment", _handle_cancel_appointment)


def _require_api_keys() -> tuple[str, str]:
    load_dotenv()
    elevenlabs_key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    gemini_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not elevenlabs_key:
        raise RuntimeError("Missing ELEVENLABS_API_KEY in environment or .env file.")
    if not gemini_key:
        raise RuntimeError("Missing GEMINI_API_KEY in environment or .env file.")
    return elevenlabs_key, gemini_key


def build_pipeline_services(
    *,
    db_path: str | Path | None = None,
    emit_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_dashboard_update: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    reference_date: date | None = None,
) -> dict[str, Any]:
    """Instantiate Silero VAD, ElevenLabs STT/TTS, Gemini LLM, tools, and aggregators."""
    elevenlabs_key, gemini_key = _require_api_keys()
    gemini_model = (os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip()
    voice_id = (os.getenv("ELEVENLABS_VOICE_ID") or DEFAULT_ELEVENLABS_VOICE_ID).strip()

    # Bonus: Silero VAD for low-latency speech detection and barge-in handling
    vad_analyzer = SileroVADAnalyzer(params=VADParams(stop_secs=0.6))

    stt = ElevenLabsRealtimeSTTService(
        api_key=elevenlabs_key,
        sample_rate=DEFAULT_SAMPLE_RATE,
        commit_strategy=CommitStrategy.MANUAL,
        settings=ElevenLabsRealtimeSTTService.Settings(
            model="scribe_v2_realtime",
            language="en",
        ),
    )

    llm = GoogleLLMService(
        api_key=gemini_key,
        settings=GoogleLLMService.Settings(
            model=gemini_model,
            temperature=0.4,
        ),
    )

    tts = ElevenLabsTTSService(
        api_key=elevenlabs_key,
        sample_rate=DEFAULT_SAMPLE_RATE,
        settings=ElevenLabsTTSService.Settings(
            voice=voice_id,
            model="eleven_flash_v2_5",
        ),
    )

    register_appointment_tools(
        llm,
        db_path=db_path,
        emit_event=emit_event,
        on_dashboard_update=on_dashboard_update,
    )

    tools_schema = build_tools_schema()
    context = LLMContext(
        messages=[
            {
                "role": "system",
                "content": build_system_instruction(reference_date=reference_date),
            }
        ],
        tools=tools_schema,
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=vad_analyzer),
    )

    return {
        "vad_analyzer": vad_analyzer,
        "stt": stt,
        "llm": llm,
        "tts": tts,
        "context": context,
        "tools_schema": tools_schema,
        "user_aggregator": user_aggregator,
        "assistant_aggregator": assistant_aggregator,
        "gemini_model": gemini_model,
        "voice_id": voice_id,
    }


async def run_voice_session(
    websocket: WebSocket,
    *,
    db_path: str | Path | None = None,
    on_dashboard_update: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> None:
    """Run a single real-time Pipecat voice call session over a FastAPI WebSocket."""
    init_db(db_path)

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=DEFAULT_SAMPLE_RATE,
            audio_out_sample_rate=DEFAULT_SAMPLE_RATE,
            serializer=BrowserAudioSerializer(sample_rate=DEFAULT_SAMPLE_RATE),
            allowed_origins=[],
        ),
    )

    task_holder: dict[str, PipelineTask | None] = {"task": None}

    async def emit_event(payload: dict[str, Any]) -> None:
        active_task = task_holder["task"]
        if active_task is not None:
            await active_task.queue_frames([OutputTransportMessageUrgentFrame(message=payload)])

    services = build_pipeline_services(
        db_path=db_path,
        emit_event=emit_event,
        on_dashboard_update=on_dashboard_update,
    )
    stt = services["stt"]
    llm = services["llm"]
    tts = services["tts"]
    user_aggregator = services["user_aggregator"]
    assistant_aggregator = services["assistant_aggregator"]

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    observer = VoiceEventObserver(emit_event)
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=DEFAULT_SAMPLE_RATE,
            audio_out_sample_rate=DEFAULT_SAMPLE_RATE,
            enable_metrics=True,
        ),
        observers=[observer],
    )
    task_holder["task"] = task

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport: Any, _ws: Any) -> None:
        logger.info("Voice caller connected; starting clinic greeting.")
        await emit_event({"type": "status", "state": "connected"})
        await task.queue_frames(
            [
                LLMMessagesAppendFrame(
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "A caller has just connected. Greet them warmly as Aria from "
                                "MedCare Clinic and ask how you can help with doctor availability, "
                                "booking, or cancelling an appointment."
                            ),
                        }
                    ],
                    run_llm=True,
                )
            ]
        )

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport: Any, _ws: Any) -> None:
        logger.info("Voice caller disconnected; stopping pipeline task.")
        await task.cancel()

    @user_aggregator.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(_aggregator: Any, _strategy: Any, message: Any) -> None:
        text = (getattr(message, "content", "") or "").strip()
        if text:
            await emit_event(
                {
                    "type": "transcript",
                    "role": "user",
                    "text": text,
                    "final": True,
                    "timestamp": getattr(message, "timestamp", ""),
                }
            )

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(_aggregator: Any, message: Any) -> None:
        text = (getattr(message, "content", "") or "").strip()
        if text:
            await emit_event(
                {
                    "type": "transcript",
                    "role": "assistant",
                    "text": text,
                    "final": True,
                    "timestamp": getattr(message, "timestamp", ""),
                }
            )

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
