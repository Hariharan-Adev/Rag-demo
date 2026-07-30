# RAG backend

The FastAPI backend implements tenant-scoped document storage, asynchronous
ingestion, immutable upload versions, structured source citations, and
Qdrant-backed retrieval.

## Tenant and document authorization

Every authenticated user has a server-resolved `organization_id` and one of the
`member` or `organization_admin` roles. Request payloads cannot select a tenant.
Tenant-owned SQL queries and Qdrant payload filters include the organization.

New documents are `private` and owned by the uploader. A readable document must
belong to the user's organization and be organization-visible, owned by the user,
or explicitly shared with that user. Only the owner, a user with a `manage`
grant, or an organization administrator can mutate it. Central predicates live
in `app.services.document_access`.

## Versions and deletion

`documents` is the logical record. Every upload is retained in
`document_versions`; a successful worker run atomically changes
`documents.current_version_id`. Version records expose separate ingestion,
extraction, and indexing states plus storage key, MIME type, byte size, hashes,
and safe failure details.

Document, version, content, and chunk deletes are soft deletes with
`deleted_at` and `deleted_by`. A document delete immediately marks its Qdrant
payload inactive. Restore only revives successfully indexed rows deleted by the
document cascade, so a separately deleted version stays deleted. Hard delete is
disabled by default, restricted to organization administrators, and audited.

Lifecycle endpoints:

- `POST /documents/upload` is the canonical queued upload and returns `202`;
  `/api/documents/upload` remains a compatibility alias.
- `POST /documents/{id}/versions` queues an explicit immutable version;
  `/api/documents/{id}/versions` remains a compatibility alias.
- `GET /documents/{id}/versions` and `GET /documents/{id}/versions/{version_id}`
  expose version status.
- `POST /documents/{id}/versions/{version_id}/make-current` restores a completed
  version by pointer change.
- `DELETE /documents/{id}/versions/{version_id}` soft-deletes a non-current
  version.
- `DELETE /documents/{id}` and `POST /documents/{id}/restore` manage the trash.
- `PATCH /documents/{id}/visibility` publishes privately or to the organization.

Search and chat use the current version by default. Authorized callers may pass
`version_id` (optionally with `document_id`) to retrieve one successfully indexed
older version; the same tenant, ACL, and soft-delete predicates are applied.

## Asynchronous ingestion

The API performs bounded preflight validation, stores the upload, creates the
document/version/job records in a transaction, and returns:

```json
{
  "document_id": 1,
  "version_id": 1,
  "job_id": "uuid",
  "status": "queued"
}
```

Run the durable worker separately:

```powershell
.\.venv312\Scripts\python.exe -m app.worker
```

The worker claims jobs with compare-and-set updates, recovers stale locks, uses
bounded exponential retry with jitter for transient dependency failures only,
and performs extraction, normalization, duplicate detection, chunking, embedding,
Qdrant indexing, and final activation. Validation, corrupt-file, and unsupported
content failures are terminal on the first attempt. API responses contain stable
error codes and safe messages; full exception details remain in worker logs.

Each version job has a deterministic key built from organization, version, job
type, and pipeline version. Request idempotency is tracked separately. A unique
database constraint and compare-and-set claim prevent concurrent processing of
the same version. If a worker stops after committing chunks, the next attempt
reuses their persisted embeddings, verifies deterministic Qdrant point IDs,
upserts missing points, and finalizes without extracting again.
Use `GET /ingestion-jobs/{job_id}`, `POST /ingestion-jobs/{job_id}/retry`, and
`POST /ingestion-jobs/{job_id}/cancel` for canonical job control. The `/api/jobs`
forms remain compatibility aliases. Every ownership or tenant denial returns the
same safe not-found response.

## Duplicate and storage policy

A same-name upload by the same owner targets the existing logical document.
Identical content is rejected with `409` unless it is submitted through the
explicit version endpoint; changed content becomes the next version. A different
filename creates a distinct logical document even when its normalized content is
identical. In that case extracted content may be reused only when an active
same-organization document is readable by the uploader. Each logical
document/version still receives its own SQL chunks and deterministic Qdrant
points. Reuse never crosses organizations.

