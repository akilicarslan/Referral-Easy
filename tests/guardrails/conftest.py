"""Fixtures for the guardrail tests.

Fixtures only. The shared doubles, corpus accessors and the ``sys.path`` bridge live in
``guardrail_fixtures.py``, and test modules must import them from there rather than from here —
importing from a ``conftest`` module by name is order-dependent and silently picks up another
directory's conftest. ``tests/test_test_layout.py`` enforces that, and the fixtures module explains
why at length.

Everything here builds a layer with *injected* collaborators: no fixture reaches a provider, a
database or an artifact on disk. The checkout's ``.env`` says ``MODEL_BACKEND=live``, so a fixture
that resolved layer 2 from the environment would spend money the first time a test ran.
"""

from __future__ import annotations

import pytest
from guardrail_fixtures import (
    StubOracle,
    TickClock,
    corpus,
    references,
)

from referraleasy_guardrails.corpus import LabelledCorpus, ReferenceSet
from referraleasy_guardrails.layer_classifier import ThresholdRecord, load_threshold_record
from referraleasy_guardrails.layer_regex import RegexGuardrail
from referraleasy_guardrails.output import OutputGuardrail


@pytest.fixture(scope="session")
def labelled_corpus() -> LabelledCorpus:
    return corpus()


@pytest.fixture(scope="session")
def reference_utterances() -> ReferenceSet:
    return references()


@pytest.fixture(scope="session")
def recorded_threshold() -> ThresholdRecord | None:
    """The measurement committed with the package, or None when nothing has been measured."""
    return load_threshold_record()


@pytest.fixture
def layer1() -> RegexGuardrail:
    """Layer 1 with the environment's message cap, since the cap is part of the layer's contract."""
    return RegexGuardrail(max_chars=2000)


@pytest.fixture
def clock() -> TickClock:
    return TickClock()


@pytest.fixture
def oracle() -> StubOracle:
    return StubOracle()


@pytest.fixture
def output_guardrail(oracle: StubOracle) -> OutputGuardrail:
    return OutputGuardrail(oracle)
