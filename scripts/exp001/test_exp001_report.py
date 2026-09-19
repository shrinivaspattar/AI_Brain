"""Tests for the aggregation/report layer and the query-drafting reply parser,
using fabricated per-query records. No database, no Ollama, no rank_bm25.

Run:  python -m pytest scripts/exp001/test_exp001_report.py
"""

import exp001_common as common
import draft_queries
import run_experiment as run


def _record(qid, qtype, dense_ndcg, hybrid_ndcg):
    def arm(ndcg):
        return {"latency_ms": 1.0, "top_docs": [], "recall@10": ndcg, "mrr@10": ndcg, "ndcg@10": ndcg}

    arms = {name: arm(dense_ndcg) for name in run.ARMS}
    arms["hybrid_dense_fts"] = arm(hybrid_ndcg)
    arms["hybrid_dense_bm25"] = arm(hybrid_ndcg)
    return {"id": qid, "type": qtype, "query": "q", "embed_ms": 1.0, "arms": arms}


def _records(lexical_gain, semantic_gain, n=20):
    records = []
    for i in range(n):
        base = 0.4 + 0.01 * (i % 5)
        records.append(_record(f"l{i}", "lexical", base, base + lexical_gain))
        records.append(_record(f"s{i}", "semantic", base, base + semantic_gain))
    return records


def test_aggregate_counts_and_scopes():
    records = _records(0.0, 0.0, n=6)
    summary = run.aggregate(records, resamples=100, seed=0)
    assert set(summary) == set(run.ARMS)
    assert summary["dense"]["all"]["n"] == 12
    assert summary["dense"]["lexical"]["n"] == 6
    mean, lower, upper = summary["dense"]["all"]["ndcg@10"]
    assert lower <= mean <= upper


def test_verdict_adopts_when_lexical_gain_and_no_semantic_loss():
    records = _records(lexical_gain=0.2, semantic_gain=0.0)
    verdicts = run.paired_verdicts(records, min_margin=0.05, resamples=200, seed=0)
    assert verdicts["hybrid_dense_fts"]["adopt"] is True
    assert verdicts["hybrid_dense_bm25"]["adopt"] is True


def test_verdict_rejects_when_gain_below_margin():
    records = _records(lexical_gain=0.02, semantic_gain=0.0)
    verdicts = run.paired_verdicts(records, min_margin=0.05, resamples=200, seed=0)
    assert verdicts["hybrid_dense_fts"]["adopt"] is False


def test_verdict_rejects_on_semantic_loss():
    records = _records(lexical_gain=0.2, semantic_gain=-0.2)
    verdicts = run.paired_verdicts(records, min_margin=0.05, resamples=200, seed=0)
    assert verdicts["hybrid_dense_fts"]["adopt"] is False
    assert verdicts["hybrid_dense_fts"]["no_semantic_loss"] is False


def test_report_renders_all_sections():
    records = _records(0.1, 0.0, n=5)
    summary = run.aggregate(records, resamples=100, seed=0)
    verdicts = run.paired_verdicts(records, min_margin=0.05, resamples=100, seed=0)
    report = run.render_report({"database": "aibrain_exp001"}, summary, verdicts)
    assert "# Experiment 001 results" in report
    assert "hybrid_dense_bm25" in report
    assert "adopt=" in report
    for scope in ("all", "lexical", "semantic"):
        assert f"Metrics - {scope} queries" in report


def test_primary_metric_is_ndcg():
    assert common.PRIMARY_METRIC == "ndcg@10"


def test_parse_reply_accepts_clean_json():
    assert draft_queries.parse_reply('{"lexical": "a", "semantic": "b"}') == {"lexical": "a", "semantic": "b"}


def test_parse_reply_strips_think_blocks_and_prose():
    reply = '<think>hmm</think>Sure: {"lexical": "a", "semantic": "b"} done'
    assert draft_queries.parse_reply(reply) == {"lexical": "a", "semantic": "b"}


def test_parse_reply_rejects_bad_shapes():
    assert draft_queries.parse_reply("no json here") is None
    assert draft_queries.parse_reply('{"lexical": "a"}') is None
    assert draft_queries.parse_reply('{"lexical": 1, "semantic": 2}') is None
    assert draft_queries.parse_reply('{"lexical": "a", "semantic":') is None
