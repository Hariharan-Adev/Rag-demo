"""SQLite setup and backward-compatible document-content migration."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

from app.config import settings
from app.utils.document_content import normalize_extracted_text

DATABASE_PATH = Path(__file__).resolve().parent.parent / "data" / "rag_new.db"
UPLOAD_DIRECTORY = DATABASE_PATH.parent / "uploads"
DEFAULT_ORGANIZATION_ID = "00000000-0000-4000-8000-000000000001"


class ClosingConnection(sqlite3.Connection):
    """Commit or roll back like sqlite3.Connection, then always release the file handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def get_connection() -> sqlite3.Connection:
    """Open SQLite with foreign keys and a practical concurrent-write timeout."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=30, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


def _create_document_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS document_contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            file_hash TEXT NOT NULL,
            normalized_content_hash TEXT NOT NULL,
            extracted_text TEXT NOT NULL,
            processing_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (processing_status IN ('pending', 'processing', 'completed', 'failed')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            original_filename TEXT NOT NULL,
            display_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            content_id INTEGER NOT NULL,
            is_duplicate_content INTEGER NOT NULL DEFAULT 0 CHECK (is_duplicate_content IN (0, 1)),
            uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users(id),
            FOREIGN KEY (content_id) REFERENCES document_contents(id)
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            embedding TEXT,
            FOREIGN KEY (content_id) REFERENCES document_contents(id) ON DELETE CASCADE
        );
        """
    )


def _migrate_folder_schema(connection: sqlite3.Connection) -> None:
    """Add owner-scoped collections and upload batches without rebuilding user data."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS document_collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE (owner_id, name)
        );

        CREATE TABLE IF NOT EXISTS upload_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            collection_id INTEGER NOT NULL,
            original_folder_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            total_files INTEGER NOT NULL,
            total_bytes INTEGER NOT NULL DEFAULT 0,
            processed_files INTEGER NOT NULL DEFAULT 0,
            successful_files INTEGER NOT NULL DEFAULT 0,
            duplicate_files INTEGER NOT NULL DEFAULT 0,
            skipped_files INTEGER NOT NULL DEFAULT 0,
            failed_files INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (collection_id) REFERENCES document_collections(id) ON DELETE CASCADE
        );
        """
    )
    document_columns = _columns(connection, "documents")
    additions = {
        "collection_id": "INTEGER REFERENCES document_collections(id) ON DELETE SET NULL",
        "upload_batch_id": "INTEGER REFERENCES upload_batches(id) ON DELETE SET NULL",
        "relative_path": "TEXT",
        "processing_status": "TEXT NOT NULL DEFAULT 'completed'",
        "processing_error": "TEXT",
    }
    for column, definition in additions.items():
        if column not in document_columns:
            connection.execute(f"ALTER TABLE documents ADD COLUMN {column} {definition}")


def _migrate_workbook_schema(connection: sqlite3.Connection) -> None:
    """Add owner-scoped structured spreadsheet storage and chunk provenance."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS workbook_sheets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL,
            sheet_index INTEGER NOT NULL,
            name TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'visible',
            status TEXT NOT NULL,
            header_row INTEGER,
            headers_json TEXT NOT NULL DEFAULT '[]',
            processing_error TEXT,
            FOREIGN KEY (content_id) REFERENCES document_contents(id) ON DELETE CASCADE,
            FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE (content_id, sheet_index)
        );

        CREATE TABLE IF NOT EXISTS workbook_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet_id INTEGER NOT NULL,
            content_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL,
            row_number INTEGER NOT NULL,
            values_json TEXT NOT NULL,
            FOREIGN KEY (sheet_id) REFERENCES workbook_sheets(id) ON DELETE CASCADE,
            FOREIGN KEY (content_id) REFERENCES document_contents(id) ON DELETE CASCADE,
            FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE (sheet_id, row_number)
        );
        """
    )
    chunk_columns = _columns(connection, "chunks")
    if "sheet_name" not in chunk_columns:
        connection.execute("ALTER TABLE chunks ADD COLUMN sheet_name TEXT")
    if "row_number" not in chunk_columns:
        connection.execute("ALTER TABLE chunks ADD COLUMN row_number INTEGER")


