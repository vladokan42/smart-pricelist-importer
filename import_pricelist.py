#!/usr/bin/env python3
"""Local workbook -> source-backed enrichment -> a new, atomic JSON export."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

import llm_price_enricher as llm
import pricelist_source_contract as contract
import update_price_registry as registry


def read_reference(path: Path) -> dict[str, str]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate reference key')
            result[key] = value
        return result
    reference = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)
    if isinstance(reference, dict) and set(reference) == {'_notice', 'reference'}:
        reference = reference['reference']
    return llm.validate_reference(reference)


def atomic_json(path: Path, payload: dict) -> None:
    """Publish complete bytes without ever replacing an existing directory entry."""
    data = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '.', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # link is atomic and fails if another writer wins, unlike os.replace.
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    try:
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        # Commit already succeeded: report durability uncertainty, never a false failure.
        print('Warning: JSON published, directory durability could not be confirmed.', file=sys.stderr)


def run_import(input_path: Path, supplier: str, reference_path: Path, output_path: Path) -> dict:
    source = Path(input_path).absolute()
    reference_path = Path(reference_path).absolute()
    output = Path(output_path).absolute()
    if os.path.lexists(output):
        raise FileExistsError('output already exists')
    if any(p.is_symlink() for p in (output, *output.parents)) or '..' in output.parts:
        raise ValueError('output traversal/symlink forbidden')
    if not output.parent.is_dir():
        raise ValueError('output directory must exist')
    if not supplier.strip():
        raise ValueError('supplier must not be empty')
    if any(p.is_symlink() for p in (source, *source.parents)) or '..' in source.parts:
        raise ValueError('input traversal/symlink forbidden')
    if not source.is_file() or source.stat().st_nlink != 1:
        raise ValueError('regular non-hardlinked input required')
    if source.suffix.casefold() not in {'.xls', '.xlsx', '.csv', '.tsv'}:
        raise ValueError('supported inputs: .xlsx/.xls/.csv/.tsv')
    if not 0 < source.stat().st_size <= contract.MAX_BYTES:
        raise ValueError('source size outside limit')
    reference = read_reference(reference_path)
    llm.get_settings()  # fail before parsing or creating any output
    original_hash = registry.file_sha256(source)
    reference_hash = registry.file_sha256(reference_path)
    loaded = datetime.now(timezone.utc).date().isoformat()
    quarantine = []
    items, report = registry.parse_semantic_pricelist(source, supplier, loaded, quarantine)
    for start in range(0, len(items), registry.SEMANTIC_BATCH_SIZE):
        llm.enrich_items_batch(items[start:start + registry.SEMANTIC_BATCH_SIZE], okpd_ref=reference)
    for item in items:
        registry.calculate_semantic_prices(item)
    if registry.file_sha256(source) != original_hash or registry.file_sha256(reference_path) != reference_hash:
        raise ValueError('input or reference changed during import')
    report.update(rows=len(items), llm_requested=len(items), batch_size=registry.SEMANTIC_BATCH_SIZE,
                  ready=sum(x['price_normalization_status'] == 'READY' for x in items),
                  review=sum(bool(x['_review_reasons']) for x in items),
                  missing_vat=sum(x.get('vat_rate') is None for x in items),
                  classified=sum(x.get('okpd2_code') is not None for x in items),
                  quarantine=registry.semantic_source_value(quarantine))
    payload = {'items': items, 'report': report,
               'evidence': [{'article': x['article'], 'sheet': x['_source']['sheet'], 'row': x['_source']['row'],
                             'extracted': x['_llm_evidence'], 'review_reasons': x['_review_reasons']} for x in items],
               'source': {'filename': source.name, 'sha256': original_hash, 'size_bytes': source.stat().st_size,
                          'format': source.suffix.casefold()[1:], 'supplier': supplier, 'loaded_date': loaded,
                          'reference_sha256': reference_hash, 'reference_codes': len(reference),
                          'grain': 'one source product row; no identity merges or database publication'}}
    atomic_json(output, payload)
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--supplier', required=True)
    parser.add_argument('--reference', type=Path, required=True, help='JSON object: exact OKPD2 code -> name')
    parser.add_argument('--out', type=Path, required=True, help='new JSON file; existing paths are never replaced')
    args = parser.parse_args(argv)
    try:
        result = run_import(args.input, args.supplier, args.reference, args.out)
    except Exception as error:
        # Provider/HTTP exceptions can include source data or authentication URLs.
        print('Import failed (' + type(error).__name__ + '); no partial JSON was published.', file=sys.stderr)
        return 2
    print(json.dumps({'output': str(args.out.absolute()), 'items': len(result['items'])}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
