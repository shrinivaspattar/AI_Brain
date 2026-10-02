#!/usr/bin/env python3
"""Experiment 001: hybrid vs dense retrieval, over a SCRATCH database.

Arms (each produces a ranked list of chunks, collapsed to documents, and is
scored at document level against the approved queries' labels):
  dense                      pgvector cosine (nomic-embed-text), exact scan
  pg_fts                     Postgres full-text, OR-joined terms, ts_rank_cd (NOT BM25)
  bm25                       in-memory Okapi BM25 (rank_bm25) over the same chunks
  hybrid_dense_fts           RRF(dense, pg_fts)
  hybrid_dense_bm25          RRF(dense, bm25)
  hybrid_dense_bm25_rerank   RRF(dense, bm25)'s own candidate pool, reordered by a
                             cross-encoder (see RERANK_MODEL) scoring (query, chunk
                             text) pairs directly - added to evaluate reranking
                             (M33 follow-up) against the hybrid_dense_bm25 arm that
                             Experiment 001's original confirmation round adopted.

Reported: recall@10, mrr@10, ndcg@10 per arm, overall and per query type,
with percentile-bootstrap 95% CIs over queries, plus p50/p95 retrieval
latency. Latency = time to produce that arm's chunk ranking from a ready
query embedding (embedding time is measured separately and excluded; a
hybrid arm's latency is the sum of its two component retrievals + fusion;
the rerank arm's latency additionally includes cross-encoder scoring time
on top of hybrid_dense_bm25's own latency).

PRE-REGISTERED DECISION RULE (see exp001_common.decision_verdict): on the
primary metric (ndcg@10), a hybrid arm is adopted over dense only if the
lower CI bound of its paired lexical-subset gain exceeds --min-margin AND
its semantic-subset difference is not significantly negative. --min-margin
has no default: choose it before you see any results. The rerank arm is
judged by the same rule, against the same dense baseline, not against
hybrid_dense_bm25 - a separate, explicit comparison to hybrid_dense_bm25 is
the deciding factor for whether reranking is worth its latency/dependency
cost on top of what's already adopted (see report's extra section).

Read-only against the scratch database. Requires setup_fts.py to have run,
every chunk to have an embedding, `pip install -r scripts/exp001/requirements.txt`,
and Ollama running for query embeddings.

Usage:
    python scripts/exp001/run_experiment.py --queries q.jsonl \\
        --min-margin 0.05 --out-dir results/
"""

import argparse
import json
import time
from pathlib import Path

import exp001_common as common

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.embeddings.client import EmbeddingClient  # noqa: E402

ARMS = ("dense", "pg_fts", "bm25", "hybrid_dense_fts", "hybrid_dense_bm25", "hybrid_dense_bm25_rerank")
HYBRIDS = ("hybrid_dense_fts", "hybrid_dense_bm25", "hybrid_dense_bm25_rerank")

# A standard, small (~80MB) cross-encoder baseline for exactly this task
# (query/passage relevance scoring) - not fine-tuned, not swapped per-run;
# recorded in every results file like every other fixed hyperparameter here.
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


def _ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def load_corpus(conn):
    total = conn.execute(text("SELECT count(*) FROM document_chunks")).scalar()
    rows = conn.execute(
        text("SELECT id, document_id, content FROM document_chunks WHERE embedding IS NOT NULL ORDER BY id")
    ).all()
    if not rows or len(rows) != total:
        raise SystemExit(
            f"{total - len(rows)} of {total} chunks have no embedding (or the corpus is empty) - "
            "re-ingest into a fresh scratch database."
        )
    try:
        indexed = conn.execute(text("SELECT count(*) FROM exp001_chunk_fts")).scalar()
    except Exception as exc:
        raise SystemExit(f"exp001_chunk_fts missing - run setup_fts.py first ({exc})") from exc
    if indexed != total:
        raise SystemExit(f"exp001_chunk_fts has {indexed} rows but there are {total} chunks - re-run setup_fts.py")
    return rows