def _legacy_file_hash(document: sqlite3.Row) -> str:
    stored_filename = document["stored_filename"]
    if stored_filename:
        candidate = (UPLOAD_DIRECTORY / stored_filename).resolve()
        try:
            candidate.relative_to(UPLOAD_DIRECTORY.resolve())
        except ValueError:
            candidate = Path()
        if candidate.is_file():
            return hashlib.sha256(candidate.read_bytes()).hexdigest()
    fallback = f"legacy-file:{document['owner_id']}:{document['id']}"
    return hashlib.sha256(fallback.encode()).hexdigest()


def _unique_legacy_name(used: set[str], filename: str) -> str:
    path = Path(filename)
    stem, extension = path.stem or "document", path.suffix
    candidate = filename or f"document{extension}"
    suffix = 0
    while candidate.casefold() in used:
        suffix += 1
        candidate = f"{stem}({suffix}){extension}"
    used.add(candidate.casefold())
    return candidate


def _migrate_legacy_documents(connection: sqlite3.Connection) -> None:
    """Move document-owned chunks into shared content records without dropping user data."""
    legacy_documents = connection.execute(
        "SELECT id, filename, stored_filename, owner_id, created_at FROM documents ORDER BY id"
    ).fetchall()
    legacy_chunks = connection.execute(
        "SELECT id, document_id, content, chunk_index, embedding FROM chunks ORDER BY document_id, chunk_index, id"
    ).fetchall()
    chunks_by_document: dict[int, list[sqlite3.Row]] = {}
    for chunk in legacy_chunks:
        chunks_by_document.setdefault(int(chunk["document_id"]), []).append(chunk)

    connection.executescript(
        """
        CREATE TABLE document_contents_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            file_hash TEXT NOT NULL,
            normalized_content_hash TEXT NOT NULL,
            extracted_text TEXT NOT NULL,
            processing_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (processing_status IN ('pending', 'processing', 'completed', 'failed')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        );
        CREATE TABLE documents_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            original_filename TEXT NOT NULL,
            display_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            content_id INTEGER NOT NULL,
            is_duplicate_content INTEGER NOT NULL DEFAULT 0 CHECK (is_duplicate_content IN (0, 1)),
            uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users(id),
            FOREIGN KEY (content_id) REFERENCES document_contents_new(id)
        );
        CREATE TABLE chunks_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            embedding TEXT,
            FOREIGN KEY (content_id) REFERENCES document_contents_new(id) ON DELETE CASCADE
        );
        """
    )

    content_by_hash: dict[tuple[int, str], int] = {}
    used_names: dict[int, set[str]] = {}
    for document in legacy_documents:
        owner_id = int(document["owner_id"] or 0)
        document_chunks = chunks_by_document.get(int(document["id"]), [])
        extracted_text = "\n\n".join(str(chunk["content"]) for chunk in document_chunks)
        normalized_text = normalize_extracted_text(extracted_text)
        normalized_hash = hashlib.sha256(
            (normalized_text or f"legacy-empty:{owner_id}:{document['id']}").encode("utf-8")
        ).hexdigest()
        file_hash = _legacy_file_hash(document)
        content_key = (owner_id, normalized_hash)
        content_id = content_by_hash.get(content_key)
        duplicate = content_id is not None

        if content_id is None:
            cursor = connection.execute(
                """
                INSERT INTO document_contents_new
                    (owner_id, file_hash, normalized_content_hash, extracted_text, processing_status, created_at)
                VALUES (?, ?, ?, ?, 'completed', ?)
                """,
                (owner_id, file_hash, normalized_hash, normalized_text, document["created_at"]),
            )
            content_id = int(cursor.lastrowid)
            content_by_hash[content_key] = content_id
            connection.executemany(
                """
                INSERT INTO chunks_new (id, content_id, chunk_index, text, embedding)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (chunk["id"], content_id, chunk["chunk_index"], chunk["content"], chunk["embedding"])
                    for chunk in document_chunks
                ],
            )

        original_filename = str(document["filename"] or "document")
        display_filename = _unique_legacy_name(
            used_names.setdefault(owner_id, set()), original_filename
        )
        connection.execute(
            """
            INSERT INTO documents_new
                (id, owner_id, original_filename, display_filename, stored_filename,
                 file_hash, content_id, is_duplicate_content, uploaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document["id"], owner_id, original_filename, display_filename,
                document["stored_filename"] or "", file_hash, content_id,
                int(duplicate), document["created_at"],
            ),
        )

    connection.executescript(
        """
        DROP TABLE chunks;
        DROP TABLE documents;
        ALTER TABLE document_contents_new RENAME TO document_contents;
        ALTER TABLE documents_new RENAME TO documents;
        ALTER TABLE chunks_new RENAME TO chunks;
        """
    )


