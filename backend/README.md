# Backend

The SQLite schema separates physical uploads (`documents`) from reusable processed
content (`document_contents` and `chunks`). Duplicate hashes are always scoped by
`owner_id`.

On startup, a legacy `documents`/`chunks.document_id` database is migrated in place.
A one-time `rag_new.db.pre_content_refactor.bak` backup is created first. Legacy text
is reconstructed from ordered chunks because the old schema did not retain full
extracted text. Legacy rows with the same owner and reconstructed normalized text
share one content record; colliding display names receive numeric suffixes.

Physical files are retained independently for different uploads, even when processed
content is reused. Deleting a document removes only that file and reference. Shared
content and chunks are deleted only after the final reference is removed.

Supported parsers are registered in `app.services.document_loader` for TXT, PDF,
DOCX, XLSX, XLS, CSV, PPTX, legacy PPT, and common image formats. Image OCR uses
`pytesseract`; the host must also install the Tesseract executable and make it
available on `PATH`. If it is missing, image uploads return a clear HTTP 400 error.