def run_queries(conn, rows, queries, pool, bm25_cls, reranker):
    chunk_ids = [r[0] for r in rows]
    chunk_to_doc = {r[0]: r[1] for r in rows}
    chunk_to_content = {r[0]: r[2] for r in rows}
    bm25 = bm25_cls([common.tokenize(r[2]) for r in rows])
    embedder = EmbeddingClient()

    known_docs = set(chunk_to_doc.values())
    for q in queries:
        unknown = set(q["relevant_doc_ids"]) - known_docs
        if unknown:
            raise SystemExit(f"query {q['id']}: relevant_doc_ids not in corpus: {sorted(unknown)}")

    records = []
    for q in queries:
        relevant = set(q["relevant_doc_ids"])

        t0 = time.perf_counter()
        vector = embedder.embed([q["query"]])[0]
        embed_ms = _ms(t0)
        vector_literal = "[" + ",".join(repr(float(x)) for x in vector) + "]"

        t0 = time.perf_counter()
        dense = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT id FROM document_chunks WHERE embedding IS NOT NULL "
                    "ORDER BY embedding <=> CAST(:qv AS vector), id LIMIT :pool"
                ),
                {"qv": vector_literal, "pool": pool},
            )
        ]
        dense_ms = _ms(t0)

        t0 = time.perf_counter()
        tsquery = common.or_tsquery_string(q["query"])
        fts: list[int] = []
        if tsquery:
            fts = [
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT chunk_id FROM exp001_chunk_fts "
                        "WHERE tsv @@ to_tsquery('english', :tq) "
                        "ORDER BY ts_rank_cd(tsv, to_tsquery('english', :tq)) DESC, chunk_id LIMIT :pool"
                    ),
                    {"tq": tsquery, "pool": pool},
                )
            ]
        fts_ms = _ms(t0)

        t0 = time.perf_counter()
        scores = bm25.get_scores(common.tokenize(q["query"]))
        order = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: (-scores[i], chunk_ids[i]))[:pool]
        bm25_ranked = [chunk_ids[i] for i in order]
        bm25_ms = _ms(t0)

        t0 = time.perf_counter()
        hyb_fts = common.rrf_fuse([dense, fts])
        hyb_fts_ms = _ms(t0) + dense_ms + fts_ms
        t0 = time.perf_counter()
        hyb_bm25 = common.rrf_fuse([dense, bm25_ranked])
        hyb_bm25_ms = _ms(t0) + dense_ms + bm25_ms

        # Reranks hybrid_dense_bm25's own fused pool (not a separately
        # retrieved pool) - isolates "does reordering the same candidates
        # help" from any difference a different candidate set would cause.
        t0 = time.perf_counter()
        rerank_pairs = [(q["query"], chunk_to_content[cid]) for cid in hyb_bm25]
        rerank_scores = reranker.predict(rerank_pairs) if rerank_pairs else []
        hyb_bm25_rerank = [
            cid for cid, _score in sorted(zip(hyb_bm25, rerank_scores), key=lambda pair: -pair[1])
        ]
        rerank_ms = _ms(t0) + hyb_bm25_ms

        per_arm = {
            "dense": (dense, dense_ms),
            "pg_fts": (fts, fts_ms),
            "bm25": (bm25_ranked, bm25_ms),
            "hybrid_dense_fts": (hyb_fts, hyb_fts_ms),
            "hybrid_dense_bm25": (hyb_bm25, hyb_bm25_ms),
            "hybrid_dense_bm25_rerank": (hyb_bm25_rerank, rerank_ms),
        }
        record = {"id": q["id"], "type": q["type"], "query": q["query"], "embed_ms": embed_ms, "arms": {}}
        for arm, (chunk_ranking, latency) in per_arm.items():
            docs = common.collapse_to_docs(chunk_ranking, chunk_to_doc)
            record["arms"][arm] = {
                "latency_ms": latency,
                "top_docs": docs[: common.EVAL_K],
                **{name: fn(docs, relevant) for name, fn in common.METRICS.items()},
            }
        records.append(record)
    return records


