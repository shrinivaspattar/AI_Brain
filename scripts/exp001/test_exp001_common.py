"""Unit tests for the pure helpers in exp001_common. No database, no Ollama.

Run:  python -m pytest scripts/exp001/test_exp001_common.py
"""

import json
import math

import pytest

import exp001_common as c


def test_assert_scratch_db_rejects_production_and_test():
    for name in ("aibrain", "aibrain_test", "somethingelse", "aibrain_exp00"):
        with pytest.raises(SystemExit):
            c.assert_scratch_db(name)


def test_assert_scratch_db_accepts_scratch_names():
    c.assert_scratch_db("aibrain_exp001")
    c.assert_scratch_db("aibrain_exp001_second_run")


def test_recall_mrr_ndcg_perfect_and_miss():
    relevant = {"a"}
    assert c.recall_at_k(["a", "b"], relevant) == 1.0
    assert c.reciprocal_rank_at_k(["a", "b"], relevant) == 1.0
    assert c.ndcg_at_k(["a", "b"], relevant) == 1.0
    assert c.recall_at_k(["x", "y"], relevant) == 0.0
    assert c.reciprocal_rank_at_k(["x", "y"], relevant) == 0.0
    assert c.ndcg_at_k(["x", "y"], relevant) == 0.0


def test_mrr_uses_first_hit_position():
    assert c.reciprocal_rank_at_k(["x", "y", "a"], {"a"}) == pytest.approx(1 / 3)


def test_ndcg_rank_two_single_relevant():
    assert c.ndcg_at_k(["x", "a"], {"a"}) == pytest.approx(1 / math.log2(3))


def test_ndcg_ideal_normalisation_with_multiple_relevant():
    relevant = {"a", "b"}
    assert c.ndcg_at_k(["a", "b"], relevant) == pytest.approx(1.0)
    assert c.ndcg_at_k(["a", "x", "b"], relevant) < 1.0


def test_cutoff_is_respected():
    ranked = [f"x{i}" for i in range(10)] + ["a"]
    assert c.recall_at_k(ranked, {"a"}, k=10) == 0.0
    assert c.recall_at_k(ranked, {"a"}, k=11) == 1.0


def test_empty_relevant_set_scores_zero():
    assert c.recall_at_k(["a"], set()) == 0.0
    assert c.ndcg_at_k(["a"], set()) == 0.0


def test_rrf_prefers_items_ranked_high_in_both_lists():
    fused = c.rrf_fuse([["a", "b", "c"], ["b", "a", "d"]])
    assert fused[:2] == ["a", "b"] or fused[:2] == ["b", "a"]
    assert set(fused) == {"a", "b", "c", "d"}
    assert fused.index("d") > fused.index("a")


def test_rrf_score_matches_formula():
    fused = c.rrf_fuse([["a"], ["a"]], k=60)
    assert fused == ["a"]
    # single item present in both lists at rank 1: 2 / 61 - just check ordering
    two_lists = c.rrf_fuse([["a", "b"], ["b"]], k=60)
    assert two_lists[0] == "b"  # 1/62 + 1/61 beats 1/61


def test_rrf_is_deterministic_on_ties():
    first = c.rrf_fuse([["a"], ["b"]])
    second = c.rrf_fuse([["a"], ["b"]])
    assert first == second == ["a", "b"]


def test_collapse_to_docs_keeps_best_chunk_position():
    chunk_to_doc = {1: "d1", 2: "d2", 3: "d1", 4: "d3"}
    assert c.collapse_to_docs([2, 1, 3, 4], chunk_to_doc) == ["d2", "d1", "d3"]


def test_tokenize_keeps_identifiers():
    assert c.tokenize("Set max_connections=100; see pg_hba.conf") == [
        "set",
        "max_connections",
        "100",
        "see",
        "pg_hba",
        "conf",
    ]


def test_or_tsquery_string_dedupes_and_splits_underscores():
    assert c.or_tsquery_string("Foo foo BAR_baz!") == "foo | bar | baz"
    assert c.or_tsquery_string("!!!") == ""


def test_percentile_nearest_rank():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert c.percentile(values, 50) == 3.0
    assert c.percentile(values, 95) == 5.0
    assert math.isnan(c.percentile([], 50))


def test_bootstrap_ci_is_deterministic_and_brackets_mean():
    values = [0.0, 1.0, 1.0, 0.5, 0.25, 0.75, 1.0, 0.0]
    first = c.bootstrap_ci(values, resamples=500, seed=7)
    second = c.bootstrap_ci(values, resamples=500, seed=7)
    assert first == second
    mean, lower, upper = first
    assert lower <= mean <= upper


def test_bootstrap_ci_of_constant_values_is_degenerate():
    mean, lower, upper = c.bootstrap_ci([0.5] * 10, resamples=200, seed=1)
    assert mean == lower == upper == 0.5


def test_decision_verdict_requires_both_conditions():
    good = c.decision_verdict((0.2, 0.1, 0.3), (0.0, -0.05, 0.05), min_margin=0.05)
    assert good == {"lexical_gain": True, "no_semantic_loss": True, "adopt": True}

    no_gain = c.decision_verdict((0.2, 0.01, 0.3), (0.0, -0.05, 0.05), min_margin=0.05)
    assert no_gain["adopt"] is False and no_gain["lexical_gain"] is False

    semantic_loss = c.decision_verdict((0.2, 0.1, 0.3), (-0.2, -0.3, -0.1), min_margin=0.05)
    assert semantic_loss["adopt"] is False and semantic_loss["no_semantic_loss"] is False


def _write(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_load_approved_queries_filters_unapproved(tmp_path):
    path = tmp_path / "q.jsonl"
    _write(
        path,
        [
            {"id": "1", "type": "lexical", "query": "q1", "relevant_doc_ids": ["d1"], "approved": True},
            {"id": "2", "type": "semantic", "query": "q2", "relevant_doc_ids": ["d2"], "approved": False},
        ],
    )
    loaded = c.load_approved_queries(path)
    assert [q["id"] for q in loaded] == ["1"]


def test_load_approved_queries_validates(tmp_path):
    bad_type = tmp_path / "bad_type.jsonl"
    _write(bad_type, [{"id": "1", "type": "other", "query": "q", "relevant_doc_ids": ["d"], "approved": True}])
    with pytest.raises(ValueError):
        c.load_approved_queries(bad_type)

    no_docs = tmp_path / "no_docs.jsonl"
    _write(no_docs, [{"id": "1", "type": "lexical", "query": "q", "relevant_doc_ids": [], "approved": True}])
    with pytest.raises(ValueError):
        c.load_approved_queries(no_docs)

    missing_key = tmp_path / "missing.jsonl"
    _write(missing_key, [{"id": "1", "type": "lexical", "query": "q", "approved": True}])
    with pytest.raises(ValueError):
        c.load_approved_queries(missing_key)
