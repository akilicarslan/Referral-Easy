# Retrieval degradation ladder

Read-only reference for `directory-grounding`. Every level below `full` must produce a visible
`degraded` flag on the answer. The point of the ladder is that a patient can tell the difference
between "the directory says there is no such cardiologist" and "we could not search the directory".

| Level | What is still working | What the answer may claim | Must be labelled | Escalate |
| --- | --- | --- | --- | --- |
| `full` | Hybrid retrieval and rerank | Anything the retrieved material supports | no | no |
| `rerank_down` | Retrieval complete, ranking degraded | The full candidate set, with ordering described as approximate rather than meaningful | yes | no |
| `embeddings_down` | Lexical retrieval only | Candidates that match on terms actually present in the material; recall is knowingly incomplete | yes, including that recall is narrowed | no |
| `opensearch_down` | Deterministic fallback rather than a real search | A labelled fallback set only. Never "these are the providers in your area" | yes, prominently | no, unless the task needs complete coverage |
| `static_seed` | A fixed seed set, no model call | A labelled, deterministic starting point. Never coverage, never a ranked answer | yes, prominently | no |
| `no_source` | Nothing | Nothing. There is no grounding material | n/a | yes - `technical_outage`, reason class `no_reliable_recovery` if it persists |

## Reading the ladder

- **Degraded is not broken.** `rerank_down` and `embeddings_down` still produce grounded answers,
  which is why they do not escalate. What they must not do is present themselves as complete.
- **`static_seed` is deterministic on purpose.** It makes no model call, so it is safe when the
  model path is unavailable, and it is exactly as unrepresentative as a fixed list is. Saying so is
  part of using it.
- **`no_source` is the only hard stop.** With nothing retrieved there is nothing to attribute, and
  an unattributed provider answer is the thing this skill exists to prevent. Hand off rather than
  answer from memory.
- **Levels are not fine-grained quality scores.** `opensearch_down` is not "worse than
  `embeddings_down`"; they fail differently. `embeddings_down` loses recall, `opensearch_down` may
  lose the search entirely and fall back.

## The boundary this ladder protects

Two claims may never be made from directory material, at any level:

1. **Availability.** A free appointment is established by the availability check against the
   scheduling system, never by a directory field such as "accepting new patients".
2. **Booking confirmation.** Nothing in a directory establishes that an appointment exists, at any
   degradation level. A degraded retrieval cannot produce a confirmation because a complete one
   cannot either.

## What to say

Word the label in terms of the patient's question, not the infrastructure:

- Preferred: "Our provider directory search is running in a limited mode today, so this list may be
  missing some nearby practices."
- Not: "`degraded: embeddings_down`."

Keep it short, keep it honest, and keep the evidence list attached - a labelled degraded answer is
still an answer the patient can act on.
