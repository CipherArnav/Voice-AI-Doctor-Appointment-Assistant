"""Core appointment tools exposed to the Voice AI pipeline and API."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.database import get_connection, init_db


def normalize_phone_number(phone_number: str) -> str:
    """Strip non-digit characters and validate length for caller identification."""
    digits = re.sub(r"\D", "", phone_number or "")
    if len(digits) < 7 or len(digits) > 15:
        raise ValueError(
            "Please provide a valid phone number containing between 7 and 15 digits."
        )
    return digits


def format_phone_display(phone_number: str, digits: str) -> str:
    """Return a clean human-readable phone representation."""
    cleaned = (phone_number or "").strip()
    if cleaned:
        return cleaned
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return digits


def parse_date_input(date_str: str, *, reference_date: date | None = None) -> str:
    """Parse common date inputs ('today', 'tomorrow', 'YYYY-MM-DD', 'Nov 25', etc.) to YYYY-MM-DD."""
    raw = (date_str or "").strip()
    if not raw:
        raise ValueError("Date cannot be empty.")

    today = reference_date or date.today()
    lowered = raw.lower()
    if lowered == "today":
        return today.isoformat()
    if lowered == "tomorrow":
        return (today + timedelta(days=1)).isoformat()

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue

    # Month-name formats with or without year
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", raw, flags=re.IGNORECASE)
    for fmt in ("%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue

    for fmt in ("%b %d", "%B %d", "%d %b", "%d %B"):
        try:
            parsed = datetime.strptime(cleaned, fmt).date().replace(year=today.year)
            return parsed.isoformat()
        except ValueError:
            continue

    raise ValueError(
        f"Could not understand date '{date_str}'. Please use YYYY-MM-DD (for example, {today.isoformat()})."
    )


def parse_time_input(time_str: str) -> str:
    """Parse 12-hour or 24-hour time strings into canonical 24-hour 'HH:MM'."""
    raw = (time_str or "").strip().upper()
    if not raw:
        raise ValueError("Time cannot be empty.")

    # Normalize spacing before AM/PM (e.g. '10:30AM' -> '10:30 AM')
    raw = re.sub(r"\s*(AM|PM)$", r" \1", raw).strip()

    for fmt in ("%H:%M", "%I:%M %p", "%I %p", "%H"):
        try:
            return datetime.strptime(raw, fmt).strftime("%H:%M")
        except ValueError:
            continue

    raise ValueError(
        f"Could not understand time '{time_str}'. Please use formats like '10:30 AM' or '14:30'."
    )


def format_time_12h(time_24h: str) -> str:
    """Format canonical 'HH:MM' time for natural speech output."""
    return datetime.strptime(time_24h, "%H:%M").strftime("%I:%M %p").lstrip("0")


def _find_doctors(
    conn: Any,
    doctor_name: str | None = None,
    specialty: str | None = None,
) -> list[Any]:
    """Find doctors matching optional name and/or specialty filters."""
    query = "SELECT id, name, specialty, room FROM doctors WHERE 1=1"
    params: list[Any] = []

    if doctor_name and doctor_name.strip():
        cleaned_name = re.sub(r"^dr\.?\s+", "", doctor_name.strip(), flags=re.IGNORECASE)
        query += " AND (LOWER(name) LIKE ? OR LOWER(name) LIKE ?)"
        params.extend([f"%{doctor_name.strip().lower()}%", f"%{cleaned_name.lower()}%"])

    if specialty and specialty.strip():
        query += " AND LOWER(specialty) LIKE ?"
        params.append(f"%{specialty.strip().lower()}%")

    query += " ORDER BY id;"
    return conn.execute(query, params).fetchall()


def check_doctor_availability(
    doctor_name: str | None = None,
    date: str | None = None,
    specialty: str | None = None,
    *,
    db_path: str | Path | None = None,
    reference_date: date | None = None,
) -> dict[str, Any]:
    """Return available appointment slots for a given doctor, specialty, and/or date.

    Args:
        doctor_name: Optional full or partial doctor name (e.g., "Dr. Ananya Sharma" or "Sharma").
        date: Optional target date (e.g., "2026-11-25", "today", "tomorrow").
        specialty: Optional medical specialty filter (e.g., "Cardiology").
        db_path: Optional override path to the SQLite database file.
        reference_date: Optional reference date for relative terms like "today".

    Returns:
        Dictionary containing availability status, matching doctors, and open slots.
    """
    resolved = init_db(db_path, reference_date=reference_date)

    parsed_date: str | None = None
    if date and date.strip():
        try:
            parsed_date = parse_date_input(date, reference_date=reference_date)
        except ValueError as exc:
            return {"success": False, "error": str(exc), "doctors": []}

    with get_connection(resolved) as conn:
        doctors = _find_doctors(conn, doctor_name=doctor_name, specialty=specialty)
        if not doctors:
            all_doctors = conn.execute(
                "SELECT name, specialty FROM doctors ORDER BY id;"
            ).fetchall()
            return {
                "success": False,
                "error": "No matching doctor was found.",
                "available_doctors": [
                    {"name": d["name"], "specialty": d["specialty"]} for d in all_doctors
                ],
                "doctors": [],
            }

        results: list[dict[str, Any]] = []
        total_open_slots = 0
        for doc in doctors:
            slot_query = """
                SELECT id, slot_date, slot_time
                FROM slots
                WHERE doctor_id = ? AND is_booked = 0
            """
            slot_params: list[Any] = [doc["id"]]
            if parsed_date:
                slot_query += " AND slot_date = ?"
                slot_params.append(parsed_date)
            slot_query += " ORDER BY slot_date, slot_time;"

            slot_rows = conn.execute(slot_query, slot_params).fetchall()
            slots = [
                {
                    "slot_id": row["id"],
                    "date": row["slot_date"],
                    "time": row["slot_time"],
                    "time_display": format_time_12h(row["slot_time"]),
                }
                for row in slot_rows
            ]
            total_open_slots += len(slots)
            results.append(
                {
                    "doctor_id": doc["id"],
                    "doctor_name": doc["name"],
                    "specialty": doc["specialty"],
                    "room": doc["room"],
                    "available_slots": slots,
                }
            )

    return {
        "success": True,
        "filter_date": parsed_date,
        "total_open_slots": total_open_slots,
        "doctors": results,
    }


def book_appointment(
    phone_number: str,
    doctor_name: str,
    date: str,
    time: str,
    reason: str = "",
    *,
    db_path: str | Path | None = None,
    reference_date: date | None = None,
) -> dict[str, Any]:
    """Book an available slot for a caller identified solely by their phone number.

    Args:
        phone_number: Caller's spoken phone number (e.g., "9876543210").
        doctor_name: Doctor's full or partial name (e.g., "Dr. Ananya Sharma" or "Sharma").
        date: Appointment date (e.g., "2026-11-25", "today", "tomorrow").
        time: Appointment time (e.g., "10:30 AM" or "10:30").
        reason: Optional brief reason for the visit.
        db_path: Optional override path to the SQLite database file.
        reference_date: Optional reference date for relative terms like "today".

    Returns:
        Dictionary indicating booking success and appointment confirmation details.
    """
    try:
        phone_normalized = normalize_phone_number(phone_number)
        parsed_date = parse_date_input(date, reference_date=reference_date)
        parsed_time = parse_time_input(time)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    resolved = init_db(db_path, reference_date=reference_date)
    caller_display = format_phone_display(phone_number, phone_normalized)

    with get_connection(resolved, immediate=True) as conn:
        doctors = _find_doctors(conn, doctor_name=doctor_name)
        if not doctors:
            all_doctors = conn.execute(
                "SELECT name, specialty FROM doctors ORDER BY id;"
            ).fetchall()
            return {
                "success": False,
                "error": f"No doctor matching '{doctor_name}' was found.",
                "available_doctors": [
                    {"name": d["name"], "specialty": d["specialty"]} for d in all_doctors
                ],
            }

        if len(doctors) > 1:
            return {
                "success": False,
                "error": f"Multiple doctors match '{doctor_name}'. Please specify the full name.",
                "matching_doctors": [
                    {"name": d["name"], "specialty": d["specialty"]} for d in doctors
                ],
            }

        doctor = doctors[0]
        slot = conn.execute(
            """
            SELECT id, is_booked
            FROM slots
            WHERE doctor_id = ? AND slot_date = ? AND slot_time = ?;
            """,
            (doctor["id"], parsed_date, parsed_time),
        ).fetchone()

        if slot is None:
            open_slots = conn.execute(
                """
                SELECT slot_date, slot_time
                FROM slots
                WHERE doctor_id = ? AND is_booked = 0 AND slot_date = ?
                ORDER BY slot_time;
                """,
                (doctor["id"], parsed_date),
            ).fetchall()
            return {
                "success": False,
                "error": (
                    f"{doctor['name']} does not have a slot at {format_time_12h(parsed_time)} "
                    f"on {parsed_date}."
                ),
                "available_slots_on_date": [
                    {
                        "date": row["slot_date"],
                        "time": row["slot_time"],
                        "time_display": format_time_12h(row["slot_time"]),
                    }
                    for row in open_slots
                ],
            }

        if slot["is_booked"]:
            open_slots = conn.execute(
                """
                SELECT slot_date, slot_time
                FROM slots
                WHERE doctor_id = ? AND is_booked = 0 AND slot_date = ?
                ORDER BY slot_time;
                """,
                (doctor["id"], parsed_date),
            ).fetchall()
            return {
                "success": False,
                "error": (
                    f"The {format_time_12h(parsed_time)} slot with {doctor['name']} "
                    f"on {parsed_date} is already booked."
                ),
                "available_slots_on_date": [
                    {
                        "date": row["slot_date"],
                        "time": row["slot_time"],
                        "time_display": format_time_12h(row["slot_time"]),
                    }
                    for row in open_slots
                ],
            }

        now_iso = datetime.now().isoformat(timespec="seconds")
        conn.execute("UPDATE slots SET is_booked = 1 WHERE id = ?;", (slot["id"],))
        cursor = conn.execute(
            """
            INSERT INTO appointments
                (slot_id, doctor_id, caller_phone, phone_normalized, reason, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'booked', ?, ?);
            """,
            (
                slot["id"],
                doctor["id"],
                caller_display,
                phone_normalized,
                (reason or "").strip(),
                now_iso,
                now_iso,
            ),
        )
        appointment_id = int(cursor.lastrowid)

    return {
        "success": True,
        "message": (
            f"Appointment #{appointment_id} confirmed with {doctor['name']} "
            f"({doctor['specialty']}) on {parsed_date} at {format_time_12h(parsed_time)} "
            f"for phone number {caller_display}."
        ),
        "appointment": {
            "id": appointment_id,
            "slot_id": slot["id"],
            "doctor_id": doctor["id"],
            "doctor_name": doctor["name"],
            "specialty": doctor["specialty"],
            "room": doctor["room"],
            "date": parsed_date,
            "time": parsed_time,
            "time_display": format_time_12h(parsed_time),
            "caller_phone": caller_display,
            "phone_normalized": phone_normalized,
            "reason": (reason or "").strip(),
            "status": "booked",
        },
    }


def cancel_appointment(
    phone_number: str,
    doctor_name: str | None = None,
    date: str | None = None,
    *,
    db_path: str | Path | None = None,
    reference_date: date | None = None,
) -> dict[str, Any]:
    """Cancel an existing booked appointment using the caller's phone number.

    Args:
        phone_number: Caller's spoken phone number used when booking.
        doctor_name: Optional doctor name to disambiguate if the caller has multiple bookings.
        date: Optional appointment date to disambiguate if the caller has multiple bookings.
        db_path: Optional override path to the SQLite database file.
        reference_date: Optional reference date for relative terms like "today".

    Returns:
        Dictionary confirming cancellation or detailing why cancellation could not proceed.
    """
    try:
        phone_normalized = normalize_phone_number(phone_number)
        parsed_date = (
            parse_date_input(date, reference_date=reference_date)
            if date and date.strip()
            else None
        )
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    resolved = init_db(db_path, reference_date=reference_date)

    with get_connection(resolved, immediate=True) as conn:
        query = """
            SELECT
                a.id,
                a.slot_id,
                a.doctor_id,
                d.name AS doctor_name,
                d.specialty,
                s.slot_date,
                s.slot_time,
                a.caller_phone,
                a.phone_normalized
            FROM appointments a
            JOIN doctors d ON d.id = a.doctor_id
            JOIN slots s ON s.id = a.slot_id
            WHERE a.phone_normalized = ? AND a.status = 'booked'
        """
        params: list[Any] = [phone_normalized]

        if doctor_name and doctor_name.strip():
            cleaned_name = re.sub(r"^dr\.?\s+", "", doctor_name.strip(), flags=re.IGNORECASE)
            query += " AND (LOWER(d.name) LIKE ? OR LOWER(d.name) LIKE ?)"
            params.extend([f"%{doctor_name.strip().lower()}%", f"%{cleaned_name.lower()}%"])

        if parsed_date:
            query += " AND s.slot_date = ?"
            params.append(parsed_date)

        query += " ORDER BY s.slot_date, s.slot_time, a.id;"
        matches = conn.execute(query, params).fetchall()

        if not matches:
            return {
                "success": False,
                "error": f"No active appointment found for phone number {phone_number}.",
            }

        if len(matches) > 1:
            return {
                "success": False,
                "error": (
                    "Multiple active appointments were found for this phone number. "
                    "Please specify which doctor or date to cancel."
                ),
                "matching_appointments": [
                    {
                        "appointment_id": row["id"],
                        "doctor_name": row["doctor_name"],
                        "specialty": row["specialty"],
                        "date": row["slot_date"],
                        "time": row["slot_time"],
                        "time_display": format_time_12h(row["slot_time"]),
                    }
                    for row in matches
                ],
            }

        target = matches[0]
        now_iso = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE appointments SET status = 'cancelled', updated_at = ? WHERE id = ?;",
            (now_iso, target["id"]),
        )
        conn.execute("UPDATE slots SET is_booked = 0 WHERE id = ?;", (target["slot_id"],))

    return {
        "success": True,
        "message": (
            f"Appointment #{target['id']} with {target['doctor_name']} "
            f"on {target['slot_date']} at {format_time_12h(target['slot_time'])} "
            f"has been cancelled."
        ),
        "cancelled_appointment": {
            "id": target["id"],
            "slot_id": target["slot_id"],
            "doctor_id": target["doctor_id"],
            "doctor_name": target["doctor_name"],
            "specialty": target["specialty"],
            "date": target["slot_date"],
            "time": target["slot_time"],
            "time_display": format_time_12h(target["slot_time"]),
            "caller_phone": target["caller_phone"],
            "phone_normalized": target["phone_normalized"],
            "status": "cancelled",
        },
    }
