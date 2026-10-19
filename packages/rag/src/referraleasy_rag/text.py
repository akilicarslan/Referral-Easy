"""Text preprocessing shared by documents and queries.

The pipeline requires that documents and queries are embedded with the *same* model, the same
dimensions and the same preprocessing. This module is the single implementation of the third
of those, and :data:`PREPROCESSING_VERSION` names it so the index can record what produced it.

It is intentionally lossless for the facts that ground an answer: phone numbers, names and
identifiers survive normalization untouched. An aggressive normalizer that "improves" recall
while dropping a plus sign from a phone number makes the retriever worse, not better.
"""

from __future__ import annotations

import re

#: Bump when :func:`normalize_text` changes meaning. Recorded on every indexed document.
PREPROCESSING_VERSION = "text-norm-1"

_WHITESPACE = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9]+")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?;])\s+")

_SUFFIXES = ("ies", "ist", "ian", "ing", "ics", "ed", "ly", "al", "ic", "s")


def normalize_text(text: str) -> str:
    """Collapse runs of whitespace and trim.

    Applied identically to document chunks and to queries before they reach the embedding
    provider, so a document embedded through one path can never be compared against a query
    embedded through another.
    """
    return _WHITESPACE.sub(" ", text).strip()


def split_sentences(text: str) -> list[str]:
    """Split prose into sentence-sized atoms. Atoms are never split again downstream."""
    return [part.strip() for part in _SENTENCE_BOUNDARY.split(normalize_text(text)) if part.strip()]


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, digits kept (``+1 312 555 0110`` -> ``13125550110``)."""
    return _WORD.findall(text.lower())


def stem(token: str) -> str:
    """A deliberately crude suffix stripper for the lexical seed fallback.

    It exists so that "cardiologist" and "cardiology" match in a rung that may not call a
    model at all. It is not a linguistic stemmer and is not used anywhere embeddings are
    available — the vector arm already handles morphology far better, and an over-eager stem
    on a phone number or an id would destroy an exact match.
    """
    if not token.isalpha() or len(token) <= 5:
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            base = token[: -len(suffix)]
            if suffix == "ies":
                return base + "y"
            return base
    return token


def estimate_tokens(text: str) -> int:
    """Rough token count for budget checks.

    ~4 characters per token. It is an estimate, and it is used only to decide where to cut a
    chunk, never to assert that something fits inside a model's context window: the hard limit
    is checked against the real 2048-token maximum (ADR-0006) with the same estimate plus a
    safety factor, and the chunker refuses to emit an atom that is larger than the limit.
    """
    return max(1, (len(text) + 3) // 4)


def contains_term(haystack: str, needle: str) -> bool:
    """Case-insensitive substring test used to avoid printing a name twice in one chunk."""
    return needle.strip().lower() in haystack.lower()
