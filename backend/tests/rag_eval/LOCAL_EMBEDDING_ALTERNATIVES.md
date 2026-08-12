# Local embedding alternatives benchmark

Date: 2026-08-10

This benchmark runs entirely locally after the initial model download. It uses
the existing 72-passage synthetic near-duplicate identifier corpus, eight
queries, cosine similarity, and no application documents or external
inference service. The index-size estimate is `72 * dimensions * 4` for dense
float32 values only; it excludes Qdrant payload/index overhead.

| Model | License | Dim | Recall@5 | Recall@10 | MRR | Median query embedding | Corpus embedding | Est. reindex / 1,000 chunks | Dense index / 1,000 chunks |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| sentence-transformers/all-MiniLM-L6-v2 | Apache-2.0 | 384 | 1.00 | 1.00 | 0.8167 | 9.25 ms | 205.86 ms / 72 | 2.86 s | 1.46 MiB |
| BAAI/bge-small-en-v1.5 | MIT | 384 | 1.00 | 1.00 | 0.9000 | 16.72 ms | 378.29 ms / 72 | 5.25 s | 1.46 MiB |
| intfloat/e5-small-v2 | MIT | 384 | 1.00 | 1.00 | 0.9000 | 18.98 ms | 454.36 ms / 72 | 6.31 s | 1.46 MiB |

## Ranking evidence

all-MiniLM ranked the exact number at 3 and the filename control at 5. Both
alternatives moved the number to rank 1, while the filename control remained
rank 5. All other probes ranked first. Therefore the alternatives improved
ranking quality but did not improve Recall@5 or Recall@10 on this corpus.

## CPU memory and loading

Windows working-set deltas observed while loading each model in the same
benchmark process were +19.15 MiB (MiniLM), +11.04 MiB (BGE-small), and +8.37
MiB (E5-small). These are incremental process measurements, not isolated peak
model RAM figures; framework allocations and prior loads remain in-process.
Warm cache load times were 284.89 ms, 157.79 ms, and 161.83 ms respectively.
The first approved download/load run took 7.87 s for MiniLM, 16.24 s for BGE,
and 61.34 s for E5, so cold setup must be planned separately from warm service
latency.

## Recommendation

Keep the current all-MiniLM production configuration and existing hybrid
keyword retrieval for now. The measured MRR gain alone does not justify a
model migration because Recall@5 did not improve and both alternatives add
query and reindex latency. If a future controlled, authorized A/B evaluation
uses a larger representative fixture set and shows a material Recall@5 gain,
evaluate `BAAI/bge-small-en-v1.5` first: it matches the current 384-dimension
index shape, has an MIT model-card license, achieved the same MRR as E5 here,
and avoids E5's required `query:`/`passage:` asymmetric prefix contract.

Commands:

```powershell
.\venv\Scripts\python.exe -m scripts.benchmark_local_embeddings
.\venv\Scripts\python.exe -m unittest tests.test_embedding_alternative_benchmark -v
```
