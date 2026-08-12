# all-MiniLM-L6-v2 category benchmark

Date: 2026-08-10

This is a local, vector-only benchmark of the configured
`sentence-transformers/all-MiniLM-L6-v2` model. It uses isolated synthetic
documents, the existing SQLite rollback vector provider, and no external
evaluation or answer-generation service.

## Clean category corpus

Two probes per category (14 total) were retrieved from a 14-document corpus.
Each query's intended chunk ranked first.

| Category | Recall@5 | Recall@10 | MRR | Ranks |
| --- | ---: | ---: | ---: | --- |
| Semantic paraphrase | 1.00 | 1.00 | 1.0000 | 1, 1 |
| Names | 1.00 | 1.00 | 1.0000 | 1, 1 |
| Numbers | 1.00 | 1.00 | 1.0000 | 1, 1 |
| Workbook labels | 1.00 | 1.00 | 1.0000 | 1, 1 |
| Document headings | 1.00 | 1.00 | 1.0000 | 1, 1 |
| Abbreviations | 1.00 | 1.00 | 1.0000 | 1, 1 |
| Cross-domain terminology | 1.00 | 1.00 | 1.0000 | 1, 1 |

## Identifier-heavy near-duplicate stress control

The existing 72-chunk local corpus is deliberately harder: every target has
eight near-duplicate records. Vector-only results show that dense retrieval is
strong for exact names, IDs, labels, column headings, and abbreviations, but
is weaker for some non-semantic identifiers.

| Query form | Recall@5 | Recall@10 | MRR | Correct rank |
| --- | ---: | ---: | ---: | ---: |
| Employee name | 1.00 | 1.00 | 1.0000 | 1 |
| Employee ID | 1.00 | 1.00 | 1.0000 | 1 |
| Code | 1.00 | 1.00 | 1.0000 | 1 |
| Exact label | 1.00 | 1.00 | 1.0000 | 1 |
| Number | 1.00 | 1.00 | 0.3333 | 3 |
| Column heading | 1.00 | 1.00 | 1.0000 | 1 |
| Unusual abbreviation | 1.00 | 1.00 | 1.0000 | 1 |
| Filename (auxiliary control) | 0.00 | 1.00 | 0.1111 | 9 |

## Interpretation

The clean corpus demonstrates that the configured model can rank the tested
semantic paraphrases, headings, workbook labels, abbreviations, and
cross-domain terms correctly. It does not establish universal performance.
The near-duplicate stress control shows an observable dense-retrieval ranking
weakness for exact numeric references and a Recall@5 failure for filenames.
These are evidence for retaining the existing hybrid keyword signal, not for
changing the production embedding model in this task.

Commands:

```powershell
.\venv\Scripts\python.exe -m unittest tests.rag_eval.test_minilm_vector_baseline.MiniLMVectorRetrievalBaselineTests.test_all_minilm_category_benchmark -v
.\venv\Scripts\python.exe -m unittest tests.rag_eval.test_minilm_vector_baseline.MiniLMVectorRetrievalBaselineTests.test_all_minilm_identifier_retrieval_baseline -v
```
