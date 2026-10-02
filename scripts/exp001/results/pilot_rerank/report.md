# Experiment 001 results

## Run parameters

- database: aibrain_exp001
- documents: 154
- chunks: 1002
- queries: {'lexical': 51, 'semantic': 59}
- embedding_model: nomic-embed-text
- postgres: 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
- rrf_k: 60
- pool_per_arm: 50
- eval_k: 10
- primary_metric: ndcg@10
- bootstrap_resamples: 2000
- seed: 0
- bm25_tokenizer: lowercase [a-z0-9_]+, no stemming
- pg_fts: to_tsquery('english', OR-joined terms), ts_rank_cd
- rerank_model: cross-encoder/ms-marco-MiniLM-L-6-v2

## Metrics - all queries (n=110); mean [95% bootstrap CI]

| arm | recall@10 | mrr@10 | ndcg@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|
| dense | 0.982 [0.955, 1.000] | 0.796 [0.736, 0.852] | 0.842 [0.794, 0.887] | 8.3 | 12.1 |
| pg_fts | 0.918 [0.864, 0.964] | 0.642 [0.569, 0.715] | 0.709 [0.646, 0.774] | 11.1 | 24.7 |
| bm25 | 0.973 [0.936, 1.000] | 0.759 [0.696, 0.822] | 0.811 [0.760, 0.862] | 5.6 | 13.9 |
| hybrid_dense_fts | 0.964 [0.927, 0.991] | 0.775 [0.711, 0.833] | 0.822 [0.769, 0.871] | 19.1 | 38.0 |
| hybrid_dense_bm25 | 0.973 [0.945, 1.000] | 0.832 [0.778, 0.884] | 0.868 [0.824, 0.911] | 13.3 | 24.5 |
| hybrid_dense_bm25_rerank | 0.973 [0.945, 1.000] | 0.824 [0.766, 0.878] | 0.861 [0.813, 0.905] | 8120.5 | 10191.9 |

## Metrics - lexical queries (n=51); mean [95% bootstrap CI]

| arm | recall@10 | mrr@10 | ndcg@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|
| dense | 0.980 [0.941, 1.000] | 0.752 [0.668, 0.842] | 0.809 [0.741, 0.879] | 8.8 | 13.7 |
| pg_fts | 0.980 [0.941, 1.000] | 0.701 [0.607, 0.795] | 0.770 [0.695, 0.843] | 3.6 | 13.8 |
| bm25 | 1.000 [1.000, 1.000] | 0.774 [0.688, 0.858] | 0.829 [0.762, 0.893] | 1.4 | 5.2 |
| hybrid_dense_fts | 0.980 [0.941, 1.000] | 0.785 [0.698, 0.869] | 0.833 [0.761, 0.902] | 13.1 | 24.1 |
| hybrid_dense_bm25 | 0.980 [0.941, 1.000] | 0.830 [0.752, 0.908] | 0.868 [0.803, 0.930] | 10.4 | 19.9 |
| hybrid_dense_bm25_rerank | 0.980 [0.941, 1.000] | 0.807 [0.719, 0.891] | 0.850 [0.778, 0.916] | 6597.0 | 9501.5 |

## Metrics - semantic queries (n=59); mean [95% bootstrap CI]

| arm | recall@10 | mrr@10 | ndcg@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|
| dense | 0.983 [0.949, 1.000] | 0.834 [0.756, 0.907] | 0.871 [0.807, 0.928] | 7.7 | 11.4 |
| pg_fts | 0.864 [0.780, 0.949] | 0.591 [0.489, 0.690] | 0.657 [0.565, 0.745] | 14.1 | 31.6 |
| bm25 | 0.949 [0.881, 1.000] | 0.747 [0.650, 0.831] | 0.796 [0.713, 0.866] | 8.4 | 17.3 |
| hybrid_dense_fts | 0.949 [0.881, 1.000] | 0.767 [0.680, 0.849] | 0.812 [0.736, 0.881] | 22.8 | 40.2 |
| hybrid_dense_bm25 | 0.966 [0.915, 1.000] | 0.833 [0.754, 0.903] | 0.867 [0.800, 0.925] | 15.9 | 27.8 |
| hybrid_dense_bm25_rerank | 0.966 [0.915, 1.000] | 0.839 [0.762, 0.908] | 0.871 [0.805, 0.929] | 8653.1 | 10315.9 |

## Pre-registered decision (ndcg@10, paired vs dense)

- **hybrid_dense_fts**: lexical diff 0.024 [-0.041, 0.088], semantic diff -0.059 [-0.113, -0.007]; min_margin=0.02 -> lexical_gain=False, no_semantic_loss=False, **adopt=False**
- **hybrid_dense_bm25**: lexical diff 0.059 [0.025, 0.101], semantic diff -0.004 [-0.051, 0.041]; min_margin=0.02 -> lexical_gain=True, no_semantic_loss=True, **adopt=True**
- **hybrid_dense_bm25_rerank**: lexical diff 0.041 [-0.009, 0.092], semantic diff -0.001 [-0.048, 0.049]; min_margin=0.02 -> lexical_gain=False, no_semantic_loss=True, **adopt=False**

## Reranking vs the already-adopted hybrid_dense_bm25 (ndcg@10, paired)

Not part of the pre-registered vs-dense rule above - this is the actual
question for this follow-up: does reranking hybrid_dense_bm25's own
candidates improve on it, enough to justify the added latency and the
sentence-transformers/torch dependency.

- all: -0.007 [-0.041, 0.024]
- lexical: -0.018 [-0.064, 0.027]
- semantic: 0.003 [-0.044, 0.053]
