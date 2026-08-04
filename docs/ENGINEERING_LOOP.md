# RAG Project Engineering Loop Workflow

This workflow is used to implement, test, secure, and document changes in the RAG project.

Project context:
- Backend: FastAPI
- Frontend: React/Vite
- Worker: background ingestion worker
- Database: SQLite, resolved through `app.database.DATABASE_PATH`
- Vector store: Qdrant, often local Qdrant in development
- Main risk areas: document upload, extraction, chunking, embeddings, retrieval, citations, SQLite persistence, Qdrant sync, and frontend chat behavior

## Core Rules

- Write the smallest amount of code required.
- Preserve existing correct behavior.
- Inspect current status and diffs before editing.
- Do not reset, delete, rebuild, or reindex data unless explicitly approved or clearly required by evidence.
- Do not interrupt live Qdrant or existing database data.
- Only Agent 1 and Agent 3 may edit code.
- All other agents validate, test, review, or document.
- Every function must have a useful comment.
- Important validations, security checks, edge cases, and non-obvious logic must be commented.
- Do not add obvious comments that simply repeat the code.
- Any failed test or security issue must return to Agent 3 for a permanent root-cause fix.

## Agent 1: Implementation Agent

Purpose:
Analyze the requirement and implement the smallest clean solution.

Tasks:
- Read the requirement carefully.
- Inspect relevant backend, frontend, worker, database, and retrieval files.
- Check current project status before editing.
- Use existing project patterns.
- Avoid unnecessary abstractions, duplicate logic, and unused dependencies.
- Preserve existing API, retrieval, upload, and UI behavior unless the requirement asks to change it.
- For RAG changes, be careful with chunking, embeddings, citations, Qdrant sync, and SQLite records.
- Remove dead code before handoff.

Output:
- Files changed
- What was implemented
- Why this approach was chosen
- Assumptions made
- Areas needing test coverage

## Agent 2: Test Validation Agent

Purpose:
Verify that Agent 1 implemented the requirement correctly.

Tasks:
- Run relevant backend tests.
- Run relevant frontend checks if frontend changed.
- Run compile or build checks where needed.
- Validate the requirement using real behavior.
- Check old behavior for regressions.
- For RAG changes, verify retrieval quality, citations, database records, and vector-store consistency when relevant.
- Do not edit code.

Output:
```text
Test Summary:
- Passed:
- Failed:
- Not tested:

Requirement Validation:
- Requirement:
- Expected behavior:
- Actual behavior:
- Status:

Issues Found:
- File:
- Problem:
- Expected:
- Actual:
```

Decision:
- If everything passes, move to Agent 5.
- If anything fails, send to Agent 3.

## Agent 3: Root Cause Patch Agent

Purpose:
Fix failures permanently using the smallest safe patch.

Tasks:
- Understand the failed test or broken behavior.
- Find the real root cause, not only the visible symptom.
- Apply a permanent fix that works for future similar cases.
- Do not hardcode for one sample case.
- Do not disable tests.
- Do not hide errors silently.
- Patch only the failing or incomplete part.
- Preserve old correct code.
- Add or update tests when needed to prevent the issue from returning.

RAG-specific checks:
- Check whether the issue is in parsing, chunking, embeddings, Qdrant sync, SQLite persistence, retrieval ranking, prompt context, or citation formatting.
- Resolve the active database through `app.database.DATABASE_PATH`.
- Do not assume `rag.db` is the active database.
- Do not rebuild or reindex unless evidence shows it is needed.

Output:
```text
Correction Plan:
- Failure:
- Root cause:
- Permanent fix:
- Why this is not temporary:
- Files to patch:

Patch Summary:
- Changed:
- Preserved:
- Tests added/updated:
- Risk:
```

Decision:
- Send to Agent 4 after patching.

## Agent 4: Retest Verification Agent

Purpose:
Confirm Agent 3's correction works.

Tasks:
- Re-run the failed tests.
- Re-run related regression tests.
- Check that old correct behavior still works.
- For RAG fixes, verify representative retrieval answers and citations.
- Confirm the fix handles similar future cases, not only one sample input.
- Do not edit code.

Output:
```text
Correction Verification:
- Previously failing tests:
- Current result:
- Regression result:
- RAG behavior checked:
- Status: Pass / Send back to Agent 3
```

## Agent 5: Security Review Agent

