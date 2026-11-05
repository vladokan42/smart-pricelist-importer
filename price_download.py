"""Standalone pure extraction from the existing importer."""
from __future__ import annotations
import math
from datetime import date as calendar_date, datetime, time
from pathlib import Path
import openpyxl
import xlrd
from openpyxl.utils import get_column_letter
import pricelist_source_contract as source_contract

MAX_CELLS = 1_000_000


EXTRA_HEADERS = {
    'fat_pct': {'жирность', 'жирность %', 'fat %'},
    'unit_volume_ml': {'объем мл', 'объём мл', 'объем, мл'},
    'units_per_box': {'количество штук', 'количество в коробе'},
    'consumer_packaging': {'потребительская упаковка'},
    'transport_packaging': {'транспортная упаковка'},
}


def _typed(value):
    if isinstance(value, (datetime, calendar_date, time)):
        return {'type': type(value).__name__, 'value': value.isoformat()}
    if isinstance(value, float) and not math.isfinite(value):
        return {'type': 'nonfinite', 'value': str(value)}
    return {'type': type(value).__name__, 'value': value}


def _read(source):
    p = Path(source).absolute()
    if '..' in p.parts or any(x.is_symlink() for x in (p, *p.parents)):
        raise ValueError('traversal/symlink forbidden')
    p = p.resolve(strict=True)
    if not p.is_file() or p.stat().st_nlink != 1:
        raise ValueError('regular non-hardlinked file required')
    if p.suffix.casefold() not in {'.xlsx', '.xls'}:
        raise ValueError('only .xlsx/.xls supported')
    if not 0 < p.stat().st_size <= source_contract.MAX_BYTES:
        raise ValueError('source size outside limit')
    fmt, issues = source_contract.detect_format(p)
    if issues:
        raise ValueError(','.join(issues))
    sheets = []
    if fmt == 'xlsx':
        archive, issues = source_contract.inspect_xlsx_archive(p)
        if issues:
            raise ValueError(','.join(issues))
        a = openpyxl.load_workbook(p, read_only=False, data_only=False, keep_links=True)
        b = None
        try:
            if sum(s.max_row * s.max_column for s in a) > MAX_CELLS:
                raise ValueError('workbook cell limit exceeded')
            b = openpyxl.load_workbook(p, read_only=True, data_only=True, keep_links=False)
            for index, s in enumerate(a):
                rows, cells = [], []
                for source_row, cached_row in zip(s.iter_rows(), b[s.title].iter_rows(), strict=True):
                    values, typed = [], []
                    for c, cached in zip(source_row, cached_row, strict=True):
                        value = cached.value if c.data_type == 'f' else c.value
                        values.append(value)
                        typed.append({'cell': c.coordinate if hasattr(c, 'coordinate') else get_column_letter(len(typed)+1)+str(len(rows)+1),
                                      'excel_type': c.data_type, 'original': _typed(c.value),
                                      'cached': _typed(cached.value) if c.data_type == 'f' else None,
                                      'number_format': c.number_format})
                    rows.append(values)
                    cells.append(typed)
                sheets.append({'index': index, 'name': s.title, 'state': s.sheet_state,
                               'rows': rows, 'cells': cells, 'merged_ranges': [str(x) for x in s.merged_cells.ranges]})
            meta = {'formula_detection': 'available', 'formula_policy': 'mapped formulas quarantined; cache not trusted; no execution',
                    'external_links': len(getattr(a, '_external_links', [])), 'archive': archive}
        finally:
            a.close()
            if b:
                b.close()
    else:
        book = xlrd.open_workbook(str(p), on_demand=True, formatting_info=True)
        try:
            if sum(book.sheet_by_index(i).nrows * book.sheet_by_index(i).ncols for i in range(book.nsheets)) > MAX_CELLS:
                raise ValueError('workbook cell limit exceeded')
            for i in range(book.nsheets):
                s = book.sheet_by_index(i)
                cells = [[{'cell': get_column_letter(c+1)+str(r+1),
                           'excel_type': xlrd.XL_CELL_EMPTY if s.cell_type(r,c) == xlrd.XL_CELL_BLANK else s.cell_type(r,c),
                           'original': _typed(s.cell_value(r,c)), 'cached': None}
                          for c in range(s.ncols)] for r in range(s.nrows)]
                sheets.append({'index': i, 'name': s.name, 'state': 'visible' if s.visibility == 0 else 'hidden',
                               'rows': [s.row_values(r) for r in range(s.nrows)], 'cells': cells,
                               'bold_rows': [r + 1 for r in range(s.nrows) if
                                   any(str(s.cell_value(r, c)).strip() for c in range(s.ncols)) and
                                   all(book.font_list[book.xf_list[s.cell_xf_index(r, c)].font_index].bold
                                       for c in range(s.ncols) if str(s.cell_value(r, c)).strip())],
                               'datemode': book.datemode})
            meta = {'formula_detection': 'unavailable_for_xls', 'external_links': None,
                    'formula_policy': 'xlrd returns possibly stale cached results; formula text/presence unavailable; original XLS retained; no execution'}
        finally:
            book.release_resources()
    return p, fmt, sheets, meta
