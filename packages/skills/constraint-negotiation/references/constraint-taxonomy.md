# Constraint taxonomy

Read-only reference for `constraint-negotiation`. The **Kind** column is a *default*: the binding
value is always the `kind` on the `Constraint` record, which a patient or operator may have set
differently. The record wins over this table.

| ConstraintField | Typical kind | Why | Relaxed by | Alternative kind |
| --- | --- | --- | --- | --- |
| `specialty` | hard | It is the referral. Changing it is a clinical decision, not a search setting | Clinician, via a new referral | no alternative - escalate |
| `insurance_plan` | hard | Determines whether a visit is covered at all | Patient, or payer | `different_provider`, `different_hospital` |
| `visit_type` | hard | Requirements are keyed by visit type | Patient, via clinician | no alternative - escalate |
| `max_distance_miles` | hard | Travel a patient has said they cannot do | Patient only | `wider_radius`, `different_hospital` |
| `accessibility` | hard | A stated need, not a preference | Patient only | `different_provider`, `different_hospital` |
| `provider_language` | hard | Care in a language the patient uses | Patient only | `different_provider`, `human_handoff` |
| `accepts_new_patients` | soft | Usually a practice fact, occasionally a patient ask | Patient, or refreshed directory data | `different_provider` |
| `preferred_day` | soft | Scheduling convenience | Patient only | `different_day` |
| `preferred_time_of_day` | soft | Scheduling convenience | Patient only | `different_time_of_day` |

## Reading the table

- **A soft constraint may still be treated as hard by the record.** A patient who says "I cannot do
  mornings because of work" has a preference that behaves like a constraint, and the right response
  is to record it as they described it, not to downgrade it because the field is usually soft.
- **A hard constraint is not a negotiation input when `relaxable: false`.** That combination means
  the boundary is fixed: the correct output is `no_match`, not a question the patient has already
  answered by setting the flag.
- **`source` matters.** `patient_stated` and `referral_document` are the patient's own words in
  different places; `system_mandatory` is set by the platform and is not relaxable by a patient
  decision at all; `inferred` is the weakest and must be confirmed before it is used to exclude
  anyone.

## Pricing an alternative

Every option carries its cost in the patient's units, not in the system's:

- `additional_distance_miles` for a radius or hospital change - **computed from coordinates**. The
  patient's `GeoPoint` and the candidate's `GeoPoint`, great-circle, rounded for display. Never
  read from `ProviderCandidate.rank` or a relevance score; a score orders results, it does not
  measure travel.
- `estimated_delay_days` for a day or time-of-day change - the difference between the earliest
  available slot under the current constraints and the earliest under the relaxed one. If no slot
  is available under either, there is no delay to quote and the option is not presented.

An option that cannot be priced in one of these units is not an `AlternativeOption`; it is an
intention. Do not present it.

## Presenting

Two to four options per decision. All of them attainable *now*, each backed by the candidates or
slots that make it real. The `presented_options` recorded on the `ConstraintChange` is the full
list shown - which is what makes the consent reviewable afterwards. Recording only the accepted
option discards the evidence that the choice was a choice.

Never present the same relaxation twice in one round under a different description. If there is only
one thing that can move, say so plainly and let the patient accept it or decline it.
