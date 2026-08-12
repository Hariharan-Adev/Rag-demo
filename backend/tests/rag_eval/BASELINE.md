# Known RAG failure-pattern baseline

Date: 2026-08-10 10:54:43 +05:30

This baseline uses isolated temporary storage, synthetic workbooks, and the
existing `tests/fixtures/agriculture_dataset.csv`. It does not call an external
evaluation service or an answer-generation API.

## Result

- Regression command: `venv\Scripts\python.exe -m unittest tests.rag_eval.test_known_failure_regressions -v`
- Total: 28 checks (14 retrieval, 14 final answer)
- Passed: 27
- Failed: 1
- Retrieval: 14 passed, 0 failed
- Final answer: 13 passed, 1 failed
- Duration: 50.228 seconds

## Current failure

`test_answer__follow_up_show_those` fails after this grounded prior turn:

`How many equipment records are priced between 50000 and 200000?`

The follow-up `show those` remains scoped to the correct CSV source, so its
retrieval expectation passes. The final answer is:

```text
Values from the prior grounded result (3):
- 125000
- 75000
- 200000
```

The expected answer identifies the matching records: `Power Tiller`,
`Irrigation Pump`, and `Harvester`. This task intentionally does not fix the
answer-generation/follow-up formatting behavior.

## Passing behavior groups

- records split across multiple workbook tabs;
- exact person/name lookup;
- counts and percentages;
- numeric ranges and exact numeric values;
- named source scope;
- unavailable answers with no sources;
- `name them` and `what about the other ones?` follow-ups;
- no global search for a follow-up without prior context;
- source selection across multiple workbooks;
- clarification for equally plausible workbooks.

## Compatibility validation

`venv\Scripts\python.exe -m unittest tests.rag_eval.test_eval_framework tests.test_chat tests.test_source_selection_gate tests.test_workbook_domain_neutral -v`
passed all 51 tests in 120.773 seconds.

The final read-only security review passed: fixtures are synthetic, external
generation is blocked for both prior and final turns, temporary storage is
isolated, and no secrets or production data are used.
