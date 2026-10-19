# Reason class playbook

Read-only reference for `handoff-triage`. One row per class in `HandoffReasonClass`. Pick the most
specific class that fits; `other` requires a populated `reason_detail`.

| Class | Triggered by | Summary must contain | Resolution may | Resolution may not |
| --- | --- | --- | --- | --- |
| `technical_outage` | A dependency is down or erroring | Which step could not complete, and that the referral itself is fine | Retry once the dependency is back | Describe the outage to the patient as a problem with their referral |
| `no_match` | Search ran correctly, nothing satisfied the constraints | The constraints that excluded everything, with the binding one named | Offer the options the automated path could not, or confirm that none exist | Relax a hard constraint, or present an approximate provider as a match |
| `missing_document` | A required document is absent, expired or unreadable | Which document, for which facility and visit type, and who can supply it | Accept the document when it arrives and re-run only the affected checks | Mark a requirement satisfied without the document |
| `conflicting_sources` | Two authoritative sources disagree | Both values with their evidence ids, and what each is authoritative for | Decide which source governs for this case, and record the reasoning | Present one value to the patient as though the other did not exist |
| `prolonged_unknown_booking` | Reconciliation budget exhausted, booking still unknown | That a booking request was sent, that its outcome is unknown, and what is being done | Query the facility's own system and record what it says | Re-book, mint a new idempotency key, or declare the booking failed or confirmed |
| `patient_requested` | The patient asked for a person | What the patient wants to discuss, in their words | Answer the question, or route to the right team | Treat the request as an opening to skip a step the workflow still requires |
| `no_reliable_recovery` | No automated path can recover this | Everything decided so far, and precisely what cannot be determined | Any recovery a person judges sound, within policy | Claim a resolution the evidence does not support |

## The boundary that applies to every row

**An operator resolving a ticket is not booking consent.** A human other than the patient cannot
grant approval for a booking. The `HumanQueueTicket` records `final_approval_still_required: true`
at creation, before anyone touches it, so that a resolution cannot silently drop the requirement.

In practice: an operator may resolve the *problem* - find the document, fix the outage, decide which
source governs - and return the workflow to `AWAITING_FINAL_APPROVAL` (or to the earlier state its
`return_state` names). The patient approves there. A workflow returned directly to
`BOOKING_IN_FLIGHT` on the strength of a resolved ticket is the failure this rule exists to prevent,
and the policy table's `return_state` allow-list is what bounds it structurally.

## Consent

`consent_given_at` is populated only after the patient reviews the summary and agrees. Until then:

- no queue entry,
- no sharing of the summary, the patient's name, or the referral detail,
- and the workflow stays where it is.

Consent is to share *this* summary. If the summary changes materially - a new conflicting value, a
different document - it is a new summary and it is shown again.

## Writing the summary

It is read by a patient, so: no `reason_code` values, no workflow state names, no upstream error
strings, no evidence identifiers. It says what happened, what is still open, and what the person
receiving it will do. It contains no clinical speculation and no promise about coverage or cost.

One summary, one purpose. If two problems need two people, that is two handoffs - not one summary
asking a single person to do two unrelated things.
