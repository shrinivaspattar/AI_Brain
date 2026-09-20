from __future__ import annotations

# Reciprocal Rank Fusion constant. 60 is the value from the RRF paper and the
# one used in the Experiment 001 measurements; it is an empirical heuristic,
# not a universal constant.
RRF_K = 60


def rrf_fuse(rankings: list[list], k: int = RRF_K) -> list:
    """Reciprocal Rank Fusion: score(item) = sum over lists of 1 / (k + rank),
    ranks 1-based. Deterministic tie-break: best single rank, then item."""
    scores: dict = {}
    best_rank: dict = {}
    for ranking in rankings:
        for position, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + position)
            best_rank[item] = min(best_rank.get(item, position), position)
    return sorted(scores, key=lambda item: (-scores[item], best_rank[item], str(item)))
