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

## Metrics - all queries (n=110); mean [95% bootstrap CI]

| arm | recall@10 | mrr@10 | ndcg@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|
| dense | 0.982 [0.955, 1.000] | 0.796 [0.736, 0.852] | 0.842 [0.794, 0.887] | 3.0 | 3.6 |
| pg_fts | 0.918 [0.864, 0.964] | 0.642 [0.569, 0.715] | 0.709 [0.646, 0.774] | 3.4 | 10.0 |
| bm25 | 0.973 [0.936, 1.000] | 0.759 [0.696, 0.822] | 0.811 [0.760, 0.862] | 2.8 | 4.1 |
| hybrid_dense_fts | 0.964 [0.927, 0.991] | 0.775 [0.711, 0.833] | 0.822 [0.769, 0.871] | 6.7 | 12.9 |
| hybrid_dense_bm25 | 0.973 [0.945, 1.000] | 0.832 [0.778, 0.884] | 0.868 [0.824, 0.911] | 5.9 | 7.3 |

## Metrics - lexical queries (n=51); mean [95% bootstrap CI]

| arm | recall@10 | mrr@10 | ndcg@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|
| dense | 0.980 [0.941, 1.000] | 0.752 [0.668, 0.842] | 0.809 [0.741, 0.879] | 3.0 | 3.9 |
| pg_fts | 0.980 [0.941, 1.000] | 0.701 [0.607, 0.795] | 0.770 [0.695, 0.843] | 1.0 | 3.3 |
| bm25 | 1.000 [1.000, 1.000] | 0.774 [0.688, 0.858] | 0.829 [0.762, 0.893] | 0.7 | 2.1 |
| hybrid_dense_fts | 0.980 [0.941, 1.000] | 0.785 [0.698, 0.869] | 0.833 [0.761, 0.902] | 4.2 | 7.0 |
| hybrid_dense_bm25 | 0.980 [0.941, 1.000] | 0.830 [0.752, 0.908] | 0.868 [0.803, 0.930] | 3.8 | 5.1 |

## Metrics - semantic queries (n=59); mean [95% bootstrap CI]

| arm | recall@10 | mrr@10 | ndcg@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|
| dense | 0.983 [0.949, 1.000] | 0.834 [0.756, 0.907] | 0.871 [0.807, 0.928] | 3.0 | 3.5 |
| pg_fts | 0.864 [0.780, 0.949] | 0.591 [0.489, 0.690] | 0.657 [0.565, 0.745] | 5.5 | 11.2 |
| bm25 | 0.949 [0.881, 1.000] | 0.747 [0.650, 0.831] | 0.796 [0.713, 0.866] | 3.5 | 4.7 |
| hybrid_dense_fts | 0.949 [0.881, 1.000] | 0.767 [0.680, 0.849] | 0.812 [0.736, 0.881] | 8.9 | 14.3 |
| hybrid_dense_bm25 | 0.966 [0.915, 1.000] | 0.833 [0.754, 0.903] | 0.867 [0.800, 0.925] | 6.6 | 7.7 |

## Pre-registered decision (ndcg@10, paired vs dense)

- **hybrid_dense_fts**: lexical diff 0.024 [-0.041, 0.088], semantic diff -0.059 [-0.113, -0.007]; min_margin=0.02 -> lexical_gain=False, no_semantic_loss=False, **adopt=False**
- **hybrid_dense_bm25**: lexical diff 0.059 [0.025, 0.101], semantic diff -0.004 [-0.051, 0.041]; min_margin=0.02 -> lexical_gain=True, no_semantic_loss=True, **adopt=True**
