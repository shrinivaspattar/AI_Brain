#!/usr/bin/env python3
"""Experiment 001 step (decision D4): draft CANDIDATE queries for a human to
spot-check. Read-only on the database; calls the local chat model via Ollama.

For each randomly sampled chunk it asks the local model for two queries the
chunk answers - a 'lexical' one (built around an exact identifier/command/
option/error string that appears verbatim) and a 'semantic' one (a natural
paraphrase avoiding the passage's distinctive terms). Every candidate is
written with approved=false. NOTHING is used by run_experiment.py until you
review the file and set approved=true.

Known limits you should check while reviewing:
- Labels start as the single source document. Another document may answer
  the query too; add its id to relevant_doc_ids, otherwise recall is
  pessimistic for every arm.
- A query written from a passage tends to share its vocabulary. Reject or
  reword 'semantic' queries that still contain the passage's rare terms,
  and 'lexical' queries whose token isn't actually distinctive.

The sample size is deliberately required (no default): choose it, don't
inherit it.

Usage:
    python scripts/exp001/draft_queries.py --num-chunks 60 --out queries.jsonl
"""

import argparse
import json
import random
import re

import exp001_common as common

import ollama  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import settings  # noqa: E402

PROMPT = (
    "You are helping build a search-evaluation set. Below is one passage from "
    "technical documentation. Write two search queries for which this passage "
    "is a strong answer.\n"
    '1. "lexical": a short query built around an exact identifier, command, '
    "option name, function name or error string that appears VERBATIM in the passage.\n"
    '2. "semantic": a natural-language question a reader might ask WITHOUT using '
    "the passage's distinctive terms (paraphrase the idea). It must NOT contain "
    "any code identifier, class, function, option or parameter name from the passage.\n"
    'Return ONLY JSON: {"lexical": "...", "semantic": "..."}\n\n'
    "Passage:\n"
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def parse_reply(content: str) -> dict | None:
    content = _THINK_RE.sub("", content).strip()
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data.get("lexical"), str) or not isinstance(data.get("semantic"), str):
        return None
    return data


def excluded_chunk_ids(path: str) -> set[int]:
    """source_chunk_id values already used in an earlier query file, so a
    confirmation round samples passages the pilot never saw."""
    ids: set[int] = set()
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                ids.add(json.loads(line)["source_chunk_id"])
    return ids


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exclude-from", help="earlier queries .jsonl whose source chunks must not be reused")
    parser.add_argument("--database", default=common.DEFAULT_SCRATCH_DB)
    parser.add_argument("--num-chunks", required=True, type=int)
    parser.add_argument("--min-chars", type=int, default=300,
                        help="skip chunks shorter than this (default 300; a harness parameter, not tuned)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    engine = create_engine(common.scratch_url(args.database))
    with engine.connect() as conn:
        common.assert_connected_to_scratch(conn)
        rows = conn.execute(
            text(
                "SELECT id, document_id, content FROM document_chunks "
                "WHERE length(content) >= :m ORDER BY id"
            ),
            {"m": args.min_chars},
        ).all()

    if args.exclude_from:
        skip = excluded_chunk_ids(args.exclude_from)
        rows = [r for r in rows if r[0] not in skip]

    if len(rows) < args.num_chunks:
        raise SystemExit(f"only {len(rows)} eligible chunks, fewer than --num-chunks {args.num_chunks}")

    sample = random.Random(args.seed).sample(rows, args.num_chunks)
    client = ollama.Client(host=settings.OLLAMA_HOST)

    written = skipped = 0
    with open(args.out, "w") as fh:
        for chunk_id, document_id, content in sample:
            reply = client.chat(
                model=settings.CHAT_MODEL,
                messages=[{"role": "user", "content": PROMPT + content}],
                format="json",
                # Qwen3's hidden reasoning tokens dominate runtime on CPU-only
                # hardware and add nothing for this simple extraction task.
                think=False,
            )
            data = parse_reply(reply.message.content or "")
            if data is None:
                skipped += 1
                continue
            for query_type in common.QUERY_TYPES:
                fh.write(
                    json.dumps(
                        {
                            "id": f"c{chunk_id}-{query_type[:3]}",
                            "type": query_type,
                            "query": data[query_type].strip(),
                            "relevant_doc_ids": [document_id],
                            "source_chunk_id": chunk_id,
                            "approved": False,
                        }
                    )
                    + "\n"
                )
                written += 1

    print(f"wrote {written} candidate queries to {args.out} ({skipped} chunks skipped: unparseable reply)")
    print("Review the file: set approved=true on good queries, fix or delete the rest.")


if __name__ == "__main__":
    main()