def aggregate(records, resamples, seed):
    summary = {}
    for arm in ARMS:
        summary[arm] = {}
        for scope in ("all", *common.QUERY_TYPES):
            subset = [r for r in records if scope == "all" or r["type"] == scope]
            entry = {"n": len(subset)}
            for metric in common.METRICS:
                entry[metric] = common.bootstrap_ci(
                    [r["arms"][arm][metric] for r in subset], resamples=resamples, seed=seed
                )
            latencies = [r["arms"][arm]["latency_ms"] for r in subset]
            entry["latency_p50_ms"] = common.percentile(latencies, 50)
            entry["latency_p95_ms"] = common.percentile(latencies, 95)
            summary[arm][scope] = entry
    return summary


def paired_verdicts(records, min_margin, resamples, seed):
    out = {}
    for arm in HYBRIDS:
        diffs = {}
        for scope in common.QUERY_TYPES:
            values = [
                r["arms"][arm][common.PRIMARY_METRIC] - r["arms"]["dense"][common.PRIMARY_METRIC]
                for r in records
                if r["type"] == scope
            ]
            diffs[scope] = common.bootstrap_ci(values, resamples=resamples, seed=seed)
        out[arm] = {
            "primary_metric": common.PRIMARY_METRIC,
            "paired_diff_vs_dense": diffs,
            "min_margin": min_margin,
            **common.decision_verdict(diffs["lexical"], diffs["semantic"], min_margin),
        }
    return out


def rerank_vs_adopted(records, resamples, seed):
    """The comparison that actually decides whether reranking is worth
    adding on top of what Experiment 001 already adopted: paired
    ndcg@10 difference of hybrid_dense_bm25_rerank over hybrid_dense_bm25
    itself (not over dense) - positive and clear of noise means reranking
    earns its latency/dependency cost; anything else means it doesn't."""
    diffs = {}
    for scope in ("all", *common.QUERY_TYPES):
        values = [
            r["arms"]["hybrid_dense_bm25_rerank"][common.PRIMARY_METRIC]
            - r["arms"]["hybrid_dense_bm25"][common.PRIMARY_METRIC]
            for r in records
            if scope == "all" or r["type"] == scope
        ]
        diffs[scope] = common.bootstrap_ci(values, resamples=resamples, seed=seed)
    return diffs


def fmt(ci):
    return f"{ci[0]:.3f} [{ci[1]:.3f}, {ci[2]:.3f}]"


