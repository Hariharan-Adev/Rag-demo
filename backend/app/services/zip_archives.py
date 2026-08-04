"""Security-first ZIP inspection and bounded member extraction."""

from __future__ import annotations

import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from app.config import settings
from app.utils.document_content import sanitize_filename

ARCHIVE_TEMP_ROOT = Path(tempfile.gettempdir()) / "docsense-rag-archives"
ZIP_DOCUMENT_EXTENSIONS = {
    ".pdf", ".docx", ".txt", ".md", ".json", ".xml", ".html", ".htm",
    ".csv", ".xls", ".xlsx", ".ppt", ".pptx",
    ".png", ".jpg", ".jpeg", ".webp",
}
NESTED_ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".gz"}
DANGEROUS_EXTENSIONS = {
    ".exe", ".dll", ".bat", ".cmd", ".ps1", ".sh", ".py", ".js",
    ".jar", ".msi", ".apk",
}
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class ArchiveValidationError(ValueError):
    """Raised when an archive-level security rule fails."""


@dataclass(frozen=True)
class ArchiveMember:
    info: zipfile.ZipInfo
    filename: str
    rejection_reason: str | None = None


@dataclass(frozen=True)
class ArchivePlan:
    members: tuple[ArchiveMember, ...]
    total_entries: int
    total_extracted_size: int


def temporary_archive_directory():
    """Return a self-cleaning server-controlled workspace for one archive."""
    ARCHIVE_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="archive-", dir=ARCHIVE_TEMP_ROOT)


def _validate_member_path(name: str) -> None:
    if (
        not name
        or "\\" in name
        or _CONTROL_CHARACTERS.search(name)
        or name.startswith("/")
        or _DRIVE_PREFIX.match(name)
    ):
        raise ArchiveValidationError("Archive failed security validation.")
    parts = PurePosixPath(name).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ArchiveValidationError("Archive failed security validation.")


def _is_link_or_special(info: zipfile.ZipInfo) -> bool:
    unix_mode = info.external_attr >> 16
    if unix_mode:
        kind = stat.S_IFMT(unix_mode)
        if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
            return True
    # DOS reparse-point flag. ZIP cannot safely represent hard links or junctions.
    return bool(info.external_attr & 0x400)


def inspect_archive(archive_path: Path) -> ArchivePlan:
    """Validate the complete archive directory before extracting any member."""
    if archive_path.stat().st_size > settings.max_zip_upload_mb * 1024 * 1024:
        raise ArchiveValidationError("Archive exceeds maximum allowed size.")
    if not zipfile.is_zipfile(archive_path):
        raise ArchiveValidationError("Archive failed security validation.")

    members: list[ArchiveMember] = []
    total_uncompressed = 0
    total_compressed = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = [info for info in archive.infolist() if not info.is_dir()]
            if not entries:
                raise ArchiveValidationError("The archive contains no files.")
            if len(entries) > settings.max_zip_files:
                raise ArchiveValidationError("Too many files in archive.")
            for info in entries:
                _validate_member_path(info.filename)
                if info.flag_bits & 0x1:
                    raise ArchiveValidationError("Password protected archives are not supported.")
                if _is_link_or_special(info):
                    raise ArchiveValidationError("Archive links are not supported.")
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}:
                    raise ArchiveValidationError("Archive uses an unsupported compression method.")

                suffix = Path(info.filename).suffix.lower()
                if suffix in NESTED_ARCHIVE_EXTENSIONS:
                    raise ArchiveValidationError("Nested archives are not supported.")
                total_uncompressed += info.file_size
                total_compressed += info.compress_size
                if total_uncompressed > settings.max_zip_extracted_mb * 1024 * 1024:
                    raise ArchiveValidationError("Archive exceeds maximum extracted size.")
                if info.file_size and (not info.compress_size or info.file_size / info.compress_size > settings.max_zip_compression_ratio):
                    raise ArchiveValidationError("Archive compression ratio exceeds the safe limit.")

                basename = PurePosixPath(info.filename).name
                if suffix in DANGEROUS_EXTENSIONS:
                    members.append(ArchiveMember(info, basename or "rejected", "Unsupported file type"))
                elif suffix not in ZIP_DOCUMENT_EXTENSIONS:
                    members.append(ArchiveMember(info, basename or "rejected", "Unsupported file type"))
                elif info.file_size > settings.max_file_size_mb * 1024 * 1024:
                    members.append(ArchiveMember(info, basename, "Individual file too large"))
                elif info.file_size == 0:
                    members.append(ArchiveMember(info, basename, "The extracted file is empty"))
                else:
                    try:
                        safe_name = sanitize_filename(basename, ZIP_DOCUMENT_EXTENSIONS)
                    except ValueError:
                        members.append(ArchiveMember(info, basename or "rejected", "Unsupported file type"))
                    else:
                        members.append(ArchiveMember(info, safe_name))
    except zipfile.BadZipFile as error:
        raise ArchiveValidationError("Archive failed security validation.") from error

    if total_uncompressed and (not total_compressed or total_uncompressed / total_compressed > settings.max_zip_compression_ratio):
        raise ArchiveValidationError("Archive compression ratio exceeds the safe limit.")
    return ArchivePlan(tuple(members), len(members), total_uncompressed)


def extract_member(archive: zipfile.ZipFile, member: ArchiveMember, destination: Path) -> Path:
    """Stream one approved member to a server-named file with an enforced byte limit."""
    if member.rejection_reason is not None:
        raise ValueError("Rejected members cannot be extracted.")
    target = destination / f"{uuid4().hex}{Path(member.filename).suffix.lower()}"
    written = 0
    limit = settings.max_file_size_mb * 1024 * 1024
    try:
        with archive.open(member.info) as source, target.open("xb") as output:
            while chunk := source.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise ArchiveValidationError("Individual file too large")
                output.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if written != member.info.file_size:
        target.unlink(missing_ok=True)
        raise ArchiveValidationError("Archive failed security validation.")
    return target
