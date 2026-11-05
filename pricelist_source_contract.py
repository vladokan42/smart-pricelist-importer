"""Standalone pure extraction from the existing importer."""
from __future__ import annotations
import zipfile
from pathlib import Path
from typing import Any

SUPPORTED = {'.csv', '.xlsx', '.xlsm', '.xls'}


MAX_BYTES = 50 * 1024 * 1024


MAX_ZIP_ENTRIES = 20_000


MAX_ZIP_UNCOMPRESSED_BYTES = 250 * 1024 * 1024


MAX_ZIP_ENTRY_BYTES = 100 * 1024 * 1024


MAX_ZIP_COMPRESSION_RATIO = 1_000


def detect_format(path: Path) -> tuple[str, list[str]]:
    suffix = path.suffix.casefold()
    issues: list[str] = []
    if suffix not in SUPPORTED:
        return 'unsupported', ['unsupported_extension']
    prefix = path.read_bytes()[:8]
    if suffix in {'.xlsx', '.xlsm'}:
        if not zipfile.is_zipfile(path):
            issues.append('xlsx_signature_mismatch')
        return suffix[1:], issues
    if suffix == '.xls':
        if not prefix.startswith(bytes.fromhex('D0CF11E0A1B11AE1')):
            issues.append('xls_signature_mismatch')
        return 'xls', issues
    return 'csv', issues


def inspect_xlsx_archive(path: Path) -> tuple[dict[str, Any], list[str]]:
    """Ограничить распаковку XLSX/XLSM до передачи файла openpyxl."""
    issues: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            total_uncompressed = sum(item.file_size for item in members)
            largest = max((item.file_size for item in members), default=0)
            max_ratio = max(
                (item.file_size / max(item.compress_size, 1) for item in members),
                default=0.0,
            )
            unsafe_names = [
                item.filename for item in members
                if item.filename.startswith(('/', '\\')) or '..' in Path(item.filename).parts
            ]
            member_names = [item.filename.casefold() for item in members]
            macro_parts = [
                name for name in member_names
                if name.endswith('/vbaproject.bin') or name.endswith('vbaproject.bin')
                or name.endswith('/vbadata.xml')
            ]
    except (OSError, zipfile.BadZipFile):
        return {}, ['xlsx_archive_unreadable']
    if len(members) > MAX_ZIP_ENTRIES:
        issues.append('xlsx_archive_too_many_entries')
    if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
        issues.append('xlsx_archive_uncompressed_too_large')
    if largest > MAX_ZIP_ENTRY_BYTES:
        issues.append('xlsx_archive_entry_too_large')
    if max_ratio > MAX_ZIP_COMPRESSION_RATIO:
        issues.append('xlsx_archive_suspicious_compression_ratio')
    if unsafe_names:
        issues.append('xlsx_archive_unsafe_member_path')
    if macro_parts:
        issues.extend(['xlsx_macro_parts_present', 'macro_container_present'])
    return {
        'entries': len(members),
        'uncompressed_bytes': total_uncompressed,
        'largest_entry_bytes': largest,
        'max_compression_ratio': round(max_ratio, 3),
        'unsafe_member_paths': len(unsafe_names),
        'macro_parts': len(macro_parts),
    }, issues
