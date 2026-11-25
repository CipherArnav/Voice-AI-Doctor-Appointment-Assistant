"""Unit tests for the mock SQLite database and core appointment tools."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from backend.database import get_dashboard_state, init_db
from backend.tools import (
    book_appointment,
    cancel_appointment,
    check_doctor_availability,
)


class AppointmentToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test_appointments.db"
        self.ref_date = date(2026, 11, 25)
        init_db(self.db_path, reference_date=self.ref_date)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_seed_populates_doctors_slots_and_dashboard_state(self) -> None:
        state = get_dashboard_state(self.db_path)
        self.assertEqual(len(state["doctors"]), 5)
        self.assertGreaterEqual(len(state["appointments"]), 2)
        for doc in state["doctors"]:
            self.assertGreater(len(doc["slots"]), 0)

    def test_check_doctor_availability_filters_by_doctor_and_date(self) -> None:
        result = check_doctor_availability(
            doctor_name="Sharma",
            date="today",
            db_path=self.db_path,
            reference_date=self.ref_date,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["filter_date"], "2026-11-25")
        self.assertEqual(len(result["doctors"]), 1)
        self.assertEqual(result["doctors"][0]["doctor_name"], "Dr. Ananya Sharma")
        times = [slot["time"] for slot in result["doctors"][0]["available_slots"]]
        self.assertIn("09:00", times)
        self.assertIn("10:30", times)

    def test_book_and_cancel_appointment_round_trip(self) -> None:
        # Book using formatted phone and 12h time
        booked = book_appointment(
            phone_number="987-654-3210",
            doctor_name="Vikram Mehta",
            date="2026-11-25",
            time="9:30 AM",
            reason="Skin rash",
            db_path=self.db_path,
            reference_date=self.ref_date,
        )
        self.assertTrue(booked["success"])
        self.assertEqual(booked["appointment"]["doctor_name"], "Dr. Vikram Mehta")
        self.assertEqual(booked["appointment"]["time"], "09:30")
        self.assertEqual(booked["appointment"]["phone_normalized"], "9876543210")

        # Slot is no longer available
        avail_after_book = check_doctor_availability(
            doctor_name="Mehta",
            date="2026-11-25",
            db_path=self.db_path,
            reference_date=self.ref_date,
        )
        open_times = [s["time"] for s in avail_after_book["doctors"][0]["available_slots"]]
        self.assertNotIn("09:30", open_times)

        # Double booking the same slot fails cleanly
        double_book = book_appointment(
            phone_number="9123456789",
            doctor_name="Mehta",
            date="2026-11-25",
            time="09:30",
            db_path=self.db_path,
            reference_date=self.ref_date,
        )
        self.assertFalse(double_book["success"])
        self.assertIn("already booked", double_book["error"])

        # Cancel using unformatted digits-only phone number
        cancelled = cancel_appointment(
            phone_number="9876543210",
            db_path=self.db_path,
            reference_date=self.ref_date,
        )
        self.assertTrue(cancelled["success"])
        self.assertEqual(cancelled["cancelled_appointment"]["status"], "cancelled")

        # Slot is available again after cancellation
        avail_after_cancel = check_doctor_availability(
            doctor_name="Mehta",
            date="2026-11-25",
            db_path=self.db_path,
            reference_date=self.ref_date,
        )
        restored_times = [s["time"] for s in avail_after_cancel["doctors"][0]["available_slots"]]
        self.assertIn("09:30", restored_times)

    def test_invalid_phone_and_missing_cancellation_handled(self) -> None:
        bad_book = book_appointment(
            phone_number="123",
            doctor_name="Sharma",
            date="today",
            time="09:00",
            db_path=self.db_path,
            reference_date=self.ref_date,
        )
        self.assertFalse(bad_book["success"])

        no_appt = cancel_appointment(
            phone_number="9998887776",
            db_path=self.db_path,
            reference_date=self.ref_date,
        )
        self.assertFalse(no_appt["success"])
        self.assertIn("No active appointment", no_appt["error"])


if __name__ == "__main__":
    unittest.main()
