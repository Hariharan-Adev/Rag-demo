# Backend changes

## 2026-07-22 12:11:14 +05:30 — ZIP Archive Upload Support

### Files modified

- `backend/app/config.py`
- `backend/app/routes/upload.py`
- `backend/app/services/zip_archives.py` (new)
- `backend/tests/test_zip_upload.py` (new)
- `frontend/src/services/api.ts`
- `frontend/src/components/UploadDocumentsModal.tsx`

### New services and classes

- Added the `zip_archives` service for full-archive inspection and bounded, one-at-a-time extraction.
- Runs archive inspection and extraction in a worker thread so bounded filesystem/decompression work does not occupy the async request loop.
- Added `ArchiveValidationError`, `ArchiveMember`, and `ArchivePlan` to separate validation outcomes from document processing.
- Reused `_process_document_upload` for every approved member; no alternate extraction, duplicate, embedding, indexing, or persistence pipeline was introduced.

### Security controls

- Streams uploads into a server-generated temporary workspace and always removes the ZIP and extracted files when processing finishes or fails.
- Validates the complete central directory before extraction.
- Rejects traversal paths, absolute paths, Windows drive paths, backslash paths, control characters, symlinks, special files, and reparse points.
- Discards archive directory structure and uses sanitized basenames plus server-generated extraction filenames.
- Rejects nested archives and encrypted entries.
- Enforces configurable limits: 50 MB compressed upload, 250 MB total expanded data, 100 files, and a 100:1 per-entry and aggregate compression ratio.
- Rejects executable/dangerous and unsupported extensions without preventing other valid documents from being processed.
- Enforces the existing per-document size and file-signature validation in the normal upload pipeline.
- Records archive hash, counts, rejection reasons, duration, expanded size, and security-validation failures without logging document contents.

### API changes

- Added authenticated `POST /documents/upload-zip` using multipart field `archive` and optional `collection_id`.
- Returns one consolidated response containing the overall archive status, aggregate counts, and a result for every member.
- Added `archive_extensions` and `max_zip_upload_mb` to `GET /documents/upload-config`.
- The existing `POST /documents/upload` behavior and response contract are unchanged.
- The upload modal accepts `.zip` in file mode and displays member-level results. ZIP files remain unsupported inside folder-upload mode to prevent nested archive ingestion.

### Database changes

- None. Existing document, content, chunk, collection, ownership, and audit storage are reused.

### Testing performed

- Added coverage for successful/partial archive processing, duplicate-content reuse, traversal, nested archives, encrypted entries, symlinks, signature mismatch, compression-ratio limits, file-count limits, compressed-size limits, expanded-size limits, and temporary-workspace cleanup.
- `python -m unittest tests.test_zip_upload -v`: 11 tests passed.
- `python -m unittest discover -s tests -v`: 45 tests passed.
- `npm run build`: TypeScript and Vite production build passed.

### Validation results

- Existing duplicate-detection, folder-upload, parser, retrieval, and chat tests remain green.
- Invalid individual members are reported while valid members continue processing.
- Archive-level security failures abort before extraction and return safe user-facing errors.
- Temporary archive workspaces are empty after success and all tested failure modes.

### Known limitations

- Only standard ZIP containers are accepted; multi-volume, nested, and password-protected archives are intentionally unsupported.
- Archive members are processed sequentially to bound disk, memory, and embedding load.
- Archive directory structure is intentionally not retained.

### Future improvements

- Move long-running archive jobs to a durable background queue if deployment workloads require asynchronous progress reporting.
- Add operational metrics and alerts for archive rejection categories and processing latency.
- Add configurable tenant-specific archive limits if per-organization policies are introduced.

## 2026-07-23 — Domain-neutral Excel ingestion and analysis

### Files changed

- `backend/app/config.py`
- `backend/app/database.py`
- `backend/app/routes/chat.py`
- `backend/app/routes/search.py`
- `backend/app/routes/upload.py`
- `backend/app/services/rag_service.py`
- `backend/app/services/vector_search.py`
- `backend/app/services/workbooks.py` (new)
- `backend/app/services/workbook_analysis.py` (new)
- `backend/tests/test_workbook_domain_neutral.py` (new)

### Change summary

- Added safe extraction of every configured `.xlsx`/`.xls` worksheet, including hidden sheets by default, with dynamic header detection and typed normalization for strings, dates, numbers, booleans, cached formula results, formatted leading-zero numbers, and empty/failed sheet metadata.
- Added row-oriented semantic chunks containing workbook, sheet, and row provenance.
- Added deterministic owner-scoped workbook analysis for counts, totals, averages, minima/maxima, distinct values, grouped summaries, and filtered lists.
- Added dynamic column matching, ambiguity clarification, sheet restrictions, workbook selection checks, and concise calculation/source metadata.
- Added document-level chat scope and sheet/row citations to retrieval results.
- Preserved file-size, signature, extraction, chunk, ownership, collection, and document security boundaries; spreadsheet-controlled output is rendered as inert text.

### Reason

- Spreadsheet questions must work across arbitrary business domains and all workbook tabs without deriving calculations from top-K semantic chunks or relying on employee-specific schemas.

### Test result

- `python -m unittest discover -s tests -v`: 51 tests passed.
- New coverage verifies 13 employee tabs, finance totals/averages, cross-tab inventory distinct counts, final-sheet retrieval, empty-sheet handling, non-distinct duplicate counting, one-sheet scope, and cross-owner denial.
- `npm run build`: TypeScript and Vite production build passed; Vite reported its existing non-fatal large-chunk warning.
