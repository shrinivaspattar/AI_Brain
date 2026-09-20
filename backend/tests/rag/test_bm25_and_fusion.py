from unittest.mock import MagicMock

import pytest

from app.rag.bm25_index import Bm25Index, Bm25IndexCache, tokenize
from app.rag.fusion import rrf_fuse


def test_tokenize_lowercases_and_keeps_identifiers():
    assert tokenize("Set max_connections=100; see pg_hba.conf") == [
        "set", "max_connections", "100", "see", "pg_hba", "conf",
    ]


def test_bm25_ranks_the_chunk_with_the_rare_term_first():
    index = Bm25Index(
        [10, 20, 30],
        ["how to add middleware to an app", "configure the flux_capacitor option here", "notes about the weather"],
    )
    assert index.search("flux_capacitor", limit=5)[0] == 20


def test_bm25_excludes_chunks_with_no_matching_word():
    index = Bm25Index([1, 2], ["alpha beta", "gamma delta"])
    assert index.search("alpha", limit=5) == [1]


def test_bm25_empty_query_or_empty_corpus_returns_nothing():
    assert Bm25Index([1], ["alpha"]).search("   ", limit=5) == []
    assert Bm25Index([], []).search("alpha", limit=5) == []
    assert len(Bm25Index([], [])) == 0


def test_bm25_respects_limit_and_breaks_ties_by_id():
    index = Bm25Index([7, 3, 5], ["match", "match", "match"])
    assert index.search("match", limit=2) == [3, 5]


def test_rrf_prefers_items_high_in_both_lists():
    fused = rrf_fuse([["a", "b", "c"], ["b", "a", "d"]])
    assert set(fused[:2]) == {"a", "b"}
    assert fused.index("d") > fused.index("a")


def test_rrf_single_list_keeps_order_and_is_deterministic():
    assert rrf_fuse([["x", "y", "z"]]) == ["x", "y", "z"]
    assert rrf_fuse([["a"], ["b"]]) == rrf_fuse([["a"], ["b"]]) == ["a", "b"]


def _db_with(signature_rows, rows):
    """A fake session: the first execute() gives (count, max id), the second
    gives the chunk rows."""
    db = MagicMock()
    db.get_bind.return_value.url.render_as_string.return_value = "postgresql://x/db"
    signature = MagicMock()
    signature.one.return_value = signature_rows
    content = MagicMock()
    content.all.return_value = rows
    db.execute.side_effect = [signature, content]
    return db


def test_cache_builds_once_then_reuses_while_signature_is_unchanged():
    cache = Bm25IndexCache()
    db1 = _db_with((2, 2), [(1, "alpha"), (2, "beta")])
    first = cache.get(db1)

    db2 = MagicMock()
    db2.get_bind.return_value.url.render_as_string.return_value = "postgresql://x/db"
    signature = MagicMock()
    signature.one.return_value = (2, 2)
    db2.execute.return_value = signature
    second = cache.get(db2)

    assert second is first
    assert db2.execute.call_count == 1  # only the cheap signature query, no rebuild


def test_cache_rebuilds_when_chunks_change():
    cache = Bm25IndexCache()
    first = cache.get(_db_with((1, 1), [(1, "alpha")]))
    second = cache.get(_db_with((2, 5), [(1, "alpha"), (5, "beta")]))
    assert second is not first
    assert second.search("beta", limit=3) == [5]


def test_cache_rebuilds_when_the_database_differs_even_if_counts_match():
    cache = Bm25IndexCache()
    first = cache.get(_db_with((1, 1), [(1, "alpha")]))
    other = _db_with((1, 1), [(1, "omega")])
    other.get_bind.return_value.url.render_as_string.return_value = "postgresql://x/other_db"
    second = cache.get(other)
    assert second is not first
    assert second.search("omega", limit=3) == [1]