def _create_indexes(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_owner_display_filename
            ON documents(owner_id, display_filename);
        CREATE INDEX IF NOT EXISTS idx_documents_owner_file_hash
            ON documents(owner_id, file_hash);
        CREATE INDEX IF NOT EXISTS idx_documents_owner_content_id
            ON documents(owner_id, content_id);
        CREATE INDEX IF NOT EXISTS idx_document_contents_owner_file_hash
            ON document_contents(owner_id, file_hash);
        CREATE INDEX IF NOT EXISTS idx_collections_owner ON document_collections(owner_id);
        CREATE INDEX IF NOT EXISTS idx_batches_owner ON upload_batches(owner_id);
        CREATE INDEX IF NOT EXISTS idx_documents_owner_collection
            ON documents(owner_id, collection_id);
        CREATE INDEX IF NOT EXISTS idx_workbook_sheets_owner_content
            ON workbook_sheets(owner_id, content_id);
        CREATE INDEX IF NOT EXISTS idx_workbook_rows_owner_content
            ON workbook_rows(owner_id, content_id);
        CREATE INDEX IF NOT EXISTS idx_workbook_rows_sheet
            ON workbook_rows(sheet_id, row_number);
        """
    )


def _add_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    """Add one column when upgrading an existing SQLite database."""
    if _table_exists(connection, table) and column not in _columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_multitenant_architecture(connection: sqlite3.Connection) -> None:
    """Data-preserving v2 migration for tenants, versions, jobs, and provenance."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS organizations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TEXT
        );
        """
    )
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version = '002_multitenant_rag'"
    ).fetchone():
        return

    default_organization_id = DEFAULT_ORGANIZATION_ID
    connection.execute(
        "INSERT INTO organizations (id, name) VALUES (?, ?)",
        (default_organization_id, settings.default_organization_name),
    )

    tenant_tables = (
        "users", "documents", "document_contents", "chunks",
        "document_collections", "upload_batches", "workbook_sheets",
        "workbook_rows", "audit_events", "llm_usage", "rate_limit_windows",
    )
    for table in tenant_tables:
        _add_column(
            connection,
            table,
            "organization_id",
            f"TEXT NOT NULL DEFAULT '{DEFAULT_ORGANIZATION_ID}'",
        )
        connection.execute(
            f"UPDATE {table} SET organization_id = ? WHERE organization_id IS NULL",
            (default_organization_id,),
        )

    _add_column(connection, "users", "role", "TEXT NOT NULL DEFAULT 'member'")
    _add_column(connection, "users", "deleted_at", "TEXT")

    # Remove the legacy global email uniqueness and enforce tenant-scoped identity.
    connection.executescript(
        """
        CREATE TABLE users_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            organization_id TEXT NOT NULL
                DEFAULT '00000000-0000-4000-8000-000000000001',
            role TEXT NOT NULL DEFAULT 'member'
                CHECK (role IN ('member','organization_admin')),
            deleted_at TEXT,
            FOREIGN KEY (organization_id) REFERENCES organizations(id),
            UNIQUE (organization_id, email)
        );
        INSERT INTO users_v2
            (id, email, password_hash, created_at, organization_id, role, deleted_at)
        SELECT id, email, password_hash, created_at, organization_id,
               COALESCE(role, 'member'), deleted_at
        FROM users;
        DROP TABLE users;
        ALTER TABLE users_v2 RENAME TO users;
        """
    )

    _add_column(connection, "documents", "visibility", "TEXT NOT NULL DEFAULT 'private'")
    _add_column(connection, "documents", "current_version_id", "INTEGER")
    _add_column(connection, "documents", "deleted_at", "TEXT")
    _add_column(connection, "document_contents", "deleted_at", "TEXT")
    _add_column(connection, "chunks", "document_id", "INTEGER")
    _add_column(connection, "chunks", "version_id", "INTEGER")
    _add_column(connection, "chunks", "source_type", "TEXT")
    _add_column(connection, "chunks", "source_location_json", "TEXT NOT NULL DEFAULT '{}'")
    _add_column(connection, "chunks", "deleted_at", "TEXT")
    _add_column(connection, "audit_events", "request_id", "TEXT")
    _add_column(connection, "audit_events", "job_id", "TEXT")

    connection.executescript(
        """
        CREATE TABLE document_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id TEXT NOT NULL,
            document_id INTEGER NOT NULL,
            version_number INTEGER NOT NULL,
            content_id INTEGER,
            stored_filename TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            normalized_content_hash TEXT,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','processing','completed','failed','cancelled')),
            processing_error_code TEXT,
            processing_error_message TEXT,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            deleted_at TEXT,
            FOREIGN KEY (organization_id) REFERENCES organizations(id),
            FOREIGN KEY (document_id) REFERENCES documents(id),
            FOREIGN KEY (content_id) REFERENCES document_contents(id),
            FOREIGN KEY (created_by) REFERENCES users(id),
            UNIQUE (organization_id, document_id, version_number)
        );
        CREATE TABLE ingestion_jobs (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            document_id INTEGER,
            version_id INTEGER,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','processing','retry_scheduled','completed','failed','cancelled')),
            idempotency_key TEXT NOT NULL,
            job_type TEXT NOT NULL DEFAULT 'document_ingestion',
            payload_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            locked_by TEXT,
            locked_at TEXT,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (organization_id) REFERENCES organizations(id),
            FOREIGN KEY (owner_id) REFERENCES users(id),
            FOREIGN KEY (document_id) REFERENCES documents(id),
            FOREIGN KEY (version_id) REFERENCES document_versions(id),
            UNIQUE (organization_id, idempotency_key)
        );
        CREATE TABLE document_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id TEXT NOT NULL,
            document_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            permission TEXT NOT NULL CHECK (permission IN ('read','manage')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (document_id) REFERENCES documents(id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            UNIQUE (organization_id, document_id, user_id, permission)
        );
        CREATE TABLE chat_sessions (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TEXT,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        );
        CREATE TABLE chat_messages (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user','assistant')),
            content TEXT NOT NULL,
            citations_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TEXT,
            FOREIGN KEY (session_id) REFERENCES chat_sessions(id)
        );
        """
    )

    legacy_documents = connection.execute(
        """SELECT id, organization_id, owner_id, content_id, stored_filename,
                  file_hash, processing_status, uploaded_at
           FROM documents ORDER BY id"""
    ).fetchall()
    for document in legacy_documents:
        content = connection.execute(
            """SELECT normalized_content_hash, processing_status
               FROM document_contents WHERE id = ?""",
            (document["content_id"],),
        ).fetchone()
        status = "completed"
        if content and content["processing_status"] in {"pending", "processing", "failed"}:
            status = str(content["processing_status"]).replace("pending", "queued")
        cursor = connection.execute(
            """INSERT INTO document_versions
               (organization_id, document_id, version_number, content_id,
                stored_filename, file_hash, normalized_content_hash, status,
                created_by, created_at, completed_at)
               VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'completed' THEN ? END)""",
            (
                document["organization_id"], document["id"], document["content_id"],
                document["stored_filename"], document["file_hash"],
                content["normalized_content_hash"] if content else None, status,
                document["owner_id"], document["uploaded_at"], status,
                document["uploaded_at"],
            ),
        )
        version_id = int(cursor.lastrowid)
        connection.execute(
            "UPDATE documents SET current_version_id = ? WHERE id = ?",
            (version_id, document["id"]),
        )
        connection.execute(
            """UPDATE chunks
               SET organization_id = ?, document_id = ?, version_id = ?,
                   source_type = COALESCE(source_type, 'text')
               WHERE content_id = ?""",
            (
                document["organization_id"], document["id"], version_id,
                document["content_id"],
            ),
        )

    connection.executescript(
        """
        DROP INDEX IF EXISTS idx_chunks_content_chunk_index;
        CREATE UNIQUE INDEX idx_chunks_content_version_index
            ON chunks(content_id, version_id, chunk_index);
        CREATE INDEX idx_users_org_email ON users(organization_id, email);
        CREATE INDEX idx_documents_org_owner ON documents(organization_id, owner_id);
        CREATE INDEX idx_documents_org_visibility ON documents(organization_id, visibility, deleted_at);
        CREATE INDEX idx_versions_org_document ON document_versions(organization_id, document_id, deleted_at);
        CREATE INDEX idx_contents_org_hash ON document_contents(organization_id, normalized_content_hash);
        CREATE INDEX idx_chunks_org_document_version ON chunks(organization_id, document_id, version_id, deleted_at);
        CREATE INDEX idx_jobs_org_status_available ON ingestion_jobs(organization_id, status, available_at);
        CREATE INDEX idx_audit_org_created ON audit_events(organization_id, created_at);
        CREATE INDEX idx_usage_org_user ON llm_usage(organization_id, user_id);
        """
    )
    connection.execute(
        "INSERT INTO schema_migrations (version) VALUES ('002_multitenant_rag')"
    )


def _migrate_operational_v3(connection: sqlite3.Connection) -> None:
    """Scope quota keys by tenant and add durable structured job results."""
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version = '003_operational_jobs'"
    ).fetchone():
        return
    _add_column(connection, "ingestion_jobs", "result_json", "TEXT")
    connection.executescript(
        f"""
        CREATE TABLE rate_limit_windows_v3 (
            organization_id TEXT NOT NULL DEFAULT '{DEFAULT_ORGANIZATION_ID}',
            scope TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (organization_id, scope, endpoint, window_start)
        );
        INSERT INTO rate_limit_windows_v3
            (organization_id, scope, endpoint, window_start, request_count)
        SELECT organization_id, scope, endpoint, window_start, request_count
        FROM rate_limit_windows;
        DROP TABLE rate_limit_windows;
        ALTER TABLE rate_limit_windows_v3 RENAME TO rate_limit_windows;

        CREATE TABLE llm_usage_v3 (
            organization_id TEXT NOT NULL DEFAULT '{DEFAULT_ORGANIZATION_ID}',
            user_id INTEGER NOT NULL,
            usage_date TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (organization_id, user_id, usage_date)
        );
        INSERT INTO llm_usage_v3
            (organization_id, user_id, usage_date, request_count,
             prompt_tokens, completion_tokens)
        SELECT organization_id, user_id, usage_date, request_count,
               prompt_tokens, completion_tokens
        FROM llm_usage;
        DROP TABLE llm_usage;
        ALTER TABLE llm_usage_v3 RENAME TO llm_usage;
        """
    )
    connection.execute(
        "INSERT INTO schema_migrations (version) VALUES ('003_operational_jobs')"
    )


def _migrate_document_lifecycle_v4(connection: sqlite3.Connection) -> None:
    """Add explicit lifecycle attribution and per-version processing metadata."""
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version = '004_document_lifecycle'"
    ).fetchone():
        return

    for table in ("documents", "document_versions", "document_contents", "chunks"):
        _add_column(connection, table, "deleted_by", "INTEGER")
    for table in ("document_versions", "document_contents", "chunks"):
        _add_column(
            connection,
            table,
            "deleted_with_document",
            "INTEGER NOT NULL DEFAULT 0",
        )

    _add_column(connection, "documents", "updated_at", "TEXT")
    connection.execute(
        "UPDATE documents SET updated_at = COALESCE(updated_at, uploaded_at)"
    )

    version_columns = {
        "storage_key": "TEXT",
        "mime_type": "TEXT",
        "file_size": "INTEGER",
        "ingestion_status": "TEXT NOT NULL DEFAULT 'queued'",
        "extraction_status": "TEXT NOT NULL DEFAULT 'queued'",
        "indexing_status": "TEXT NOT NULL DEFAULT 'queued'",
        "failure_reason": "TEXT",
    }
    for column, definition in version_columns.items():
        _add_column(connection, "document_versions", column, definition)
    connection.execute(
        """UPDATE document_versions
           SET storage_key = COALESCE(storage_key, stored_filename),
               ingestion_status = CASE status
                   WHEN 'completed' THEN 'completed'
                   WHEN 'failed' THEN 'failed'
                   WHEN 'cancelled' THEN 'cancelled'
                   WHEN 'processing' THEN 'processing'
                   ELSE 'queued'
               END,
               extraction_status = CASE
                   WHEN status = 'completed' THEN 'completed'
                   WHEN status = 'failed' THEN 'failed'
                   WHEN status = 'cancelled' THEN 'cancelled'
                   ELSE extraction_status
               END,
               indexing_status = CASE
                   WHEN status = 'completed' THEN 'completed'
                   WHEN status = 'failed' THEN 'failed'
                   WHEN status = 'cancelled' THEN 'cancelled'
                   ELSE indexing_status
               END,
               failure_reason = COALESCE(
                   failure_reason, processing_error_message
               )"""
    )

    _add_column(
        connection, "document_contents", "parser_version", "TEXT NOT NULL DEFAULT '1'"
    )
    _add_column(connection, "chunks", "token_count", "INTEGER")
    _add_column(connection, "chunks", "vector_point_id", "TEXT")
    _add_column(connection, "chunks", "created_at", "TEXT")
    connection.execute(
        """UPDATE chunks
           SET token_count = COALESCE(
                   token_count,
                   CASE WHEN trim(text) = '' THEN 0
                        ELSE length(trim(text)) - length(replace(trim(text), ' ', '')) + 1
                   END
               ),
               created_at = COALESCE(created_at, CURRENT_TIMESTAMP)"""
    )

    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_documents_org_deleted_updated
            ON documents(organization_id, deleted_at, updated_at);
        CREATE INDEX IF NOT EXISTS idx_versions_org_status
            ON document_versions(organization_id, ingestion_status, deleted_at);
        CREATE INDEX IF NOT EXISTS idx_contents_org_deleted
            ON document_contents(organization_id, deleted_at);
        CREATE INDEX IF NOT EXISTS idx_chunks_org_vector_point
            ON chunks(organization_id, vector_point_id);
        """
    )
    connection.execute(
        "INSERT INTO schema_migrations (version) VALUES ('004_document_lifecycle')"
    )


