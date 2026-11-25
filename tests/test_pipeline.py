"""Unit tests for the Pipecat voice pipeline setup, tools schema, and serializer."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from pipecat.frames.frames import (
    InputAudioRawFrame,
    InputTransportMessageFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageUrgentFrame,
)

from backend.pipeline import (
    build_pipeline_services,
    build_system_instruction,
    build_tools_schema,
)
from backend.serializer import BrowserAudioSerializer
from backend.server import app


class PipelineTests(unittest.TestCase):
    def test_system_instruction_enforces_strict_caller_rules(self) -> None:
        prompt = build_system_instruction(reference_date=date(2026, 11, 25))
        self.assertIn("2026-11-25", prompt)
        self.assertIn("exclusively by the phone number", prompt)
        self.assertIn("NEVER ask for a login", prompt)
        self.assertIn("OTP", prompt)
        self.assertIn("caller-ID", prompt)
        self.assertIn("read the phone number back", prompt)
        self.assertIn("check_doctor_availability", prompt)
        self.assertIn("book_appointment", prompt)
        self.assertIn("cancel_appointment", prompt)

    def test_tools_schema_registers_all_three_required_tools(self) -> None:
        schema = build_tools_schema()
        names = [tool.name for tool in schema.standard_tools]
        self.assertEqual(
            names,
            ["check_doctor_availability", "book_appointment", "cancel_appointment"],
        )

    def test_serializer_round_trip(self) -> None:
        async def _check() -> None:
            serializer = BrowserAudioSerializer(sample_rate=16000)
            pcm_in = b"\x01\x00\x02\x00\xff\x7f"
            frame_in = await serializer.deserialize(pcm_in)
            self.assertIsInstance(frame_in, InputAudioRawFrame)
            self.assertEqual(frame_in.audio, pcm_in)

            msg_in = await serializer.deserialize(json.dumps({"type": "ping"}))
            self.assertIsInstance(msg_in, InputTransportMessageFrame)
            self.assertEqual(msg_in.message, {"type": "ping"})

            pcm_out = await serializer.serialize(
                OutputAudioRawFrame(audio=pcm_in, sample_rate=16000, num_channels=1)
            )
            self.assertEqual(pcm_out, pcm_in)

            event_out = await serializer.serialize(
                OutputTransportMessageUrgentFrame(message={"type": "status", "state": "speaking"})
            )
            self.assertEqual(json.loads(event_out), {"type": "status", "state": "speaking"})

            interrupt_out = await serializer.serialize(InterruptionFrame())
            self.assertEqual(json.loads(interrupt_out), {"type": "interruption"})

        asyncio.run(_check())

    def test_build_pipeline_services_with_silero_vad_and_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "pipeline_test.db"
            services = build_pipeline_services(db_path=db_path)
            self.assertIsNotNone(services["vad_analyzer"])
            self.assertIsNotNone(services["stt"])
            self.assertIsNotNone(services["llm"])
            self.assertIsNotNone(services["tts"])
            llm = services["llm"]
            self.assertTrue(llm.has_function("check_doctor_availability"))
            self.assertTrue(llm.has_function("book_appointment"))
            self.assertTrue(llm.has_function("cancel_appointment"))

    def test_fastapi_health_and_dashboard_endpoints(self) -> None:
        client = TestClient(app)
        health_resp = client.get("/api/health")
        self.assertEqual(health_resp.status_code, 200)
        health_data = health_resp.json()
        self.assertEqual(health_data["status"], "ok")
        self.assertEqual(health_data["pipeline"]["vad"], "silero")
        self.assertTrue(health_data["keys_configured"]["elevenlabs"])
        self.assertTrue(health_data["keys_configured"]["gemini"])

        dash_resp = client.get("/api/dashboard")
        self.assertEqual(dash_resp.status_code, 200)
        dash_data = dash_resp.json()
        self.assertEqual(len(dash_data["doctors"]), 5)

        index_resp = client.get("/")
        self.assertEqual(index_resp.status_code, 200)
        self.assertIn("MedCare Clinic", index_resp.text)
        self.assertIn("Try Call", index_resp.text)
        self.assertEqual(client.get("/styles.css").status_code, 200)
        self.assertEqual(client.get("/app.js").status_code, 200)


if __name__ == "__main__":
    unittest.main()
