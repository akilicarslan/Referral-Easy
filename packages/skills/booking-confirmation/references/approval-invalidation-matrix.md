# Approval invalidation matrix

Read-only reference for `booking-confirmation`. Synthetic examples throughout.

An approval is bound to **patient + workflow + state version + payload hash + expiry**. It is valid
while all five match and it has not been consumed. Anything below is an invalidation, and the remedy
is always the same: show the new payload, ask again.

## What invalidates an approval

| Change | `invalidated_reason` | Why |
| --- | --- | --- |
| Different `slot_id` | `payload_changed` | The patient agreed to a time. A different time is a different offer. |
| Different `provider_id` or `hospital_id` | `payload_changed` | They agreed to a clinician and a building. |
| Different `visit_type` | `payload_changed` | First visit and return visit are different appointments with different requirements. |
| Different `document_ids` | `payload_changed` | The package the facility receives changed. |
| Different `contact_phone` | `payload_changed` | It is in the payload the patient reviewed. |
| Workflow advanced past the approved version | `state_version_superseded` | Consent was taken at a version; the workflow is no longer at it. |
| `expires_at` passed | `expired` | Consent is time-bounded by design. |
| Patient said they changed their mind | `patient_withdrew` | Withdrawal needs no justification and no second confirmation. |
| Approval already consumed by another booking request | (rejected as already consumed) | One approval, one booking. |

Reordering keys, dropping null-valued fields, or normalising whitespace does **not** invalidate:
the hash is computed over a canonical encoding with sorted keys, insignificant whitespace removed
and null-valued keys omitted. Cosmetic differences are not changes, and a payload that hashes the
same is the same booking.

## How each BookingState may be described

| State | May say | May never say |
| --- | --- | --- |
| `REQUESTED` | "We have sent your request to the practice." | anything containing "confirmed", "booked", "scheduled", "you're in" |
| `PENDING_REVIEW` | "The practice is reviewing your request." | anything containing "confirmed" or any date claimed as fixed |
| `CONFIRMED` | "The practice has confirmed your appointment for <date, in the facility's timezone>." | anything about cost or coverage |
| `REJECTED` | "The practice could not take this request." | "the appointment was cancelled" - nothing was booked to cancel |
| `CANCEL_PENDING` | "We have asked to cancel it; the practice has not confirmed the cancellation yet." | "it is cancelled" |
| `CANCELLED` | "The appointment is cancelled." | - |

## What transport responses mean

| Response | Booking state | Notes |
| --- | --- | --- |
| HTTP 200, body acknowledges receipt | `REQUESTED` | The request arrived. That is all. |
| HTTP 200, body contains a booking id | `REQUESTED` | An identifier is not a confirmation. |
| HTTP 200, body's state field says confirmed | `CONFIRMED` | Only with the state field, and only with the evidence stored. |
| HTTP 200, body is malformed or the state is unrecognisable | unknown | Outcome-unknown; hand to reconciliation. |
| HTTP 409, slot conflict | no booking | The approval is invalidated if the replacement payload differs. |
| HTTP 422 or a rejection body | no booking | `REJECTED` describes the facility's answer, not the transport. |
| Timeout, no response | unknown | The write may or may not have landed. Never describe as failed. |
| HTTP 5xx | unknown, unless the body says the write did not occur | A server error is not evidence about the booking. |

## The two sentences that matter

- *"Your appointment is confirmed"* requires a `CONFIRMED` state that came from the facility. Not a
  200, not an id, not a document, not a receipt.
- *"The booking failed"* requires evidence that no booking exists. A timeout is not that evidence;
  it means the question is unanswered.