def render_report(meta, summary, verdicts, rerank_deltas):
    lines = ["# Experiment 001 results", "", "## Run parameters", ""]
    lines += [f"- {k}: {v}" for k, v in meta.items()]
    for scope in ("all", *common.QUERY_TYPES):
        n = summary["dense"][scope]["n"]
        lines += ["", f"## Metrics - {scope} queries (n={n}); mean [95% bootstrap CI]", ""]
        lines.append("| arm | recall@10 | mrr@10 | ndcg@10 | p50 ms | p95 ms |")
        lines.append("|---|---|---|---|---|---|")
        for arm in ARMS:
            e = summary[arm][scope]
            lines.append(
                f"| {arm} | {fmt(e['recall@10'])} | {fmt(e['mrr@10'])} | {fmt(e['ndcg@10'])} | "
                f"{e['latency_p50_ms']:.1f} | {e['latency_p95_ms']:.1f} |"
            )
    lines += ["", f"## Pre-registered decision ({common.PRIMARY_METRIC}, paired vs dense)", ""]
    for arm, v in verdicts.items():
        lines.append(
            f"- **{arm}**: lexical diff {fmt(v['paired_diff_vs_dense']['lexical'])}, "
            f"semantic diff {fmt(v['paired_diff_vs_dense']['semantic'])}; "
            f"min_margin={v['min_margin']} -> lexical_gain={v['lexical_gain']}, "
            f"no_semantic_loss={v['no_semantic_loss']}, **adopt={v['adopt']}**"
        )
    lines += [
        "",
        f"## Reranking vs the already-adopted hybrid_dense_bm25 ({common.PRIMARY_METRIC}, paired)",
        "",
        "Not part of the pre-registered vs-dense rule above - this is the actual",
        "question for this follow-up: does reranking hybrid_dense_bm25's own",
        "candidates improve on it, enough to justify the added latency and the",
        "sentence-transformers/torch dependency.",
        "",
    ]
    for scope in ("all", *common.QUERY_TYPES):
        lines.append(f"- {scope}: {fmt(rerank_deltas[scope])}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=common.DEFAULT_SCRATCH_DB)
    parser.add_argument("--queries", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--min-margin", required=True, type=float)
    parser.add_argument("--pool", type=int, default=50,
                        help="chunk candidates per arm before fusion/collapse (default 50; recorded, not tuned)")
    parser.add_argument("--resamples", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    try:
        from rank_bm25 import BM25Okapi
    except ImportError as exc:
        raise SystemExit("rank_bm25 missing: pip install -r scripts/exp001/requirements.txt") from exc

    try:
        from sentence_transformers import CrossEncoder
    except ImportError as exc:
        raise SystemExit(
            "sentence-transformers missing: pip install -r scripts/exp001/requirements.txt"
        ) from exc
    # Built once, outside the per-query loop - a CrossEncoder is a real
    # model load (and the whole point of measuring latency per query is
    # the per-query scoring cost, not model-load cost).
    reranker = CrossEncoder(RERANK_MODEL)

    queries = common.load_approved_queries(args.queries)
    if not queries:
        raise SystemExit("no approved queries - review the drafted file and set approved=true")
    counts = {t: sum(1 for q in queries if q["type"] == t) for t in common.QUERY_TYPES}
    if not all(counts.values()):
        raise SystemExit(f"need approved queries of both types, have {counts}")

    engine = create_engine(common.scratch_url(args.database))
    with engine.connect() as conn:
        common.assert_connected_to_scratch(conn)
        rows = load_corpus(conn)
        pg_version = conn.execute(text("SHOW server_version")).scalar()
        records = run_queries(conn, rows, queries, args.pool, BM25Okapi, reranker)

    summary = aggregate(records, args.resamples, args.seed)
    verdicts = paired_verdicts(records, args.min_margin, args.resamples, args.seed)
    rerank_deltas = rerank_vs_adopted(records, args.resamples, args.seed)
    meta = {
        "database": args.database,
        "documents": len({r[1] for r in rows}),
        "chunks": len(rows),
        "queries": counts,
        "embedding_model": settings.EMBEDDING_MODEL,
        "postgres": pg_version,
        "rrf_k": common.RRF_K,
        "pool_per_arm": args.pool,
        "eval_k": common.EVAL_K,
        "primary_metric": common.PRIMARY_METRIC,
        "bootstrap_resamples": args.resamples,
        "seed": args.seed,
        "bm25_tokenizer": "lowercase [a-z0-9_]+, no stemming",
        "pg_fts": "to_tsquery('english', OR-joined terms), ts_rank_cd",
        "rerank_model": RERANK_MODEL,
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "results.json").write_text(
        json.dumps(
            {
                "meta": meta,
                "summary": summary,
                "verdicts": verdicts,
                "rerank_vs_adopted": rerank_deltas,
                "per_query": records,
            },
            indent=2,
        )
    )
    (args.out_dir / "report.md").write_text(render_report(meta, summary, verdicts, rerank_deltas))
    print(f"wrote {args.out_dir / 'report.md'} and results.json")


if __name__ == "__main__":
    main()
