# RAG architecture

This document describes the implemented request path as of 2026-08-10. It is
an application architecture reference, not a claim that every query is
answerable or that every retrieval result is factually sufficient.

```text
User Query
  -> Follow-Up Resolution
  -> Source Selection
       -> Structured Workbook Route
       -> Retrieval Route
            -> Retrieval-query normalization
            -> Vector retrieval + keyword retrieval
            -> Reciprocal Rank Fusion
            -> evidence-aware context selection
            -> answerability check
            -> LLM
  -> Citation / provenance validation
  -> Final response
```

Query normalization is intentionally applied only inside retrieval. It does
not rewrite the question used for follow-up detection, structured planning, or
answer generation.

## Request stages

| Stage | Implementation and configuration | Failure behavior and security boundary | Observability and regression coverage |
| --- | --- | --- | --- |
| User query | `backend/app/routes/chat.py` passes the authenticated user, optional document/version/collection scope, and conversation ID to `backend/app/services/rag_service.py:answer_question`. | Authentication and request limits run at the route boundary. Query text is treated as untrusted user input. | Standard audit events contain outcome metadata, not document text. `tests/test_chat.py`, `tests/test_security_limits.py`. |
| Query normalization | `backend/app/utils/query_normalization.py`, called by `backend/app/services/vector_search.py:search_chunks`. It normalizes Unicode presentation variants, whitespace, selected punctuation, numeric grouping, and numeric percentage forms. | Empty normalized input returns no retrieval results. It preserves names, IDs, filenames, comparison meaning, and case; it does not perform LLM or semantic rewriting. | No query text is exposed by diagnostics. `tests/test_query_normalization.py`, `tests/rag_eval/test_manual_query_phrasing_assessment.py`. |
| Follow-up resolution | `backend/app/services/chat_context.py:resolve_follow_up` runs before source selection. It uses newest persisted grounded context, a configurable freshness window, and deterministic reference detection. `CHAT_FOLLOW_UP_CONTEXT_MINUTES` defaults to 30. | Reference requests without valid, current, provenance-valid context return the standard unavailable response and must not fall through to global retrieval. Context rows are re-authorized before reuse. | Diagnostic routing records follow-up status but public diagnostic serialization omits query text. `tests/test_chat_context.py`, `tests/test_chat_history.py`, `tests/rag_eval/test_known_failure_regressions.py`. |
| Source selection | `backend/app/services/source_selection.py:select_sources` compares filename/document scope, structured schema/value evidence, and retrieval evidence. Config: `RAG_RETRIEVAL_LIMIT` (15), `RAG_MIN_SCORE` (0.30), `RAG_STRUCTURED_RESULT_LIMIT` (100). | Explicit scopes are checked for readable/current access first. Weak evidence becomes unavailable; close candidates from different documents return clarification. Current-version, organization, ownership, ACL, and soft-delete checks are enforced. | Candidate scores/reasons are available only in opt-in safe diagnostics/audit metadata. `tests/test_source_selection_gate.py`, `tests/test_workbook_domain_neutral.py`. |
| Structured workbook route | `backend/app/services/workbook_analysis.py` reads persisted workbook sheets/rows through the selected document. It handles deterministic lookup, filter, count, aggregation, percentage, range, comparison, list, and grouping plans. | Uses complete eligible structured rows, not vector top-K. Unsupported/ambiguous plans return unavailable or clarification instead of arithmetic guesses. Structured results are validated against provenance and ACL/current-version lifecycle. | Diagnostics record the structured path and safe identifiers. `tests/test_workbook_analysis_generic.py`, `tests/test_workbook_domain_neutral.py`, `tests/test_workbook_extraction_audit.py`. |
| Semantic retrieval | `backend/app/services/vector_search.py` creates an all-MiniLM-L6-v2 query embedding through `backend/app/services/embeddings.py`, then searches `backend/app/services/vector_store.py`. Current configuration supports local Qdrant (`QDRANT_MODE=local`, `QDRANT_LOCAL_PATH`) and a SQLite rollback provider. | Model initialization is synchronized and bounded by `EMBEDDING_MODEL_LOAD_TIMEOUT_SECONDS` (60). Search derives authorized current version IDs before querying, then rechecks returned chunk IDs against SQLite. Provider failure propagates to the normal safe API error path; lack of evidence becomes unavailable. | Safe diagnostics include IDs and component scores, never chunk contents. `tests/test_embeddings.py`, `tests/test_qdrant_integration.py`, `tests/test_qdrant_vector_store.py`. |
| Keyword retrieval | `backend/app/services/keyword_search.py:search_keyword_chunks` provides local BM25-style matching over authorized current chunks and filename/source-location tokens. It is enabled when `RAG_RETRIEVAL_MODE=hybrid`. | SQL applies the same readable-document, organization, version, processing-status, and deletion filters as the retrieval path. No keyword result is returned when no authorized evidence matches. | Keyword scores can appear in safe diagnostics. `tests/test_keyword_search.py`, `tests/test_rag_hybrid_security.py`. |
| Rank fusion | `vector_search.search_chunks` combines vector and keyword ranks using deterministic Reciprocal Rank Fusion. Config: `RAG_RETRIEVAL_MODE` (`hybrid` or `vector`), `RAG_VECTOR_CANDIDATE_LIMIT` (30), `RAG_KEYWORD_CANDIDATE_LIMIT` (30), and `RAG_RRF_K` (60). | Chunk IDs are deduplicated and every vector candidate is authoritatively rechecked. `vector` mode is a configuration rollback path that bypasses keyword retrieval without removing Qdrant. | Diagnostics retain vector, keyword, and fusion scores. `tests/rag_eval/test_minilm_vector_baseline.py`, `tests/test_rag_hybrid_security.py`. |
| Reranking | No reranker is installed or invoked. Diagnostic schema has an optional reranking-score field for future compatible tooling. | No external reranking service receives document text. | Current diagnostic reranking scores are null/empty. The ranking evaluation found hybrid quality sufficient to defer a reranker. `tests/rag_eval/test_minilm_vector_baseline.py`. |
| Context selection | `rag_service.select_final_context` removes canonical, identical, near-identical, and overlapping evidence; favors source diversity for complex questions; obeys `RAG_FINAL_CONTEXT_LIMIT` (5) and `RAG_FINAL_CONTEXT_TOKEN_BUDGET` (6000). `expand_final_context_neighbors` may add up to `RAG_NEIGHBOR_EXPANSION_MAX_NEIGHBORS` (2) only above `RAG_NEIGHBOR_EXPANSION_MIN_SCORE` (0.50), in the same current document/version/section. | Context cannot exceed the hard token budget. Neighbors are reloaded via ACL/current-version SQL and duplicate workbook rows are not collapsed merely for similar prose. | Diagnostics report only final chunk IDs. `tests/test_final_context_selection.py`. |
| Answerability check | `rag_service.has_sufficient_retrieval_evidence` requires selected-document, citable, non-empty, relevant/confident evidence before an LLM call. Structured answers bypass the LLM when deterministic analysis succeeds. | Insufficient evidence returns the project standard unavailable answer with `grounded=false` and `sources=[]`; unrelated or uncitable chunks do not reach generation. | Audit outcome distinguishes insufficient evidence. `tests/test_chat.py`, `tests/rag_eval/test_known_failure_regressions.py`. |
| LLM | Retrieval-route prompts are built in `rag_service.py` and sent by `backend/app/services/groq_client.py` to the configured Groq or Azure OpenAI provider. The system prompt places supplied source text inside an explicit untrusted-data boundary. | Provider errors are not converted into fabricated answers. Embedded document instructions are data, not commands. Structured routes avoid LLM arithmetic. | Token usage is recorded; diagnostic output omits prompts, answers, credentials, and document text. `tests/test_rag_prompt_injection.py`, `tests/test_chat.py`. |
| Citation validation | `source_selection.validate_grounded_result` validates selected document, readable/current version, final-context membership for retrieval citations, result-plan/provenance alignment, and unavailable-answer source removal. | Citation mismatch, stale/unreadable source, discarded-context citation, or unavailable answer with citations is converted to the standard unavailable result. | Safe diagnostics retain authorized IDs only. `tests/test_source_selection_gate.py`, `tests/test_rag_diagnostic_endpoint.py`. |
| Final response | `rag_service.answer_question` returns answer, question type, grounded state, and validated sources. `strip_internal_context` removes server-only follow-up plans before returning. | Clarification has no grounded sources. Unavailable has no sources. Standard chat behavior is unchanged by diagnostics. | `POST /chat/diagnostics` is separate, authenticated, disabled by default via `RAG_DIAGNOSTICS_ENABLED`, ACL-filtered, and never returns text previews. `tests/test_rag_diagnostic_endpoint.py`, `tests/test_rag_diagnostics.py`. |