def _migrate_pipeline_contract_v5(connection: sqlite3.Connection) -> None:
    """Add pipeline-idempotency and validated indexing metadata."""
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version = '005_pipeline_contract'"
    ).fetchone():
        return
    _add_column(connection, "ingestion_jobs", "request_idempotency_key", "TEXT")
    _add_column(
        connection,
        "ingestion_jobs",
        "pipeline_version",
        "TEXT NOT NULL DEFAULT 'v1'",
    )
    _add_column(connection, "ingestion_jobs", "next_retry_at", "TEXT")
    _add_column(connection, "ingestion_jobs", "last_error_code", "TEXT")
    _add_column(connection, "ingestion_jobs", "last_error_message", "TEXT")
    _add_column(
        connection,
        "document_versions",
        "source_metadata_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    _add_column(connection, "chunks", "embedding_model", "TEXT")
    _add_column(connection, "chunks", "embedding_dimension", "INTEGER")
    connection.execute(
        """UPDATE ingestion_jobs
           SET request_idempotency_key = COALESCE(
                   request_idempotency_key, idempotency_key
               ),
               last_error_code = COALESCE(last_error_code, error_code),
               last_error_message = COALESCE(last_error_message, error_message),
               next_retry_at = COALESCE(next_retry_at, available_at),
               pipeline_version = ?""",
        (settings.ingestion_pipeline_version,),
    )
    connection.execute(
        """UPDATE chunks
           SET embedding_model = COALESCE(embedding_model, ?),
               embedding_dimension = COALESCE(embedding_dimension, ?)""",
        (settings.embedding_model_version, settings.embedding_dimension),
    )
    connection.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_org_request_idempotency
            ON ingestion_jobs(organization_id, request_idempotency_key)
            WHERE request_idempotency_key IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_org_pipeline
            ON ingestion_jobs(organization_id, version_id, job_type, pipeline_version);
        CREATE INDEX IF NOT EXISTS idx_chunks_org_version_model
            ON chunks(organization_id, version_id, embedding_model, deleted_at);
        """
    )
    connection.execute(
        "INSERT INTO schema_migrations (version) VALUES ('005_pipeline_contract')"
    )


def _validate_database_integrity(connection: sqlite3.Connection) -> None:
    """Fail the migration transaction if tenant or reference invariants are broken."""
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    if len(integrity) != 1 or str(integrity[0][0]).lower() != "ok":
        raise RuntimeError("Database migration validation failed: integrity check.")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            f"Database migration validation found {len(violations)} reference violation(s)."
        )
    checks = (
        (
            """SELECT COUNT(*) FROM users u
               LEFT JOIN organizations o ON o.id = u.organization_id
               WHERE u.organization_id IS NULL OR o.id IS NULL""",
            "user organization",
        ),
        (
            """SELECT COUNT(*) FROM documents d
               JOIN users u ON u.id = d.owner_id
               WHERE d.organization_id IS NULL
                  OR d.organization_id <> u.organization_id""",
            "document tenant ownership",
        ),
        (
            """SELECT COUNT(*) FROM document_versions v
               JOIN documents d ON d.id = v.document_id
               WHERE v.organization_id <> d.organization_id""",
            "version tenant ownership",
        ),
        (
            """SELECT COUNT(*) FROM documents d
               JOIN document_versions v ON v.id = d.current_version_id
               WHERE d.current_version_id IS NOT NULL
                 AND v.document_id <> d.id""",
            "current version ownership",
        ),
    )
    for query, label in checks:
        count = int(connection.execute(query).fetchone()[0])
        if count:
            raise RuntimeError(
                f"Database migration validation failed: {label} ({count})."
            )


def _migrate_observability_v6(connection: sqlite3.Connection) -> None:
    """Persist stage metrics without changing existing job lifecycle data."""
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version = '006_ingestion_metrics'"
    ).fetchone():
        return
    for name, definition in (
        ("extraction_duration_ms", "REAL"),
        ("embedding_duration_ms", "REAL"),
        ("indexing_duration_ms", "REAL"),
        ("chunks_created", "INTEGER NOT NULL DEFAULT 0"),
        ("vector_upsert_failures", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _add_column(connection, "ingestion_jobs", name, definition)
    connection.execute(
        "INSERT INTO schema_migrations (version) VALUES ('006_ingestion_metrics')"
    )


def _migrate_lifecycle_repair_v7(connection: sqlite3.Connection) -> None:
    """Repair databases where the v4 ledger predates lifecycle cascade columns."""
    required_tables = ("document_versions", "document_contents", "chunks")
    complete = all(
        "deleted_with_document" in _columns(connection, table)
        for table in required_tables
    )
    recorded = connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version = '007_lifecycle_repair'"
    ).fetchone()
    if complete and recorded:
        return
    for table in required_tables:
        _add_column(
            connection,
            table,
            "deleted_with_document",
            "INTEGER NOT NULL DEFAULT 0",
        )
    connection.execute(
        """INSERT OR IGNORE INTO schema_migrations (version)
           VALUES ('007_lifecycle_repair')"""
    )


def _migrate_active_content_indexes_v8(connection: sqlite3.Connection) -> None:
    """Make content uniqueness lifecycle-aware and keep chunks version-scoped."""
    required = {
        "document_contents": {"owner_id", "normalized_content_hash", "deleted_at"},
        "chunks": {"content_id", "version_id", "chunk_index"},
    }
    for table, columns in required.items():
        missing = columns - _columns(connection, table)
        if missing:
            raise RuntimeError(
                f"Migration 008 cannot run; {table} is missing required columns."
            )

    active_duplicates = int(connection.execute(
        """SELECT COUNT(*) FROM (
             SELECT owner_id, normalized_content_hash
             FROM document_contents
             WHERE deleted_at IS NULL
             GROUP BY owner_id, normalized_content_hash
             HAVING COUNT(*) > 1
           )"""
    ).fetchone()[0])
    if active_duplicates:
        raise RuntimeError(
            "Migration 008 found active duplicate content identities; "
            "resolve them before restarting."
        )

    chunk_duplicates = int(connection.execute(
        """SELECT COUNT(*) FROM (
             SELECT content_id, version_id, chunk_index
             FROM chunks
             GROUP BY content_id, version_id, chunk_index
             HAVING COUNT(*) > 1
           )"""
    ).fetchone()[0])
    if chunk_duplicates:
        raise RuntimeError(
            "Migration 008 found duplicate version chunk indexes; "
            "resolve them before restarting."
        )

    connection.execute("DROP INDEX IF EXISTS idx_document_contents_owner_content_hash")
    connection.execute("DROP INDEX IF EXISTS idx_chunks_content_chunk_index")
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS
               idx_document_contents_owner_active_content_hash
           ON document_contents(owner_id, normalized_content_hash)
           WHERE deleted_at IS NULL"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_content_version_index
           ON chunks(content_id, version_id, chunk_index)"""
    )
    connection.execute(
        """INSERT OR IGNORE INTO schema_migrations (version)
           VALUES ('008_active_content_indexes')"""
    )


