# Experiment 001 results

## Run parameters

- database: aibrain_exp001
- documents: 154
- chunks: 1002
- queries: {'lexical': 135, 'semantic': 144}
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

## Metrics - all queries (n=279); mean [95% bootstrap CI]

| arm | recall@10 | mrr@10 | ndcg@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|
| dense | 0.932 [0.900, 0.961] | 0.749 [0.706, 0.789] | 0.794 [0.755, 0.829] | 3.7 | 5.2 |
| pg_fts | 0.889 [0.849, 0.925] | 0.644 [0.598, 0.689] | 0.704 [0.663, 0.743] | 3.4 | 12.1 |
| bm25 | 0.964 [0.939, 0.982] | 0.810 [0.771, 0.846] | 0.848 [0.815, 0.878] | 2.6 | 5.2 |
| hybrid_dense_fts | 0.950 [0.925, 0.975] | 0.736 [0.694, 0.777] | 0.789 [0.753, 0.824] | 7.6 | 16.0 |
| hybrid_dense_bm25 | 0.961 [0.935, 0.982] | 0.805 [0.767, 0.841] | 0.844 [0.811, 0.874] | 6.5 | 10.1 |

## Metrics - lexical queries (n=135); mean [95% bootstrap CI]

| arm | recall@10 | mrr@10 | ndcg@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|
| dense | 0.933 [0.889, 0.970] | 0.743 [0.679, 0.805] | 0.789 [0.732, 0.842] | 3.5 | 5.2 |
| pg_fts | 0.933 [0.881, 0.970] | 0.736 [0.673, 0.796] | 0.784 [0.729, 0.836] | 1.2 | 3.6 |
| bm25 | 1.000 [1.000, 1.000] | 0.862 [0.817, 0.905] | 0.896 [0.861, 0.929] | 0.7 | 2.2 |
| hybrid_dense_fts | 0.970 [0.941, 0.993] | 0.752 [0.697, 0.811] | 0.806 [0.760, 0.854] | 5.0 | 8.4 |
| hybrid_dense_bm25 | 0.993 [0.978, 1.000] | 0.819 [0.768, 0.869] | 0.862 [0.822, 0.901] | 4.6 | 7.0 |

## Metrics - semantic queries (n=144); mean [95% bootstrap CI]

| arm | recall@10 | mrr@10 | ndcg@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|
| dense | 0.931 [0.889, 0.972] | 0.755 [0.697, 0.814] | 0.798 [0.746, 0.849] | 3.8 | 5.1 |
| pg_fts | 0.847 [0.785, 0.903] | 0.557 [0.495, 0.622] | 0.628 [0.569, 0.685] | 7.2 | 13.2 |
| bm25 | 0.931 [0.889, 0.972] | 0.761 [0.703, 0.822] | 0.802 [0.751, 0.855] | 3.9 | 5.5 |
| hybrid_dense_fts | 0.931 [0.889, 0.965] | 0.721 [0.659, 0.778] | 0.772 [0.719, 0.822] | 11.2 | 17.8 |
| hybrid_dense_bm25 | 0.931 [0.889, 0.972] | 0.793 [0.738, 0.851] | 0.826 [0.780, 0.876] | 7.7 | 10.5 |

## Pre-registered decision (ndcg@10, paired vs dense)

- **hybrid_dense_fts**: lexical diff 0.017 [-0.021, 0.059], semantic diff -0.026 [-0.067, 0.013]; min_margin=0.02 -> lexical_gain=False, no_semantic_loss=True, **adopt=False**
- **hybrid_dense_bm25**: lexical diff 0.073 [0.035, 0.111], semantic diff 0.029 [-0.012, 0.071]; min_margin=0.02 -> lexical_gain=True, no_semantic_loss=True, **adopt=True**
