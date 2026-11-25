"""SQLite mock database for doctors, specialties, time slots, and appointments."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "appointments.db"

SEED_DOCTORS: list[dict[str, str]] = [
    {
        "name": "Dr. Ananya Sharma",
        "specialty": "Cardiology",
        "room": "Suite 201",
    },
    {
        "name": "Dr. Vikram Mehta",
        "specialty": "Dermatology",
        "room": "Suite 104",
    },
    {
        "name": "Dr. Priya Nair",
        "specialty": "Pediatrics",
        "room": "Suite 112",
    },
    {
        "name": "Dr. Rohan Kapoor",
        "specialty": "Orthopedics",
        "room": "Suite 305",
    },
    {
        "name": "Dr. Meera Iyer",
        "specialty": "General Medicine",
        "room": "Suite 101",
    },
]

# Distinct daily slot schedules (24-hour HH:MM) per doctor index
DOCTOR_SLOT_TEMPLATES: list[list[str]] = [
    ["09:00", "10:30", "12:00", "14:30", "16:00"],
    ["09:30", "11:00", "13:30", "15:00", "16:30"],
    ["08:30", "10:00", "11:30", "14:00", "15:30"],
    ["10:00", "11:30", "14:00", "15:30", "17:00"],
    ["09:00", "10:00", "11:00", "14:00", "16:00", "17:30"],
]


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve the SQLite database path from argument, environment, or default."""
    if db_path is not None:
        return Path(db_path)
    env_path = os.getenv("APPOINTMENTS_DB_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


@contextmanager
def get_connection(
    db_path: str | Path | None = None,
    *,
    immediate: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Yield a configured SQLite connection and commit/rollback cleanly."""
    resolved = resolve_db_path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        conn.execute("BEGIN IMMEDIATE;" if immediate else "BEGIN;")
        yield conn
        if conn.in_transaction:
            conn.execute("COMMIT;")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()


def init_db(
    db_path: str | Path | None = None,
    *,
    seed: bool = True,
    reference_date: date | None = None,
) -> Path:
    """Create database tables if missing and seed mock doctors/slots when empty."""
    resolved = resolve_db_path(db_path)
    with get_connection(resolved, immediate=True) as conn:
        # executescript commits any pending transaction before executing DDL
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS doctors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                specialty TEXT NOT NULL,
                room TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doctor_id INTEGER NOT NULL,
                slot_date TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                is_booked INTEGER NOT NULL DEFAULT 0 CHECK (is_booked IN (0, 1)),
                FOREIGN KEY (doctor_id) REFERENCES doctors(id) ON DELETE CASCADE,
                UNIQUE (doctor_id, slot_date, slot_time)
            );

            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_id INTEGER NOT NULL,
                doctor_id INTEGER NOT NULL,
                caller_phone TEXT NOT NULL,
                phone_normalized TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'booked' CHECK (status IN ('booked', 'cancelled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (slot_id) REFERENCES slots(id) ON DELETE CASCADE,
                FOREIGN KEY (doctor_id) REFERENCES doctors(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_slots_doctor_date
                ON slots (doctor_id, slot_date, is_booked);

            CREATE INDEX IF NOT EXISTS idx_appointments_phone_status
                ON appointments (phone_normalized, status);
            """
        )

        if seed:
            conn.execute("BEGIN IMMEDIATE;")
            count = conn.execute("SELECT COUNT(*) FROM doctors;").fetchone()[0]
            if count == 0:
                _seed_mock_data(conn, reference_date or date.today())

    return resolved


def _seed_mock_data(conn: sqlite3.Connection, start_date: date) -> None:
    """Populate initial doctors, multi-day availability slots, and sample bookings."""
    doctor_ids: list[int] = []
    for doc in SEED_DOCTORS:
        cursor = conn.execute(
            "INSERT INTO doctors (name, specialty, room) VALUES (?, ?, ?);",
            (doc["name"], doc["specialty"], doc["room"]),
        )
        doctor_ids.append(int(cursor.lastrowid))

    # Seed 4 upcoming days of slots per doctor
    for idx, doc_id in enumerate(doctor_ids):
        times = DOCTOR_SLOT_TEMPLATES[idx % len(DOCTOR_SLOT_TEMPLATES)]
        for day_offset in range(4):
            slot_day = (start_date + timedelta(days=day_offset)).isoformat()
            for slot_time in times:
                conn.execute(
                    "INSERT INTO slots (doctor_id, slot_date, slot_time, is_booked) VALUES (?, ?, ?, 0);",
                    (doc_id, slot_day, slot_time),
                )

    # Seed two realistic sample appointments on day+1 so the dashboard has initial activity
    now_iso = datetime.now().isoformat(timespec="seconds")
    sample_bookings = [
        (doctor_ids[0], (start_date + timedelta(days=1)).isoformat(), "10:30", "555-010-1001", "5550101001", "Routine cardiac checkup"),
        (doctor_ids[4], (start_date + timedelta(days=1)).isoformat(), "11:00", "555-010-2002", "5550102002", "General consultation"),
    ]
    for doc_id, s_date, s_time, raw_phone, norm_phone, reason in sample_bookings:
        slot = conn.execute(
            "SELECT id FROM slots WHERE doctor_id = ? AND slot_date = ? AND slot_time = ?;",
            (doc_id, s_date, s_time),
        ).fetchone()
        if slot is not None:
            conn.execute("UPDATE slots SET is_booked = 1 WHERE id = ?;", (slot["id"],))
            conn.execute(
                """
                INSERT INTO appointments
                    (slot_id, doctor_id, caller_phone, phone_normalized, reason, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'booked', ?, ?);
                """,
                (slot["id"], doc_id, raw_phone, norm_phone, reason, now_iso, now_iso),
            )


def get_dashboard_state(db_path: str | Path | None = None) -> dict[str, Any]:
    """Return all doctors with their slots and all appointments for dashboard views."""
    resolved = init_db(db_path)
    with get_connection(resolved) as conn:
        doctors_rows = conn.execute(
            "SELECT id, name, specialty, room FROM doctors ORDER BY id;"
        ).fetchall()
        slots_rows = conn.execute(
            """
            SELECT id, doctor_id, slot_date, slot_time, is_booked
            FROM slots
            ORDER BY slot_date, slot_time;
            """
        ).fetchall()
        appt_rows = conn.execute(
            """
            SELECT
                a.id,
                a.slot_id,
                a.doctor_id,
                d.name AS doctor_name,
                d.specialty,
                d.room,
                s.slot_date,
                s.slot_time,
                a.caller_phone,
                a.phone_normalized,
                a.reason,
                a.status,
                a.created_at,
                a.updated_at
            FROM appointments a
            JOIN doctors d ON d.id = a.doctor_id
            JOIN slots s ON s.id = a.slot_id
            ORDER BY
                CASE WHEN a.status = 'booked' THEN 0 ELSE 1 END,
                s.slot_date,
                s.slot_time,
                a.id DESC;
            """
        ).fetchall()

    slots_by_doctor: dict[int, list[dict[str, Any]]] = {}
    for row in slots_rows:
        slots_by_doctor.setdefault(row["doctor_id"], []).append(
            {
                "id": row["id"],
                "date": row["slot_date"],
                "time": row["slot_time"],
                "is_booked": bool(row["is_booked"]),
            }
        )

    doctors = [
        {
            "id": row["id"],
            "name": row["name"],
            "specialty": row["specialty"],
            "room": row["room"],
            "slots": slots_by_doctor.get(row["id"], []),
        }
        for row in doctors_rows
    ]

    appointments = [
        {
            "id": row["id"],
            "slot_id": row["slot_id"],
            "doctor_id": row["doctor_id"],
            "doctor_name": row["doctor_name"],
            "specialty": row["specialty"],
            "room": row["room"],
            "date": row["slot_date"],
            "time": row["slot_time"],
            "caller_phone": row["caller_phone"],
            "phone_normalized": row["phone_normalized"],
            "reason": row["reason"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in appt_rows
    ]

    return {"doctors": doctors, "appointments": appointments}
