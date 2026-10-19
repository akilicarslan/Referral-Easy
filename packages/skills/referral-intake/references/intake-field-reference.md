# Intake field reference

Read-only lookup for `referral-intake`. The **Source** column is the point of the table: it says
who is allowed to supply a value. A field that no permitted source supplied is a missing field, and
stays missing.

| Field | Type | Required | Source | Blocks |
| --- | --- | --- | --- | --- |
| `referral_id` | `Uuid` | yes | The referral record itself | everything - without it there is nothing to track |
| `patient_id` | `PatientId` | yes | Already resolved before this skill runs | everything |
| `specialty` | `string` | yes | Stated in the referral, or asked of the patient. **Never inferred** | coverage check, provider matching, readiness |
| `issued_at` | `IsoTimestamp` | yes | The referral document's issue date | staleness checks, document-age limits |
| `expires_at` | `IsoTimestamp` or null | no | The referral document | nothing directly; its absence is recorded, not assumed |
| `visit_type` | `first_visit` \| `return_visit` | yes | Stated in the referral, or asked of the patient | requirements lookup (requirements are per visit type) |
| `referred_by_provider` | `string` or null | no | The referral document | nothing; needs referral is captured here |
| `clinical_reason_text` | `string` or null | no | Quoted verbatim from the referral | nothing - it is quoted, not interpreted |
| `document_ids` | `DocumentId[]` | no | The intake pipeline | document readiness |
| `synthetic` | `true` | yes | The fixture loader | nothing; it is a provenance marker |

## What "missing" means

A field is missing when **no permitted source supplied it**. Two cases that look like a value but
are not:

- A value that only becomes available through normalization is still missing when the source said
  nothing. `"follow-up"` normalizes to `return_visit`; a referral that never mentions the visit
  type does not.
- A value carried over from a *previous* referral for the same patient is missing unless the
  current referral states it. A patient's likely specialty is not this referral's specialty.

## What "contradictory" means

Two permitted sources disagree on the same field. Report both with their evidence, and ask. Never
resolve by recency, by specificity, or by which value unblocks the workflow.

| Conflict | Reported as |
| --- | --- |
| Letter says cardiology, structured record says pulmonology | `conflicting_evidence` on `specialty` |
| `issued_at` later than `expires_at` | `ambiguous_input` on the referral's validity window |
| Letter says first visit, record says return visit | `conflicting_evidence` on `visit_type` |
| Referral names a patient who is not `patient_id` | `no_match` - stop, do not merge |

## Asking a clarifying question

One question. The one that unblocks the most. Ask in the patient's terms, not the contract's:

- Preferred: "Is this a first visit with this specialist, or a follow-up?"
- Not: "Please provide `visit_type` so the requirements lookup can run."

Question ordering when several fields block: `specialty`, then `insurance_plan`, then `visit_type`,
then location, then timing. A question about timing while `specialty` is unknown wastes the
patient's attention on a step that still cannot run afterwards.
