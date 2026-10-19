# ADR-0005 — Local guardrail classifier: ONNX sentence encoder + a *measured* threshold

- **Status:** Accepted (model and runtime artifacts pinned with hashes). The **threshold is
  UNRESOLVED** — `GUARDRAIL_LOCAL_THRESHOLD` stays `CHANGEME_MEASURED_THRESHOLD` until it is
  measured.
- **Date:** 2026-10-14
- **Phase:** 0 (spike S-05)
- **Decides:** `GUARDRAIL_LOCAL_MODEL`, the artifact acquisition + verification procedure,
  and the rule for recording `GUARDRAIL_LOCAL_THRESHOLD`.

## Question

What is a concrete CPU/ONNX sentence-encoder approach for the local (stage 1) guardrail —
the model, how it is obtained, its artifact hash — and how is the decision threshold
recorded?

## What was checked

| # | Command / URL | Result |
|---|---|---|
| 1 | `curl -s "https://huggingface.co/api/models/sentence-transformers/all-MiniLM-L6-v2"` | exists; `pipeline_tag: sentence-similarity`; repo revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`; last modified `2026-06-01` |
| 2 | `curl -s ".../api/models/sentence-transformers/all-MiniLM-L6-v2/tree/main/onnx?recursive=true"` | 9 ONNX variants; LFS SHA-256 available per file |
| 3 | `curl -sI "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model_qint8_avx512_vnni.onnx"` | `x-linked-etag` = SHA-256, cross-checked against the tree API `oid` |
| 4 | `curl -s https://pypi.org/pypi/onnxruntime/json` | `1.31.0` (`requires_python >=3.11`) |
| 5 | `curl -s https://pypi.org/pypi/tokenizers/json` | `0.23.3` |
| 6 | `curl -s "https://huggingface.co/api/models/protectai/deberta-v3-base-prompt-injection-v2"` | exists (considering as the alternative — see below) |

## Decision

### Artifact (pinned by content hash, not by name)

| Item | Value |
|---|---|
| Model | `sentence-transformers/all-MiniLM-L6-v2` |
| Repo revision | `1110a243fdf4706b3f48f1d95db1a4f5529b4d41` |
| ONNX graph | `onnx/model_qint8_avx512_vnni.onnx` |
| Size | 23,026,053 bytes |
| **SHA-256** | `4278337fd0ff3c68bfb6291042cad8ab363e1d9fbc43dcb499fe91c871902474` |
| Tokenizer | `tokenizer.json`, 466,247 bytes, git blob **SHA-1** `cb202bfe2e3c98645018a6d12f182a434c9d3e02` |
| Runtime | `onnxruntime==1.31.0`, `tokenizers==0.23.3` |

Two details that matter for honesty:

- **The quantised variants are byte-identical.** `model_qint8_arm64.onnx`,
  `model_qint8_avx512.onnx` and `model_qint8_avx512_vnni.onnx` all resolve to the **same LFS
  object id** — i.e. the same SHA-256. The filenames suggest different kernels; the bytes do
  not differ. So one hash verifies all three, and choosing between them is not a kernel
  choice at all.
- **`tokenizer.json` has no SHA-256 to quote.** It is a regular git blob, not LFS, so the
  only content identity the API exposes is the **SHA-1 git blob id** above. Presenting it as
  a SHA-256 would be wrong. The authoritative pin is the **repo revision**; the blob id is a
  secondary check.

### Acquisition

`snapshot_download(repo_id=..., revision=<sha>, allow_patterns=["onnx/model_qint8_avx512_vnni.onnx",
"tokenizer.json", "config.json", "1_Pooling/config.json"])` into a non-committed model
directory, then **verify the SHA-256 and refuse to load on mismatch**. The hash is the
contract; the download is ephemeral. Models are never committed (`.pre-commit-config.yaml`
already refuses files over 2 MB).

### How the decision is made

Stage 1 of `GUARDRAIL_MODE=three_stage`, and deliberately the dumbest stage: encode the
candidate text and a **versioned set of canonical attack and benign reference utterances**
(stored as a fixture, identified by its own hash), then decide on the **maximum cosine
similarity to the attack references**. Nearest-neighbour against explicit references — not a
learned classifier head, because the references are auditable and can be explained in a
demo, and a sentence encoder alone has no business pretending to be a classifier.

### How the threshold is recorded — the important part

`GUARDRAIL_LOCAL_THRESHOLD=CHANGEME_MEASURED_THRESHOLD` is resolved by **measurement only**,
never by intuition. A threshold value is accepted into this ADR only together with:

1. the labelled fixture set's revision and content hash,
2. the sample size (and its class balance),
3. the measured operating point — the confusion matrix at that threshold, not just "0.80
   felt right",
4. the false-positive rate, because a guardrail that blocks legitimate referrals is worse
   than the prompt injection it prevents.

**Until that measurement exists, the stage is inert and must say so.** It is not allowed to
apply an unmeasured default: the offline profile reports `synthetic: true` and
`guardrail_stage1: "threshold_unmeasured"` rather than emitting a verdict a viewer could
mistake for a real detection. A guessed threshold would be the exact failure mode the
project's "no fabricated demo output" invariant forbids.

## What remains unverified

1. **Detection quality is unknown.** `all-MiniLM-L6-v2` is a *general-purpose sentence
   encoder*, not a prompt-injection detector. Its recall on real injection attempts is
   unmeasured, and nearest-neighbour matching over a small reference set is the weakest part
   of this design. This is the main reason the alternative below is recorded rather than
   dismissed.
2. **int8 vs fp32 accuracy delta:** the quantised artifact is pinned for CPU latency, and
   the accuracy cost of that choice has not been measured against the fp32 export.
3. **Pooling and normalisation.** The export's graph may or may not include mean pooling
   (`1_Pooling/config.json` and `modules.json` describe the pipeline). Whether the ONNX graph
   output is already pooled, and whether embeddings must be L2-normalised before cosine
   similarity, must be checked against the actual graph — **not assumed**.
4. **`tokenizers==0.23.3` loading this `tokenizer.json` standalone** (without
   `transformers`, 5.19.0) is unverified.
5. **Latency** against `GUARDRAIL_TOTAL_TIMEOUT_MS=3000` — unmeasured.

### Alternative recorded, not adopted

`protectai/deberta-v3-base-prompt-injection-v2` (repo revision
`90c9989b1a342275dd0d1a95aad283c04e075671`, `pipeline_tag: text-classification`, ~690k
downloads, last modified 2026-07-09) is a purpose-built injection classifier and ships an
ONNX export. It is the honest upgrade path if the encoder's measured recall is inadequate —
a decision that can only be made **after** the threshold experiment above, using the same
labelled set.

## Consequences

- Stage 1 runs with zero network, zero credentials and a verifiable artifact — it can be
  demonstrated offline, which is exactly what Phase 0 needs.
- The threshold cannot accidentally ship as a made-up number: it has a documented intake
  process, and until it completes, the stage announces that it has no opinion.
