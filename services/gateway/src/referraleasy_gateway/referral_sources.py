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
    documents_file = root.parent / "documents" / "documents.json"
    documents = (
        [
            {
                key: row.get(key)
                for key in (
                    "document_id",
                    "kind",
                    "filename",
                    "issued_at",
                    "synthetic",
                    "contains_injection_attempt",
                )
            }
            for row in json.loads(documents_file.read_text())
            if row.get("patient_id") == patient_ref
        ]
        if documents_file.is_file()
        else []
    )
    preferences = record("preferences.json")
    details = record("patient_details.json")
    if not referral:
        return {}
    return {
        "referral": referral,
        "preferences": preferences,
        "patient_details_id": details.get("patient_details_id"),
        # The patient's own synthetic documents, offered when a hospital needs one the referral
        # did not include. Metadata only: the text stays with the document service.
        "patient_documents": documents,
    }