Purpose:
Review the final working code for security problems.

Tasks:
- Check input validation.
- Check authentication and authorization.
- Check file upload safety.
- Check filename and path handling.
- Check SQL/database query safety.
- Check secret handling.
- Check unsafe logs and error leakage.
- Check CORS and rate-limit behavior when relevant.
- Check new dependencies if any were added.

RAG-specific security checks:
- Uploaded files must be validated.
- User data must not leak across users.
- Retrieval must not expose unauthorized documents.
- Errors must not reveal secrets, file paths, or internal configuration.
- Database queries must be parameterized.

Output:
```text
Security Review:
- Passed checks:
- Issues found:
- Severity:
- Recommended fix:
- Files affected:
```

Decision:
- If security passes, move to Agent 6.
- If security fails, send to Agent 3 for a permanent minimal patch, then Agent 4 retests.

## Agent 6: Final Documentation Agent

Purpose:
Document the implementation clearly with date and time.

Tasks:
- Add current date and time.
- Summarize the original requirement.
- Document changed files.
- Document mistakes or failed tests found during the loop.
- Document how each issue was fixed.
- Document final tests run.
- Document final security checks.
- Document known limitations.
- Add reprocessing or reindexing guidance if RAG data behavior changed.

Output:
```text
Implementation Report:
- Date:
- Time:
- Requirement:
- Files modified:
- What changed:
- Mistakes found:
- How they were fixed:
- Tests run:
- Security checks:
- Reprocessing/reindexing needed:
- Known limitations:
```

## Agent 7: Commit Message Agent

Purpose:
Create a clear commit message from the final changes.

Tasks:
- Review final changed files.
- Summarize only the real changes.
- Write a concise commit title.
- Add a commit body for multi-part changes.

Output:
```text
Commit message:

type: short summary

- change 1
- change 2
- change 3
```

Example:
```text
fix: improve structured RAG retrieval citations

- preserve row ranges for aggregate workbook citations
- route PDF table rows through structured retrieval
- add regression coverage for source selection
```

## Agent 8: API Documentation Agent

Purpose:
Document all API endpoints affected by the change.

For each endpoint, document:
- Method
- URL
- Purpose
- Authentication requirement
- Headers
- Request body
- Query parameters
- Path parameters
- Success response
- Error responses
- Example request
- Example response

## Agent 9: Database Documentation Agent

Purpose:
Document database tables, columns, meanings, and relationships.

Tasks:
- Identify tables affected by the change.
- Document schema/table purpose.
- Explain each column in simple language.
- Include data type, nullable status, keys, relationships, and example values.
- For RAG tables, explain documents, chunks, workbook sheets, workbook rows, users, jobs, and vector-store relationship where relevant.

## Agent 10: Frontend Design Documentation Agent

Purpose:
Document frontend design decisions when UI changes are made.

Tasks:
- Document typography.
- Document font family and font sizes.
- Document color palette.
- Document theme.
- Document spacing.
- Document layout rules.
- Document buttons, forms, tables, cards, and navigation.
- Document responsive behavior.
- Document accessibility notes.

## Activation

Use this prompt when starting work in Codex:

```text
Use the RAG Project Agent Workflow from AGENTS.md and docs/ENGINEERING_LOOP.md.
Implement this requirement through the full engineering loop.
Only Agent 1 and Agent 3 may edit code.
Agent 3 must apply permanent root-cause fixes, not temporary workarounds.
Preserve old correct behavior.
Run relevant tests, retests, and security review.
At the end, provide implementation documentation, commit message, API documentation, database documentation, and frontend design documentation when relevant.

Requirement:
[paste the requirement here]
```

## Final Loop Order

1. Agent 1 implements.
2. Agent 2 tests and validates.
3. If tests fail, Agent 3 creates a permanent root-cause patch.
4. Agent 4 retests.
5. Repeat Agent 3 and Agent 4 until tests pass.
6. Agent 5 performs security review.
7. If security fails, return to Agent 3, then Agent 4.
8. Agent 6 writes final implementation documentation.
9. Agent 7 writes the commit message.
10. Agent 8 writes API documentation.
11. Agent 9 writes database documentation.
12. Agent 10 writes frontend design documentation.

The work is complete only when implementation, tests, retests, security review, implementation documentation, commit message, API documentation, database documentation, and any relevant API, database, and frontend design documentation are complete.
