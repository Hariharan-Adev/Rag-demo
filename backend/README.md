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

## Development RAG diagnostics

`POST /chat/diagnostics` runs the normal RAG orchestration with an internal,
content-free trace. It requires the same bearer authentication and hourly chat
rate limit as `POST /chat`. The capability is disabled by default and returns
`404` unless `RAG_DIAGNOSTICS_ENABLED=true` is explicitly configured. Keep the
flag disabled in production except during an approved diagnostic window.

The request body uses the normal chat fields: `question`, optional
`conversation_id`, `collection_id`, `document_id`, and `version_id`. An explicit
document is checked with the central read ACL before RAG execution. All traced
document and chunk IDs are checked again against the authenticated user's
organization, ownership, visibility, sharing permissions, and soft-delete state
before the response is returned. Unauthorized documents receive the same safe
`404` used by other document APIs.

The response is marked with `capability: "rag_diagnostic"` and includes routing
reason codes, authorized source IDs, retrieval limits and score arrays,
structured-analysis path, final context chunk IDs, and grounded/unavailable
status. Query text, answers, document text, filenames, source locations,
authentication data, configuration, and secrets are omitted. The
`retrieved_chunks.text_previews` list is intentionally empty.

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
and performs extraction, normalization, duplicate detection, and chunking. It
commits authoritative chunk text and `pending` vector state to SQLite before
generating embeddings in bounded batches, upserts vectors to Qdrant, and marks
the chunks indexed only after Qdrant confirms the write. Validation, corrupt-file,
and unsupported content failures are terminal on the first attempt. API responses
contain stable error codes and safe messages; full exception details remain in
worker logs.

Each version job has a deterministic key built from organization, version, job
type, and pipeline version. Request idempotency is tracked separately. A unique
database constraint and compare-and-set claim prevent concurrent processing of
the same version. SQLite does not store embedding JSON for newly ingested
canonical chunks. A retry reuses vectors already confirmed in Qdrant; if the
provider never accepted them, it regenerates embeddings from committed,
authoritative SQLite text and upserts the same deterministic point IDs.
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

Qdrant payloads use `organization_id`, `document_id`, `content_id`,
`document_version_id`, `owner_id`, `visibility`, `is_deleted`, `chunk_id`,
`chunk_index`, `source_type`, `source_location`, `embedding_model`, and
`embedding_dimension`. Complete chunk text is intentionally absent: retrieval
uses the returned `chunk_id` to load current authoritative text and source
metadata from SQLite. Tenant, deletion, current/explicit version, and
private/organization ACL filters are part of the vector query itself and are
revalidated during SQLite hydration.

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

Image OCR requires Tesseract 5.x with the `eng` language pack. Local Windows
development can install the official Tesseract package, then either add
`tesseract.exe` to the service `PATH` or set `TESSERACT_CMD`, for example
`TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe`. Production images
or servers must install Tesseract and required language data during deployment.
The application fails fast in production when OCR is unavailable, and health
responses expose only `ocr.status` as `ready` or `unavailable`, never executable
paths, stack traces, document text, or secrets.

The Compose development ports bind only to `127.0.0.1`; production Qdrant must
not expose unauthenticated public ports.

`QDRANT_MODE` accepts `auto`, `local`, `remote`, or `memory`. `auto` preserves
legacy behavior by selecting a configured URL first, then `QDRANT_PATH` (or the
legacy `QDRANT_LOCAL_PATH`), then in-memory storage. Use `local` plus
`QDRANT_PATH=./data/qdrant` for persistent development without Docker. Production
requires `remote`, HTTPS, and an API key. The application uses one collection and
creates remote payload indexes for organization, owner, document, version,
visibility, and deletion filters.

`QDRANT_PREFER_GRPC=true` enables gRPC for a remote deployment that exposes a
protected gRPC endpoint. Keep it false when only the HTTPS REST endpoint is
available. Never expose Qdrant's HTTP or gRPC ports publicly without
authentication, firewall/private-network restrictions, and transport encryption.
Store the API key in environment configuration supplied by a secrets manager;
never commit it.

