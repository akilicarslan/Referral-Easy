"""Referral Readiness's three tools (§3).

The forbidden capability this file exists to make structural is
**"cannot assume a document that does not exist"**:

* :func:`inspect_document` asks for one document by id. A document that is not
  there is a 404, which the transport raises as `UpstreamRejected` — there is no
  code path that returns an empty extraction for a missing file, because an empty
  extraction is indistinguishable from a readable-but-blank one and only one of
  those is a reason to ask the patient for a new copy.
* :func:`validate_referral_package` asks the readiness route, which answers per
  required kind. This tool does not compute `satisfied` itself — a locally computed
  verdict could disagree with the hospital's, and the hospital's is the one that
  decides whether a referral is accepted.

The second forbidden capability — "cannot grant admin/clinical approval" — is not a
tool here at all. There is no approval tool in this service's allowlist, so there is
nothing to refuse: `referral_readiness` may call three tools and no fourth
(`referraleasy_agent_runtime.roles`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from referraleasy_agents.canonical import id_text, id_texts
from referraleasy_agents.errors import UnknownDataError, UnreadableDocumentRefused
from referraleasy_agents.tools.call import ToolCall, require_patient_id, require_workflow_id
from referraleasy_agents.tools.intake import injection_detected
from referraleasy_contracts.generated.common import VisitType
from referraleasy_contracts.generated.coverage import ReferralRequirement
from referraleasy_contracts.generated.documents import ReadinessResult

__all__ = [
    "get_referral_requirements",
    "inspect_document",
    "require_documents",
    "validate_referral_package",
]


def require_documents(document_ids: Sequence[str]) -> None:
    """Refuse an empty document set, in one place.

    §3's forbidden capability for this agent is "cannot assume a document that does
    not exist". The first half of that refusal is here: a package with no documents
    is a package where *everything* is missing, and reporting it as "not checked"
    would hide exactly what the agent exists to say. The second half is
    :func:`inspect_document`: a document id that is not there is a 404, which the
    transport raises as `UpstreamRejected`, so no code path returns an empty
    extraction for a file nobody has.

    It is a module-level function rather than four inline lines because
    `forbidden.py` calls it directly: a §3 boundary should be callable by the code
    that asserts the boundary, not paraphrased in a second place.
    """
    if not document_ids:
        raise UnknownDataError(
            "validate_referral_package was called with no documents. An empty package is a package "
            "with everything missing, and reporting it as 'not checked' would hide that (§3)."
        )


async def get_referral_requirements(
    call: ToolCall, *, hospital_id: str, visit_type: str, plan_id: str | None = None
) -> dict[str, Any]:
    """Read one hospital's published requirements for one visit type.

    §4: these are not presented as settled before a hospital is chosen, which is why
    `hospital_id` is required and not defaulted. A plan-specific rule wins over the
    hospital-wide one, and which rule applied is reported per requirement rather than
    flattened — a caller that dropped `evidence_id` would make the patient's document
    list unauditable.
    """
    if visit_type not in {member.value for member in VisitType}:
        raise UnknownDataError(
            f"visit_type must be one of {sorted(member.value for member in VisitType)}, got "
            f"{visit_type!r}: first-visit and return-visit requirements are not interchangeable."
        )
    body = await call.api.get(
        "/referral-requirements",
        params={"hospital_id": hospital_id, "visit_type": visit_type, "plan_id": plan_id},
        budget=call.budget,
        lease=call.lease,
    )
    rows = body.get("requirements")
    if not isinstance(rows, list):
        raise UnknownDataError(
            "GET /referral-requirements answered without a `requirements` list; the hospital's "
            "requirements cannot be asserted from a response like that."
        )
    requirements = [ReferralRequirement.model_validate(row) for row in rows]
    required_kinds = sorted(
        {kind.value for requirement in requirements for kind in requirement.required_documents}
    )
    return {
        "hospital_id": hospital_id,
        "visit_type": visit_type,
        "plan_id": plan_id,
        "requirements": [requirement.model_dump(mode="json") for requirement in requirements],
        "required_documents": required_kinds,
        "count": len(requirements),
        "evidence_ids": [id_text(requirement.evidence_id) for requirement in requirements],
    }


async def inspect_document(call: ToolCall, *, document_id: str) -> dict[str, Any]:
    """Read one document's fields and say what could not be read.

    The text is returned because it *is* the data this agent must reason about, with
    the §9 rule applied: instruction-like content is flagged
    (`injection_attempt_detected`) and never followed. A blank body is an
    unreadable document, not an empty one — reported through the same
    `unreadable_fields` channel the rest of the platform uses.
    """
    body = await call.api.get(f"/documents/{document_id}", budget=call.budget, lease=call.lease)
    text = body.get("text")
    if not isinstance(text, str):
        raise UnknownDataError(
            f"document {document_id} came back without a text body; nothing about its contents can "
            "be asserted."
        )
    unreadable = [] if text.strip() else ["text"]
    if unreadable and not body.get("kind"):
        # Neither the body nor the document's own metadata identifies it: this is a
        # file that exists and cannot be read, which is a different instruction to
        # the patient than "please send this document".
        raise UnreadableDocumentRefused(
            f"document {document_id} is blank and carries no kind; it exists and cannot be read, so "
            "it cannot satisfy any requirement."
        )
    return {
        "document_id": document_id,
        "patient_id": body.get("patient_id"),
        "kind": body.get("kind"),
        "filename": body.get("filename"),
        "mime_type": body.get("mime_type"),
        "issued_at": body.get("issued_at"),
        "source_organization": body.get("source_organization"),
        "text": text,
        "char_count": len(text),
        "unreadable_fields": unreadable,
        "injection_attempt_detected": bool(
            body.get("contains_injection_attempt") or injection_detected(text)
        ),
        # `DocumentFixture` carries no `evidence_id`; the document id is the citation.
        "evidence_ids": [],
    }


async def validate_referral_package(
    call: ToolCall,
    *,
    hospital_id: str,
    visit_type: str,
    document_ids: Sequence[str],
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Certify a document set against one hospital's requirements.

    `satisfied` comes from the readiness route and is never recomputed here; the
    per-kind checks are returned as they arrived so a caller can see *which* kind
    failed instead of only that something did. `can_proceed` is passed through for
    the same reason: it is the hospital-facing verdict, and this agent's job is to
    report it accurately rather than to reinterpret it.

    The workflow and the patient are **not tool arguments**: the workflow comes from
    the task envelope and the patient from this role's own validated input
    (`require_workflow_id`, `require_patient_id`). The readiness route writes a
    certification against a (patient, hospital, visit type) set, and a model that
    could name the patient could certify one workflow's documents against another
    patient's record (§6).
    """
    require_documents(document_ids)
    body = await call.api.post(
        "/documents/check",
        {
            "workflow_id": require_workflow_id(call),
            "patient_id": require_patient_id(call),
            "hospital_id": hospital_id,
            "plan_id": plan_id,
            "visit_type": visit_type,
            "document_ids": id_texts(document_ids),
        },
        budget=call.budget,
        lease=call.lease,
    )
    readiness = ReadinessResult.model_validate(body)
    return {
        "readiness": readiness.model_dump(mode="json"),
        "checks": [check.model_dump(mode="json") for check in readiness.checks],
        "missing_kinds": [kind.value for kind in readiness.missing_kinds or []],
        "satisfied": readiness.satisfied,
        "can_proceed": readiness.can_proceed,
        "evidence_ids": id_texts(readiness.evidence_ids),
    }
