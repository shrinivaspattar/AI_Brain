"""Shared helpers for Experiment 001 (hybrid vs dense retrieval, M33).

Pure functions (metrics, RRF, bootstrap, tokenizing, the decision rule)
plus the scratch-database guard. Nothing here touches `app/`'s behavior
or any production data.

SAFETY: every experiment script builds its database URL through
`scratch_url()`, which refuses any database name that is not an
`aibrain_exp001*` scratch database - in particular `aibrain` and
`aibrain_test` can never be selected, even by typo.

Usage (prints the scratch database URL, for `DATABASE_URL=... alembic`):
    python scripts/exp001/exp001_common.py --print-url
"""

from __future__ import annotations

import importlib
import math
import pkgutil
import random
import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_SCRATCH_DB = "aibrain_exp001"
REQUIRED_DB_PREFIX = "aibrain_exp001"
FORBIDDEN_DB_NAMES = frozenset({"aibrain", "aibrain_test"})

# Evaluation cutoff fixed by the experiment design, not a tunable.
EVAL_K = 10
# Reciprocal Rank Fusion constant. 60 is the value from the RRF paper and a
# common default, but it is an empirical heuristic, not a universal
# constant - it is recorded in every results file.
RRF_K = 60
# The single metric the pre-registered decision rule is evaluated on, fixed
# before any results exist so the "best" of several metrics can't be picked
# after the fact. Recall and MRR are reported descriptively only.
PRIMARY_METRIC = "ndcg@10"

QUERY_TYPES = ("lexical", "semantic")


def assert_scratch_db(name: str) -> None:
    if name in FORBIDDEN_DB_NAMES or not name.startswith(REQUIRED_DB_PREFIX):
        raise SystemExit(
            f"Refusing database '{name}': Experiment 001 only ever touches scratch "
            f"databases named '{REQUIRED_DB_PREFIX}*', never aibrain or aibrain_test."
        )


def scratch_url(name: str = DEFAULT_SCRATCH_DB):
    assert_scratch_db(name)
    from sqlalchemy.engine import make_url

    from app.core.config import settings

    return make_url(settings.DATABASE_URL).set(database=name)


def assert_connected_to_scratch(conn) -> None:
    from sqlalchemy import text

    current = conn.execute(text("SELECT current_database()")).scalar()
    assert_scratch_db(current)


def import_all_models() -> None:
    """Register every SQLAlchemy model so cross-table foreign keys resolve
    (a model with an FK to an un-imported sibling raises NoReferencedTable-
    Error at flush time)."""
    import app.models

    for module in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module(f"app.models.{module.name}")


# --- tokenizing ---------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_QUERY_WORD_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric/underscore tokens, used for the in-memory BM25
    arm. No stemming, unlike Postgres's 'english' text-search config - that
    difference is part of what the experiment compares."""
    return _TOKEN_RE.findall(text.lower())


def or_tsquery_string(query: str) -> str:
    """OR-joined tsquery source for the Postgres FTS arm. plainto_tsquery
    ANDs every term, which returns almost nothing for a natural-language
    paraphrase query and would unfairly starve the FTS arm; OR semantics
    with ts_rank_cd ranking is the fairer lexical baseline. Returns "" when
    the query has no usable words."""
    seen: dict[str, None] = {}
    for word in _QUERY_WORD_RE.findall(query.lower()):
        seen.setdefault(word, None)
    return " | ".join(seen)


# --- ranking utilities --------------------------------------------------


def collapse_to_docs(chunk_ids: list, chunk_to_doc: dict) -> list:
    """Chunk ranking -> document ranking, keeping each document at the
    position of its best-ranked chunk."""
    seen: dict = {}
    for chunk_id in chunk_ids:
        seen.setdefault(chunk_to_doc[chunk_id], None)
    return list(seen)


def rrf_fuse(rankings: list[list], k: int = RRF_K) -> list:
    """Reciprocal Rank Fusion: score(d) = sum over lists of 1 / (k + rank),
    ranks 1-based. Deterministic tie-break: best single rank, then id."""
    scores: dict = {}
    best_rank: dict = {}
    for ranking in rankings:
        for position, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + position)
            best_rank[item] = min(best_rank.get(item, position), position)
    return sorted(scores, key=lambda item: (-scores[item], best_rank[item], str(item)))


# --- metrics (binary relevance, document level) --------------------------


def recall_at_k(ranked: list, relevant: set, k: int = EVAL_K) -> float:
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def reciprocal_rank_at_k(ranked: list, relevant: set, k: int = EVAL_K) -> float:
    for position, item in enumerate(ranked[:k], start=1):
        if item in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked: list, relevant: set, k: int = EVAL_K) -> float:
    if not relevant:
        return 0.0
    dcg = sum(1.0 / math.log2(i + 2) for i, item in enumerate(ranked[:k]) if item in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal


METRICS = {
    "recall@10": recall_at_k,
    "mrr@10": reciprocal_rank_at_k,
    "ndcg@10": ndcg_at_k,
}


# --- statistics ---------------------------------------------------------


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
    return ordered[index]


def bootstrap_ci(
    values: list[float], resamples: int = 2000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float, float]:
    """(mean, lower, upper) percentile bootstrap over per-query values."""
    if not values:
        nan = float("nan")
        return nan, nan, nan
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(resamples))
    lower = means[int(alpha / 2 * resamples)]
    upper = means[max(0, int((1 - alpha / 2) * resamples) - 1)]
    return sum(values) / n, lower, upper


def decision_verdict(
    lexical_diff_ci: tuple[float, float, float],
    semantic_diff_ci: tuple[float, float, float],
    min_margin: float,
) -> dict:
    """Pre-registered rule, evaluated on the primary metric's paired
    (hybrid - dense) per-query differences:

      lexical_gain      : lower CI bound on the LEXICAL subset > min_margin
      no_semantic_loss  : upper CI bound on the SEMANTIC subset >= 0
                          (i.e. the semantic difference is not significantly
                          negative)

    Adopt the hybrid arm only if both hold. `min_margin` has no default: the
    operator chooses it before running, so it cannot be tuned to the result."""
    lexical_gain = lexical_diff_ci[1] > min_margin
    no_semantic_loss = semantic_diff_ci[2] >= 0
    return {
        "lexical_gain": lexical_gain,
        "no_semantic_loss": no_semantic_loss,
        "adopt": bool(lexical_gain and no_semantic_loss),
    }


# --- query file ---------------------------------------------------------


def load_approved_queries(path: Path) -> list[dict]:
    """Reads a JSONL query file, returning only entries with approved=true.
    Each entry: id, type (lexical|semantic), query, relevant_doc_ids (list),
    approved (bool)."""
    import json

    queries = []
    with Path(path).open() as fh:
        for line_number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            for key in ("id", "type", "query", "relevant_doc_ids", "approved"):
                if key not in entry:
                    raise ValueError(f"{path}:{line_number} missing key '{key}'")
            if entry["type"] not in QUERY_TYPES:
                raise ValueError(f"{path}:{line_number} type must be one of {QUERY_TYPES}")
            if not entry["relevant_doc_ids"]:
                raise ValueError(f"{path}:{line_number} has no relevant_doc_ids")
            if entry["approved"] is True:
                queries.append(entry)
    return queries


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--print-url", action="store_true")
    parser.add_argument("--database", default=DEFAULT_SCRATCH_DB)
    args = parser.parse_args()
    if args.print_url:
        print(scratch_url(args.database).render_as_string(hide_password=False))