## Security and observability summary

- `READABLE_DOCUMENT_SQL` is applied at document access boundaries for owner,
  organization, explicit permission, current-version, and soft-delete rules.
- Hybrid retrieval does not trust provider payloads: vector candidates are
  rechecked against authoritative SQLite lifecycle and ACL state before fusion.
- Diagnostics are development-only. Their public representation includes safe
  routing metadata, identifiers, and scores, but omits query text, answer text,
  full private content, previews, credentials, headers, and configuration
  secrets.
- Indexed content is explicitly untrusted prompt context. It cannot alter
  system constraints, bypass ACLs, or remove application-managed citations.

## Remaining limitations

- The known regression `follow_up_show_those` has correct retrieval/source
  scope but formats the previous range result as prices instead of equipment
  names. It is a deterministic result-plan issue, not an LLM or vector-search
  failure.
- Structured analysis is limited to persisted workbook/CSV-style schemas and
  the supported deterministic plan families. Ambiguous columns, documents, or
  numeric targets deliberately return clarification or unavailable.
- Workbook formulas are not evaluated by the application. Extraction uses
  formula/displayed-value provenance where available.
- Deterministic follow-ups require recent, reusable structured rows or an
  employee-profile context. Vague references to prior unstructured answers are
  intentionally unavailable rather than global searches.
- Evaluation and latency results are synthetic/local regression signals; they
  do not establish production-traffic accuracy, latency, or hallucination
  rates.

## Deliberately deferred improvements

- LLM query rewriting: manual phrasing evaluation did not show a meaningful,
  safe retrieval benefit. Only conservative normalization is implemented.
- Reranking: hybrid fusion improved the measured exact filename/number cases;
  no reranker is currently justified.
- Parent-child retrieval: analyzed but not implemented pending evidence from
  real evaluation failures and a migration/reindex plan.
- External evaluation, external reranking, and Docker-only vector deployment:
  not required for the local supported architecture.
- Automatic vector repair at startup: repair remains an explicit,
  confirmation-gated operational command.