New file keys are stored beneath an opaque organization partition and are
resolved beneath the configured upload root with traversal checks. Legacy flat
keys remain readable for migration compatibility. Office Open XML files are
inspected as archives before parsing; traversal entries, encryption, excessive
entry counts, expansion sizes, and compression ratios are rejected.
In production, parsing runs in an isolated subprocess that is terminated when
`PARSER_TIMEOUT_SECONDS` expires.

PDF chunks retain page ranges and block IDs. PowerPoint chunks retain slide
ranges, shape IDs/types, table rows, content origin, and speaker-note flags.
Excel chunks retain exact sheet names, visibility, row/column and cell ranges,
table names, header context, merged ranges, formulas, and cached values.
Workbook-level metadata includes sheet counts/names, visible and processed
sheets, detected tables, and non-empty-row totals. Source-specific location
schemas are validated before chunks can be indexed.

Qdrant payloads use `organization_id`, `document_id`,
`document_version_id`, `owner_id`, `visibility`, `is_deleted`, `chunk_id`,
`chunk_index`, `source_type`, `source_location`, `embedding_model`, and
`embedding_dimension`. Tenant, deletion, current/explicit version, and
private/organization ACL filters are part of the vector query itself.

## Qdrant and configuration

Start Qdrant from the repository root:

```powershell
docker compose up -d qdrant
```

Copy `.env.example` to `.env`. In production, set `APP_ENVIRONMENT=production`,
configure an HTTPS `QDRANT_URL` and `QDRANT_API_KEY`, use a strong
`JWT_SECRET_KEY`, and run API and worker as separate supervised processes.
In-process/local Qdrant is a development and test fallback only.
OpenSearch connection settings are reserved in configuration for a future
provider implementation; Qdrant is the only enabled provider in this release.

The Compose development ports bind only to `127.0.0.1`; production Qdrant must
not expose unauthenticated public ports.

`QDRANT_MODE` accepts `auto`, `local`, `remote`, or `memory`. `auto` preserves
legacy behavior by selecting a configured URL first, then `QDRANT_PATH` (or the
legacy `QDRANT_LOCAL_PATH`), then in-memory storage. Use `local` plus
`QDRANT_PATH=./data/qdrant` for persistent development without Docker. Production
requires `remote`, HTTPS, and an API key. The application uses one collection and
creates remote payload indexes for organization, owner, document, version,
visibility, and deletion filters.

`EMBEDDING_DIMENSION` must match `EMBEDDING_MODEL_VERSION`; startup rejects a
Qdrant collection with a different vector size. Chat retrieves
`RAG_RETRIEVAL_LIMIT` candidates, removes results below `RAG_MIN_SCORE`, and sends
at most `RAG_FINAL_CONTEXT_LIMIT` chunks to the LLM. Defaults are 15, 0.35, and 5.
No reranker is enabled; add one only with an evaluated model and corpus-specific
quality tests.

`GET /health/ready` checks SQLite and Qdrant. `GET /metrics` exposes per-tenant
document/job gauges plus retries, terminal failures, chunks created, vector
upsert failures, lifecycle counts, and average extraction/embedding/indexing
durations. Structured logs correlate request, organization, user, job, document,
and version identifiers without document content or storage paths.
`python -m app.reindex` backfills vector payloads after migrating existing data.

## Migrations and verification

SQLite migrations are idempotent and recorded in `schema_migrations`. Migration
`006_ingestion_metrics` adds nullable timing fields and zero-default counters,
preserving every existing job. Startup validates foreign keys and user,
document, version, and current-version tenant ownership before committing.
Before the
multi-tenant migration, startup creates
`rag_new.db.pre_multitenant_rag.bak`. Stop the API and worker before restoring
that backup; records written after migration are not present in it.

Run tests with:

```powershell
.\.venv312\Scripts\python.exe -m unittest discover -s tests -v
```

Image OCR requires the Tesseract executable on `PATH`, in a standard Windows
install location, or configured through `TESSERACT_CMD`. Optional vision
analysis falls back to OCR when the external provider is unavailable.
