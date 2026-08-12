# Manual query-phrasing assessment

This is an evaluation-only assessment. No query rewriting was added, and no
LLM was called. Each comparison uses an isolated local workbook fixture.

## Result

| Query class | Original versus manually clarified query | Retrieval finding | Rewrite decision |
| --- | --- | --- | --- |
| Follow-ups | `show those` versus an explicit range-and-entity request | The original takes the bounded `follow_up` path and retains the same source. The clarification takes structured retrieval only because it injects facts from the prior turn. | Not justified: a generic rewrite would either invent referents or risk forbidden global search without context. |
| Abbreviations | Existing local MiniLM benchmark includes `What does RZT mean?` and `Explain the LQL acronym.` | Both abbreviation probes rank the correct chunk first (Recall@5/10 1.00, MRR 1.00). | Not justified by the measured baseline. |
| Natural-language spreadsheet questions | Multi-tab count, percentage average, and source-schema sum versus concise structured forms | Both forms select the same structured source and worksheet provenance. | Not justified for these patterns. |
| Verbose question | Polite, descriptive active-project count versus `Count active projects.` | Both forms select the same structured source. | Not justified for this pattern. |

The current regression baseline has one final-answer failure: after a valid
`show those` follow-up, the result lists numeric values instead of the expected
equipment names. Its route and source are correct, so it is not evidence of a
retrieval-query phrasing failure.

## Conclusion

The available evaluation failures do not show a retrieval defect caused by
query phrasing. Do not add LLM query rewriting yet. Preserve the existing
deterministic follow-up resolver and conservative retrieval normalization. Add
new rewrite evaluation only after a failure demonstrates a stable, equivalent
manual wording improvement without adding missing facts or weakening the
no-global-search rule for context-dependent queries.

## Reproduce

```powershell
.\venv\Scripts\python.exe -m unittest tests.rag_eval.test_manual_query_phrasing_assessment -v
.\venv\Scripts\python.exe -m unittest tests.rag_eval.test_minilm_vector_baseline.MiniLMVectorRetrievalBaselineTests.test_all_minilm_category_benchmark -v
.\venv\Scripts\python.exe -m unittest tests.rag_eval.test_known_failure_regressions -v
```
