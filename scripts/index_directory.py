#!/usr/bin/env python3
"""`make index` — build the OpenSearch index the directory RAG retrieves from.

Indexing is the step most likely to look successful while doing nothing: an empty index answers
every query with zero hits and no error, which reads as "no matching directory entries" rather
than "the indexer never ran". So this script prints what it actually did — documents indexed,
vector dimensions, knowledge_version — and exits non-zero when it did nothing.

What it does:

1. loads the fixture corpus through the frozen contract types (a fixture that violates its
   schema stops the run rather than being chunked into a plausible-sounding document);
2. chunks it entity-first, so a provider's name and phone number stay in the same chunk;
3. embeds every chunk through the configured provider and asserts the width before writing;
4. creates the index if it is absent, bulk-indexes with deterministic ids, prunes documents from
   earlier runs, and reads the document count back from the cluster.

It is **idempotent**: ids are a hash of entity, chunker version and text, so a second run
overwrites its own documents instead of duplicating them.

`--write-seed` regenerates the committed seed corpus (the rung that answers without any network
call) from the same chunks, so the two can never describe different corpora.

Exit codes
    0  the index was built and the document count was verified
    1  the environment is not runnable (no credentials, no reachable OpenSearch)
    2  indexing started and failed (embedding error, dimension mismatch, bulk failure)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

# `packages/rag` is a workspace member, so `uv run` installs it and this line is redundant.
# It is kept as a provenance note (the script only ever uses this package's public API) and so
# the file can still be executed when the environment has not been re-synced after a change.
sys.path.insert(0, str(REPO_ROOT / "packages" / "rag" / "src"))

from referraleasy_rag.client import build_client, probe_opensearch  # noqa: E402
from referraleasy_rag.config import ConfigError, load_settings  # noqa: E402
from referraleasy_rag.degradation import CallLedger  # noqa: E402
from referraleasy_rag.embeddings import (  # noqa: E402
    EmbeddingError,
    build_embedding_provider,
)
from referraleasy_rag.indexing import IndexingError, index_directory  # noqa: E402
from referraleasy_rag.seed import build_seed_payload, serialize_seed  # noqa: E402

EXIT_OK = 0
EXIT_ENV_INVALID = 1
EXIT_INDEXING_FAILED = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the directory RAG index.")
    parser.add_argument(
        "--index",
        default=None,
        help="index name (default: DIRECTORY_INDEX_NAME or the built-in default)",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="delete and rebuild the index instead of updating it in place",
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="keep documents from earlier runs instead of pruning them",
    )
    parser.add_argument(
        "--write-seed",
        action="store_true",
        help="regenerate the committed seed corpus from the same chunks and exit",
    )
    parser.add_argument(
        "--print-seed",
        action="store_true",
        help="print the seed corpus to stdout instead of writing it (implies --write-seed)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print("referraleasy index_directory")
    print("=" * 68)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        return EXIT_ENV_INVALID

    print(f"environment        {settings.env_file}")
    print(f"fixtures           {settings.fixtures_dir}")
    print(f"embedding backend  {settings.embedding_backend} ({settings.embedding_provider})")
    if settings.embedding_provider != "openrouter" and not settings.embeddings_are_offline:
        print(
            "note               EMBEDDING_PROVIDER is pinned to "
            f"{settings.embedding_provider} by ADR-0006 but its credentials are unresolved, so "
            "the OpenRouter adapter is used for the same model family at the same width "
            "(see ADR-0029)."
        )

    if args.write_seed or args.print_seed:
        payload = build_seed_payload(settings)
        if args.print_seed:
            print(serialize_seed(payload), end="")
            return EXIT_OK
        target = settings.seed_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(serialize_seed(payload), encoding="utf-8")
        print(f"seed corpus        {target} ({payload['document_count']} documents)")
        print(f"knowledge_version  {payload['knowledge_version']}")
        return EXIT_OK

    probe = probe_opensearch(settings)
    print(f"openSearch         {settings.opensearch_url}")
    for line in probe.lines():
        print(line)
    if not probe.reachable:
        print(
            "\nSTOPPED: no OpenSearch node answered on any candidate URL. The index cannot be "
            "built, and an index that was not built answers every query with zero hits. "
            "Retrieval itself can still run on the seed corpus.",
            file=sys.stderr,
        )
        return EXIT_ENV_INVALID
    if probe.used_fallback:
        print(
            f"note               OPENSEARCH_URL from .env is the compose service name; "
            f"answered on {probe.url} instead (cluster {probe.cluster_name} {probe.version})."
        )

    ledger = CallLedger()
    try:
        embedder = build_embedding_provider(settings, ledger=ledger)
        client = build_client(settings, url=probe.url)
        messages: list[str] = []
        report = index_directory(
            settings,
            client=client,
            embedder=embedder,
            index_name=args.index,
            recreate=args.recreate,
            prune=not args.no_prune,
            ledger=ledger,
            reporter=messages,
        )
    except (EmbeddingError, IndexingError, ConfigError) as exc:
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Nothing may be read from an index that was not verified.", file=sys.stderr)
        return EXIT_INDEXING_FAILED

    for message in messages:
        print(f"chunking           {message}")
    print()
    for line in report.lines():
        print(line)
    print(
        f"model calls        {ledger.model_calls} text(s) over {ledger.requests} outbound "
        f"request(s) ({', '.join(sorted(set(ledger.kinds()))) or 'none'})"
    )
    print(
        f"\nOK: {report.documents_in_index} document(s) in '{report.index_name}'. "
        "Re-running this script is safe: chunk ids are deterministic, so a second run "
        "overwrites rather than duplicates."
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