During the validation release, `VECTOR_STORE=qdrant` is the primary provider and
`VECTOR_STORE=sqlite` is the explicit rollback provider. The older
`VECTOR_STORE_PROVIDER` name remains a compatibility alias.
`VECTOR_STORE_ROLLBACK_DUAL_WRITE=true` retains newly generated vectors in
`chunks.embedding` only after Qdrant confirms its upsert, allowing documents
created during the rollout to participate in a tested SQLite rollback. Disable
dual-write after cutover approval and remove the column only in the later,
separately tested schema release.

`EMBEDDING_DIMENSION` must match `EMBEDDING_MODEL_VERSION`; startup rejects a
Qdrant collection with a different vector size. Chat retrieves
`RAG_RETRIEVAL_LIMIT` candidates, removes results below `RAG_MIN_SCORE`, and sends
at most `RAG_FINAL_CONTEXT_LIMIT` chunks to the LLM. Defaults are 15, 0.35, and 5.
The threshold is applied by Qdrant and again after retrieval. When no authorized
chunk survives, chat returns a deterministic ungrounded response without calling
the LLM. The RAG prompt also forbids general knowledge and unsupported inference.
No reranker is enabled; add one only with an evaluated model and corpus-specific
quality tests, including a separate post-rerank relevance threshold.

The local embedding model is loaded lazily. Concurrent retrieval requests share
one loader and wait at most `EMBEDDING_MODEL_LOAD_TIMEOUT_SECONDS` (default 60)
before receiving a clear initialization error. `/health` and `/health/ready`
report only the safe embedding state and never trigger model loading.

Each chunk has a globally unique `vector_point_id`, per-chunk
`indexing_status`, and `qdrant_indexed_at` confirmation timestamp in SQLite.
Point IDs are deterministic UUIDv5 values derived from organization, version,
chunk index, and embedding model. This is intentionally stronger for retries than
generating a new UUIDv4 on each attempt: repeating a job updates the same Qdrant
point. Migration `009_chunk_vector_sync` adds the synchronization columns and
partial unique index without replacing historical IDs.

`GET /health/ready` checks SQLite, Qdrant, embeddings, and safe OCR readiness.
`GET /metrics` exposes per-tenant document/job gauges plus retries, terminal
failures, chunks created, vector upsert failures, lifecycle counts, and average
extraction/embedding/indexing durations. Structured logs correlate request,
organization, user, job, document, and version identifiers without document
content or storage paths.
`python -m app.reindex` backfills vector payloads after migrating existing data.
It can read historical SQLite embedding JSON for backward compatibility and
regenerates vectors from chunk text when that legacy value is absent.

Spreadsheet row labels and vectors can be upgraded independently with the
tenant-safe spreadsheet reindex command. Review candidates first:

```powershell
.\venv\Scripts\python.exe -m scripts.reindex_spreadsheets --dry-run
```

Then process active CSV, XLSX, and XLS current versions in bounded batches:

```powershell
.\venv\Scripts\python.exe -m scripts.reindex_spreadsheets --batch-size 100
```

Use `--document-id`, `--owner-id`, or `--organization-id` to narrow a rollout,
`--retry-failed` for recorded failures, and `--force` only for a deliberate
repeat. The command preserves document/version IDs, replaces labeled vectors
under their stable point IDs, and restores the previous vector payloads if the
database update fails.

## One-time SQLite vector migration

Stop API and worker processes before migrating an embedded local Qdrant store.
Back up SQLite, uploaded objects, and Qdrant from the same maintenance window so
their document versions remain aligned. For a remote deployment, use provider
snapshots or an equivalent coordinated backup.

Run the read-only preflight from `backend`:

```powershell
.\.venv312\Scripts\python.exe -m scripts.migrate_vectors_to_qdrant
```

Review its JSON counts, then apply:

```powershell
.\.venv312\Scripts\python.exe -m scripts.migrate_vectors_to_qdrant --apply
```