def _migrate_chunk_vector_sync_v9(connection: sqlite3.Connection) -> None:
    """Track idempotent Qdrant synchronization for every chunk."""
    required = {"id", "vector_point_id"}
    if missing := required - _columns(connection, "chunks"):
        raise RuntimeError(
            "Migration 009 cannot run; chunks is missing required columns: "
            + ", ".join(sorted(missing))
        )
    already_applied = connection.execute(
        """SELECT 1 FROM schema_migrations
           WHERE version = '009_chunk_vector_sync'"""
    ).fetchone() is not None
    _add_column(connection, "chunks", "qdrant_indexed_at", "TEXT")
    _add_column(
        connection,
        "chunks",
        "indexing_status",
        "TEXT NOT NULL DEFAULT 'pending'",
    )
    if not already_applied:
        connection.execute(
            """UPDATE chunks
               SET indexing_status = CASE
                   WHEN vector_point_id IS NOT NULL THEN 'completed'
                   ELSE 'pending'
               END"""
        )
    duplicate_ids = int(connection.execute(
        """SELECT COUNT(*) FROM (
             SELECT vector_point_id
             FROM chunks
             WHERE vector_point_id IS NOT NULL
             GROUP BY vector_point_id
             HAVING COUNT(*) > 1
           )"""
    ).fetchone()[0])
    if duplicate_ids:
        raise RuntimeError(
            "Migration 009 found duplicate chunk vector point IDs; "
            "reindex them before restarting."
        )
    connection.execute("DROP INDEX IF EXISTS idx_chunks_org_vector_point")
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_chunks_vector_point_id
           ON chunks(vector_point_id)
           WHERE vector_point_id IS NOT NULL"""
    )
    connection.execute(
        """INSERT OR IGNORE INTO schema_migrations (version)
           VALUES ('009_chunk_vector_sync')"""
    )


def initialize_database() -> None:
    """Create current tables and migrate legacy document-owned chunks once."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_legacy = False
    if DATABASE_PATH.exists():
        with sqlite3.connect(DATABASE_PATH, factory=ClosingConnection) as probe:
            is_legacy = _table_exists(probe, "documents") and "content_id" not in _columns(probe, "documents")
        if is_legacy:
            backup = DATABASE_PATH.with_suffix(DATABASE_PATH.suffix + ".pre_content_refactor.bak")
            if not backup.exists():
                shutil.copy2(DATABASE_PATH, backup)
        with sqlite3.connect(DATABASE_PATH, factory=ClosingConnection) as probe:
            needs_multitenant_backup = (
                not _table_exists(probe, "schema_migrations")
                or probe.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = '002_multitenant_rag'"
                ).fetchone() is None
            )
        if needs_multitenant_backup:
            backup = DATABASE_PATH.with_suffix(
                DATABASE_PATH.suffix + ".pre_multitenant_rag.bak"
            )
            if not backup.exists():
                shutil.copy2(DATABASE_PATH, backup)

    with get_connection() as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS rate_limit_windows (
                    scope TEXT NOT NULL, endpoint TEXT NOT NULL, window_start INTEGER NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (scope, endpoint, window_start)
                );
                CREATE TABLE IF NOT EXISTS llm_usage (
                    user_id INTEGER NOT NULL, usage_date TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, usage_date)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                    event_type TEXT NOT NULL, endpoint TEXT NOT NULL, outcome TEXT NOT NULL,
                    ip_hash TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            if is_legacy:
                _migrate_legacy_documents(connection)
            else:
                _create_document_schema(connection)
            _migrate_folder_schema(connection)
            _migrate_workbook_schema(connection)
            _create_indexes(connection)
            _migrate_multitenant_architecture(connection)
            _migrate_operational_v3(connection)
            _migrate_document_lifecycle_v4(connection)
            _migrate_pipeline_contract_v5(connection)
            _migrate_observability_v6(connection)
            _migrate_lifecycle_repair_v7(connection)
            _migrate_active_content_indexes_v8(connection)
            _migrate_chunk_vector_sync_v9(connection)
            _validate_database_integrity(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
