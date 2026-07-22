"""SQLite setup and backward-compatible document-content migration."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

from app.utils.document_content import normalize_extracted_text

DATABASE_PATH = Path(__file__).resolve().parent.parent / "data" / "rag_new.db"
UPLOAD_DIRECTORY = DATABASE_PATH.parent / "uploads"


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
        CREATE UNIQUE INDEX IF NOT EXISTS idx_document_contents_owner_content_hash
            ON document_contents(owner_id, normalized_content_hash);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_content_chunk_index
            ON chunks(content_id, chunk_index);
        CREATE INDEX IF NOT EXISTS idx_collections_owner ON document_collections(owner_id);
        CREATE INDEX IF NOT EXISTS idx_batches_owner ON upload_batches(owner_id);
        CREATE INDEX IF NOT EXISTS idx_documents_owner_collection
            ON documents(owner_id, collection_id);
        """
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

    with get_connection() as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
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
            _create_indexes(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