Optional flags are `--organization-id`, `--upsert-batch-size`, and
`--smoke-query-limit`. The command reads only active chunks belonging to
completed current document versions. It reuses finite legacy vectors only when
their model and dimension match current configuration; all others are regenerated
in batches. It assigns deterministic UUIDv5 point IDs, performs idempotent batch
upserts without clearing the collection, verifies every expected point and
dimension, and runs tenant/ACL-filtered test queries. SQLite rows are marked
complete only after all verification succeeds; failures are recorded as
`indexing_status = 'failed'` and are safe to rerun.

The migration intentionally preserves `chunks.embedding`. Runtime ingestion and
retrieval do not depend on that JSON column; only transitional migration/reindex
tools may read it. Keep it for at least one validated production release and
successful rollback exercise. Remove it later through a separate backed-up
schema migration, never as part of the vector cutover.

The production responsibility boundary is:

| Component | Responsibility |
| --- | --- |
| SQLite | Users, organizations, documents, contents, authoritative chunk text, permissions, jobs, and index state |
| Qdrant | Embeddings, vector indexes, filtered semantic candidate retrieval |
| Object storage | Original uploaded files |
| Embedding model | Converts chunks and questions into compatible vectors |
| Reranker | Optionally reorders candidates and applies an evaluated relevance threshold |
| LLM | Answers only from approved retrieved context |

Backend code derives `owner_id` and `organization_id` from authenticated user
state; request payloads cannot choose either identity. Qdrant payload filtering
and SQLite hydration both enforce organization and ACL boundaries.

`GET /health` checks SQLite and the selected vector provider. With Qdrant active,
the response is:

```json
{
  "status": "healthy",
  "database": "connected",
  "qdrant": "connected",
  "ocr": {
    "status": "ready"
  }
}
```

`GET /health/ready` additionally returns collection mode, point count, vector
size, payload-index names, embedding status, and safe OCR readiness. Embedded
Qdrant persists locally but documents that payload indexes have no effect;
server/Cloud mode creates and reports the tenant and ACL payload indexes used by
production queries. If OCR is not configured, health responses report
`{"ocr":{"status":"unavailable"}}` without internal path details.

Run the read-only active-point reconciliation from `backend`:

```powershell
.\.venv312\Scripts\python.exe -m scripts.check_vector_consistency
```

It compares active completed current-version SQLite point IDs with active vector
store IDs, reports missing/unexpected points, and separately reports unique
content-chunk and duplicate-content-point counts. It never returns document text.

For the required 20–30-question rollout benchmark, provide a JSONL file whose
rows contain `question` and optional `expected_chunk_id`, then run:

```powershell
.\.venv312\Scripts\python.exe -m scripts.compare_vector_retrieval `
  --questions .\retrieval_questions.jsonl `
  --output .\retrieval_comparison.csv `
  --user-id 1 `
  --organization-id YOUR_ORGANIZATION_ID
```

The output records both providers' top chunk, score, correctness, and response
time. Stop an embedded-Qdrant API process before running this command because a
local persistence directory cannot be opened concurrently.

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

The isolated Qdrant lifecycle suite can be run independently:

```powershell
.\.venv312\Scripts\python.exe -m unittest tests.test_qdrant_integration -v
```

Image OCR requires the Tesseract executable on `PATH`, in a standard Windows
install location, or configured through `TESSERACT_CMD`. Required languages are
configured with `OCR_REQUIRED_LANGUAGES` and default to `eng`. Verify local OCR
from `backend` with:

```powershell
.\venv\Scripts\python.exe -c "from app.services.image_processor.ocr import ocr_health; print(ocr_health())"
```

For containers or servers, run the same check as part of deployment health
validation and budget CPU/memory for concurrent OCR work. OCR uses bounded
timeouts, image-count, byte-size, and pixel limits; production parser execution
also runs in a bounded subprocess. Optional vision analysis remains an
enhancement and must not be used to hide required local OCR dependency failures.
