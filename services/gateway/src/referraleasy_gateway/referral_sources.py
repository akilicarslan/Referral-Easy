"""Load synthetic demo sources. The coordinator never derives facts from chat text."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def demo_sources(patient_ref: str) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[4] / "fixtures" / "patients"
    if not root.is_dir():
        return {}

    def record(name: str) -> dict[str, Any]:
        rows = json.loads((root / name).read_text())
        return next((row for row in rows if row.get("patient_id") == patient_ref), {})

    referral = record("referral_intake.json")
    preferences = record("preferences.json")
    details = record("patient_details.json")
    if not referral:
        return {}
    return {
        "referral": referral,
        "preferences": preferences,
        "patient_details_id": details.get("patient_details_id"),
    }
