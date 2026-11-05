"""Standalone pure extraction from the existing importer."""
from __future__ import annotations
import csv
import hashlib
import json
import math
import re
import unicodedata
from datetime import date, datetime, time as datetime_time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from price_multiplicity import NORMALIZATION_COLUMNS, count_matches_price_basis, normalize_imported_offer

TARGET_COLUMNS = (
    "supplier", "name", "price", "article", "date",
    "supplier_article", "product_code", "barcode", "full_name",
    "country_of_origin", "net_weight_kg", "unit_of_measure",
    "packaging_type", "packs_per_box", "unit_price_without_vat",
    "unit_price_with_vat", "box_price_without_vat", "box_price_with_vat",
    "vat_rate", "storage_temperature", "loaded_at", "source_file_name",
) + NORMALIZATION_COLUMNS + ("okpd2_code", "okpd2_name", "price_equivalents_json")


MAX_AUTO_PRICE = 999_999.99


BLOCKED_EXACT_PRICE = 1.0


MONEY = Decimal("0.01")


HEADER_ALIASES = {
    "supplier_article": ("артикул поставщика", "артикул", "арт", "товар №", "товар no", "article", "sku", "item no"),
    "product_code": ("код товара", "код 1с", "код продукции", "product code"),
    "barcode": ("штрихкод", "штрих код", "barcode", "ean", "gtin"),
    "full_name": ("полное наименование", "наименование товара", "наименование", "название", "номенклатура", "товар", "product name"),
    "country_of_origin": ("страна происхождения", "страна", "country"),
    "net_weight_kg": ("масса нетто кг", "вес нетто кг", "масса нетто", "вес нетто", "net weight"),
    "unit_of_measure": ("единица измерения", "ед изм", "ед", "единица", "unit"),
    "packaging_type": ("вид упаковки", "тип упаковки", "упаковка", "packaging"),
    "packs_per_box": ("упаковок в коробе", "шт в коробе", "штук в коробе", "кратность", "qty per box"),
    "unit_price_without_vat": ("цена за штуку без ндс", "цена за шт без ндс", "цена без ндс за штуку", "price per piece without vat"),
    "unit_price_with_vat": ("цена за штуку с ндс", "цена за шт с ндс", "цена с ндс за штуку", "price per piece with vat"),
    "box_price_without_vat": ("цена за короб без ндс", "цена короба без ндс", "price per box without vat"),
    "box_price_with_vat": ("цена за короб с ндс", "цена короба с ндс", "price per box with vat"),
    "vat_rate": ("ставка ндс", "ндс", "vat rate", "vat"),
    "storage_temperature": ("температура хранения", "режим хранения", "storage temperature"),
    "source_price_with_vat": ("исходная цена с ндс",),
    "source_price_basis": ("база исходной цены",),
    "price_basis_quantity": ("количество в базе цены",),
    "normalized_price_with_vat": ("нормализованная цена с ндс",),
    "normalized_price_unit": ("единица нормализованной цены",),
    "price_normalization_status": ("статус нормализации цены",),
    "price_normalization_reason": ("причина статуса нормализации",),
    "price_normalization_rules_version": ("версия правил нормализации",),
}


SOURCE_PRICE_FIELDS = frozenset({
    "unit_price_without_vat", "unit_price_with_vat",
    "box_price_without_vat", "box_price_with_vat",
})


def normalize_text(value: Any, fold_yo: bool = False) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value)).replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text.replace("ё", "е") if fold_yo else text


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value)).replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def parse_number(value: Any, integer: bool = False) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return int(number) if integer and number.is_integer() else number
    text = clean_text(value)
    if not text:
        return None
    text = re.sub(r"[₽%]", "", text).replace(" ", "").replace(",", ".")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group())
    if not math.isfinite(number):
        return None
    if integer:
        return int(number) if number.is_integer() else None
    return number


STRICT_NUMBER_RE = re.compile(r"[-+]?(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d+)?")


def parse_strict_number(value: Any, integer: bool = False) -> float | int | None:
    """Разобрать всё значение целиком, не извлекая случайное число из текста."""
    if value is None or isinstance(value, bool):
        return None
    if integer:
        text = clean_text(value)
        if not text or not STRICT_NUMBER_RE.fullmatch(text):
            return None
        number = Decimal(text.replace(' ', '').replace(',', '.'))
        return int(number) if number.is_finite() and number == number.to_integral_value() else None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = clean_text(value)
        if not text:
            return None
        text = re.sub(r"(?i)\s*(?:₽|руб\.?|р\.)\s*$", "", text).strip()
        if not STRICT_NUMBER_RE.fullmatch(text):
            return None
        number = float(text.replace("\u00a0", "").replace(" ", "").replace(",", "."))
    if not math.isfinite(number):
        return None
    if integer:
        return int(number) if number.is_integer() else None
    return number


def normalize_article(value: Any) -> str | None:
    text = clean_text(value)
    if not text or normalize_text(text) in {"-", "—", "новый", "новинка", "new"}:
        return None
    if not isinstance(value, str) and re.fullmatch(r"\d+\.0", text):
        return text[:-2]
    return text


def validate_price(value: Any) -> float:
    price = parse_strict_number(value)
    if price is None:
        raise ValueError("Пустая или нечисловая цена")
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"Цена должна быть больше нуля: {price}")
    if price == BLOCKED_EXACT_PRICE:
        raise ValueError("Цена ровно 1 ₽ запрещена для загрузки в реестр")
    if price > MAX_AUTO_PRICE:
        raise ValueError(
            f"Цена {price:,.2f} превышает порог {MAX_AUTO_PRICE:,.2f}. "
            "Проверьте, что выбрана ценовая колонка, а не артикул/штрихкод."
        )
    return price


def normalize_vat_rate(value: Any) -> float | None:
    """Normalize supplier VAT notation to a fraction, e.g. 20% and 0.2 -> 0.2."""
    parsed = parse_number(value)
    if parsed is None:
        return None
    rate = float(parsed)
    if rate > 1:
        rate /= 100
    if rate not in {0.0, 0.1, 0.2, 0.22}:
        return None
    return rate


def gross_from_net(net: Any, vat_rate: Any) -> float:
    rate = normalize_vat_rate(vat_rate)
    if rate is None:
        raise ValueError("Цена без НДС требует валидную ставку НДС (10%, 20% или 22%)")
    try:
        gross = (Decimal(str(net)) * (Decimal("1") + Decimal(str(rate)))).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Некорректная цена без НДС") from error
    return validate_price(float(gross))


TRUSTED_NORMALIZATION_FIELDS = frozenset(NORMALIZATION_COLUMNS)


def format_wholesale_header_map(row: list[Any]) -> dict[str, int] | None:
    """Recognize the one verified four-column Format pricelist layout."""
    labels = []
    for value in row[:4]:
        label = normalize_text(value, fold_yo=True)
        label = re.sub(r"[._/\\-]+", " ", label)
        labels.append(re.sub(r"\s+", " ", label).strip())
    if labels == ["вид товара номенклатура", "ед изм", "ост", "крупный опт"]:
        if any(clean_text(value) is not None for value in row[4:]):
            raise ValueError("ФОРМАТ: дополнительные колонки требуют проверки условий цены")
        # The layout has one commercial column. With no «без НДС» marker, the
        # Data Agent source policy treats it as VAT-inclusive.
        return {"full_name": 0, "unit_of_measure": 1, "unit_price_with_vat": 3}
    return None


def header_field(value: Any, *, trusted_preprocessing: bool = False) -> str | None:
    header = normalize_text(value, fold_yo=True)
    if not header:
        return None
    header = re.sub(r"[._/\\-]+", " ", header)
    header = re.sub(r"\s+", " ", header).strip()
    for field, aliases in HEADER_ALIASES.items():
        if field in TRUSTED_NORMALIZATION_FIELDS and not trusted_preprocessing:
            if header in aliases:
                return None
            continue
        if header in aliases:
            return field
    if "артикул" in header or header == "код":
        return "supplier_article"
    if "штрих код" in header or header in {"ean", "gtin", "barcode"}:
        return "barcode"
    if "товар под заказ" in header:
        return None
    if "наимен" in header or "назван" in header or "номенклатур" in header:
        return "full_name"
    if "страна" in header:
        return "country_of_origin"
    if re.search(r'\bставк\w*\b', header) and "ндс" in header:
        return "vat_rate"
    if "ндс" in header and ("%" in header or header == "ндс") and not re.search(r'цен|прайс|стоим', header):
        return "vat_rate"
    if ("вес" in header or "масса" in header) and ("ед" in header or "нетто" in header):
        return "net_weight_kg"
    if "единиц" in header and "измер" in header:
        return "unit_of_measure"
    if "кол во" in header and "упаков" in header:
        return "packs_per_box"
    if ("шт" in header or "упаков" in header) and "кор" in header:
        return "packs_per_box"
    if "короб" in header and "без ндс" in header:
        return "box_price_without_vat"
    if "короб" in header and "с ндс" in header:
        return "box_price_with_vat"
    if ("прайс" in header or "цена" in header) and "без ндс" in header:
        return "unit_price_without_vat"
    if ("прайс" in header or "цена" in header) and "с ндс" in header:
        return "unit_price_with_vat"
    # Supplier lists with a plain «Цена» column have no explicit tax marker;
    # the source contract treats those prices as VAT-inclusive by default.
    if "прайс" in header or "цен" in header:
        return "unit_price_with_vat"
    return None


def header_mapping(labels: list[Any], *, trusted_preprocessing: bool = False) -> dict[str, int]:
    mapping, duplicates = {}, []
    for col, label in enumerate(labels):
        field = header_field(label, trusted_preprocessing=trusted_preprocessing)
        if field in SOURCE_PRICE_FIELDS and field in mapping:
            duplicates.append((mapping[field], col))
        if field:
            mapping.setdefault(field, col)
    prices = [labels[col] for field, col in mapping.items() if field in SOURCE_PRICE_FIELDS]
    # Compare commercial qualifiers across ALL money columns, not just gross/gross.
    # Remove only tax/unit vocabulary; unknown qualifiers still need an explicit choice.
    def condition(label):
        text = normalize_text(label)
        text = re.sub(r'\b(?:без|с|включая|включает|вкл)\s*ндс\b', ' ', text)
        text = re.sub(r'\b(?:за|per)?\s*\d+(?:[.,]\d+)?\s*(?:шт\w*|штук\w*|единиц\w*|кг|килограмм\w*|л|литр\w*|упак\w*|короб\w*|кор|ящ\w*)\b', ' ', text)
        text = re.sub(r'\b(?:цен\w*|прайс\w*|стоимост\w*|руб\w*|ндс|шт\w*|штук\w*|ед\w*|упак\w*|короб\w*|кор|ящ\w*|кг|килограмм\w*|л|литр\w*|за|на|в|с|без|price|net|gross|vat|rub|unit|piece|item|box|case|package|pack|kg|liter|per|with|without)\b', ' ', text)
        return ' '.join(re.findall(r'\w+', text))
    conditions = {condition(label) for label in prices}
    if 'full_name' in mapping and (duplicates or len(conditions) > 1):
        raise ValueError('ambiguous_price_columns: REVIEW: требуется явный выбор условий цены; колонки ' + repr(prices))
    return mapping


def detect_header(
    rows: list[list[Any]], *, trusted_preprocessing: bool = False,
) -> tuple[int, dict[str, int]]:
    best: tuple[int, dict[str, int]] = (-1, {})
    # Prefer a real one-row header before trying merged/company header bands.
    # This prevents words such as «прайс» in the supplier title from being
    # assigned to the article column of a later header row.
    for row_index, row in enumerate(rows[:50]):
        format_layout = format_wholesale_header_map(row)
        if format_layout is not None:
            return row_index, format_layout
        mapping = header_mapping(row, trusted_preprocessing=trusted_preprocessing)
        if "full_name" in mapping and any(k.endswith("_vat") for k in mapping):
            return row_index, mapping
    # Besides regular one-row headers, supplier workbooks often use vertically
    # merged, multi-row headers: e.g. «руб. / Включает НДС / Цена». Combine a
    # short header band column-wise, but retain its final row as data start.
    for end_index in range(min(len(rows), 30)):
        for start_index in range(max(0, end_index - 3), end_index + 1):
            labels = []
            max_columns = max((len(row) for row in rows[start_index:end_index + 1]), default=0)
            for col in range(max_columns):
                parts = [
                    cleaned
                    for row in rows[start_index:end_index + 1]
                    if col < len(row)
                    for cleaned in [clean_text(row[col])]
                    if cleaned is not None
                ]
                labels.append(" ".join(parts))
            mapping = header_mapping(labels, trusted_preprocessing=trusted_preprocessing)
            if "full_name" in mapping and any(k.endswith("_vat") for k in mapping):
                return end_index, mapping
            if len(mapping) > len(best[1]):
                best = (end_index, mapping)
    raise ValueError("Не найдена строка заголовков с наименованием и ценой с указанной базой/НДС")


def csv_rows(path: Path) -> list[list[Any]]:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            with path.open("r", encoding=encoding, newline="") as source:
                sample = source.read(4096)
                source.seek(0)
                dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
                return list(csv.reader(source, dialect, doublequote=True, strict=True))
        except UnicodeDecodeError:
            continue
    raise ValueError("Не удалось определить кодировку CSV")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_header_text(rows: list[list[Any]], header_row: int, column: int) -> str:
    """Собрать текст короткой многострочной шапки одной исходной колонки."""
    parts = [
        value
        for row_index in range(max(0, header_row - 3), header_row + 1)
        if column < len(rows[row_index])
        for value in [clean_text(rows[row_index][column])]
        if value is not None
    ]
    return " ".join(parts)


SEMANTIC_VERSION = 'semantic-pricelist-v2'


SEMANTIC_BATCH_SIZE = 10


def semantic_source_value(value):
    if isinstance(value, dict):
        return {k: semantic_source_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [semantic_source_value(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return {'type': 'nonfinite', 'value': str(value)}
    return value.isoformat() if isinstance(value, (date, datetime, datetime_time)) else value


def semantic_source_sheets(path: Path):
    """Reuse the source-cell reader, including formulas and every sheet."""
    if path.suffix.casefold() in {'.xls', '.xlsx'}:
        from price_download import _read
        _path, _fmt, sheets, meta = _read(path)
        if meta.get('external_links'):
            raise ValueError('external workbook links forbidden')
        return sheets, meta
    if path.suffix.casefold() not in {'.csv', '.tsv'}:
        raise ValueError('semantic source must be xls/xlsx/csv/tsv')
    rows = csv_rows(path)
    return [{'index': 0, 'name': 'CSV', 'state': 'visible', 'rows': rows,
             'cells': [[{'column': i, 'original': {'type': type(v).__name__, 'value': v}}
                        for i, v in enumerate(row)] for row in rows]}], {'formula_detection': 'not_applicable'}


def semantic_header(rows: list[list[Any]]) -> tuple[int, dict[str, int]]:
    try:
        return detect_header(rows)
    except ValueError as error:
        if str(error).startswith('ambiguous_price_columns:'):
            raise
        # A name-only header is enough for a partial product record, not a
        # fallback catalogue parser. Never infer a data column by its position.
        for index, row in enumerate(rows[:50]):
            mapping = header_mapping(row)
            if 'full_name' in mapping:
                return index, mapping
        raise ValueError('no deterministic product-name header')


def semantic_warning(item: dict[str, Any], field: str, reason: str) -> None:
    item.setdefault('_review_reasons', []).append(field + ': ' + reason)
    item.setdefault('_invalid_fields', []).append(field)
    item[field] = None


def parse_semantic_pricelist(path: Path, supplier: str, loaded_date: str,
                             quarantine: list[dict[str, Any]] | None = None):
    import enrich_price_registry_details as details
    from price_download import EXTRA_HEADERS
    sheets, meta = semantic_source_sheets(path)
    sha = file_sha256(path)
    items, report = [], {'accepted': 0, 'skipped_missing_name': 0, 'skipped_categories': 0, 'rejected': 0}
    for sheet in sheets:
        rows = sheet['rows']
        if not any(any(clean_text(v) for v in row) for row in rows):
            continue
        try:
            header, mapping = semantic_header(rows)
        except ValueError as error:
            if str(error).startswith('ambiguous_price_columns:'):
                raise
            if quarantine is not None:
                quarantine.append({'sheet': sheet['name'], 'reason': str(error), 'rows': len(rows)})
            continue
        # Detail aliases precede broad registry aliases (notably manufacturer
        # article, which must never become a supplier article).
        direct = {}
        for col, value in enumerate(rows[header]):
            label = details.key(value)
            for field, aliases in {**details.ALIASES, **EXTRA_HEADERS}.items():
                if label in aliases:
                    direct[field] = col
                    if field == 'manufacturer_article':
                        mapping = {f: c for f, c in mapping.items() if c != col}
            if label in {'id', 'ид', 'price_registry_id', 'ид реестра'}:
                direct['_existing_id'] = col
            if label in {'тип строки', 'row type'}:
                direct['_row_type'] = col
        manufacturer_columns = {col for col, value in enumerate(rows[header])
                                if details.key(value) in details.ALIASES['manufacturer_article']}
        for col, value in enumerate(rows[header]):
            field = header_field(value)
            if field and col not in manufacturer_columns:
                mapping.setdefault(field, col)
        headers = [source_header_text(rows, header, c) for c in range(max(map(len, rows), default=0))]
        merged_titles = set()
        if sheet.get('merged_ranges'):
            from openpyxl.worksheet.cell_range import CellRange
            merged_titles = {CellRange(r).min_row for r in sheet['merged_ranges']
                             if CellRange(r).max_col > CellRange(r).min_col}
        context = rows[:header + 1] + [r for i, r in enumerate(rows[header + 1:], header + 2)
            if any(clean_text(v) for v in r) and (i in merged_titles or
                mapping['full_name'] >= len(r) or clean_text(r[mapping['full_name']]) is None)]
        for row_index in range(header + 1, len(rows)):
            row = rows[row_index]
            value = lambda col: row[col] if col < len(row) else None
            name = clean_text(value(mapping['full_name']))
            if not name:
                report['skipped_missing_name'] += 1
                continue
            row_type = normalize_text(value(direct['_row_type'])) if '_row_type' in direct else ''
            category = row_type in {'category', 'категория', 'раздел', 'группа', 'итого'} or normalize_text(name) in {'итого', 'всего'}
            category = category or bool(re.match(r'^(?:категория|раздел|группа|итого|всего)\s*[:：]', name, re.I))
            # A merged title spanning columns, with no commercial/identity cells,
            # is an explicit worksheet section label, not a one-column product.
            if row_index + 1 in merged_titles and sum(clean_text(v) is not None for v in row) == 1:
                category = True
            # Explicit Format layout + bold one-cell label; missing price alone
            # never excludes a product (unit/stock/other product cells survive).
            if (format_wholesale_header_map(rows[header]) and row_index + 1 in sheet.get('bold_rows', [])
                    and sum(clean_text(v) is not None for v in row) == 1):
                category = True
            if category or header_field(name) == 'full_name':
                report['skipped_categories'] += 1
                if quarantine is not None:
                    quarantine.append({'sheet': sheet['name'], 'row': row_index + 1,
                                       'reason': 'category_or_repeated_header', 'raw_row': row})
                continue
            cells = sheet['cells'][row_index]
            formulas = {i for i, cell in enumerate(cells) if cell.get('excel_type') == 'f'}
            if mapping['full_name'] in formulas:
                report['rejected'] += 1
                if quarantine is not None:
                    quarantine.append({'sheet': sheet['name'], 'row': row_index + 1, 'reason': 'formula_name', 'cells': cells})
                continue
            item: dict[str, Any] = dict.fromkeys(TARGET_COLUMNS)
            original_article = normalize_article(value(mapping['supplier_article'])) if 'supplier_article' in mapping else None
            item.update(supplier=supplier, name=name, full_name=name, supplier_article=original_article,
                        article=original_article or f'AUTO-{sha[:16]}-S{sheet["index"]}-R{row_index + 1}',
                        date=loaded_date, loaded_at=loaded_date, source_file_name=path.name,
                        _semantic=True, _details={}, _review_reasons=[], _llm_evidence=[], _invalid_fields=[], _invalid_details=[])
            item['_source'] = {'file': str(path.resolve()), 'filename': path.name, 'sha256': sha,
                'sheet': sheet['name'], 'sheet_index': sheet['index'], 'sheet_state': sheet.get('state'),
                'row': row_index + 1, 'headers': headers, 'raw_row': row, 'cells': cells,
                'context': context, 'workbook': meta}
            item['_source'] = semantic_source_value(item['_source'])
            item['_source_prices'] = {}
            for field, col in mapping.items():
                raw = value(col)
                if field in SOURCE_PRICE_FIELDS:
                    item['_source_prices'][field] = {'raw': semantic_source_value(raw), 'header': headers[col], 'column': col}
                if field not in TARGET_COLUMNS or field == 'full_name':
                    continue
                if col in formulas:
                    semantic_warning(item, field, 'formula cell not trusted; raw retained')
                    continue
                if field in SOURCE_PRICE_FIELDS:
                    if clean_text(raw) is not None:
                        try:
                            item[field] = validate_price(raw)
                        except ValueError as error:
                            semantic_warning(item, field, str(error))
                elif field in {'packs_per_box', 'net_weight_kg'}:
                    number = parse_strict_number(raw, integer=field == 'packs_per_box')
                    if number is not None and number > 0:
                        if field == 'net_weight_kg' and 'кг' not in normalize_text(headers[col]) and 'г' in normalize_text(headers[col]):
                            number = float(Decimal(str(number)) / 1000)
                        item[field] = number
                    elif clean_text(raw):
                        semantic_warning(item, field, 'invalid positive numeric value')
                elif field == 'vat_rate':
                    text = clean_text(raw)
                    numeric = parse_strict_number(text.rstrip('%').strip()) if text else None
                    item[field] = normalize_vat_rate(numeric)
                    if text and item[field] is None:
                        semantic_warning(item, field, 'invalid explicit VAT rate')
                elif field in {'supplier_article', 'barcode'}:
                    item[field] = normalize_article(raw)
                else:
                    item[field] = clean_text(raw)
            for field, col in direct.items():
                raw = value(col)
                if col in formulas or not clean_text(raw):
                    continue
                if field == '_existing_id':
                    identifier = parse_strict_number(raw, integer=True)
                    if identifier is None or identifier <= 0:
                        raise ValueError('invalid explicit registry ID')
                    item[field] = identifier
                elif field in details.TEXT_FIELDS:
                    item['_details'][field] = clean_text(raw)
                elif field in details.NUMBER_FIELDS:
                    number = parse_strict_number((clean_text(raw) or '').rstrip('%').strip() if field == 'fat_pct' else raw,
                                                 integer=field == 'units_per_box')
                    if number is not None and (0 <= number <= 100 if field == 'fat_pct' else number > 0):
                        item['_details'][field] = number
                    else:
                        item['_review_reasons'].append(field + ': invalid explicit detail value')
                        item['_invalid_details'].append(field)
                if field in item['_details']:
                    item['_llm_evidence'].append({'field': field, 'text': str(raw), 'kind': 'literal'})
            item['article'] = item['supplier_article'] or f'AUTO-{sha[:16]}-S{sheet["index"]}-R{row_index + 1}'
            item['_original_supplier_article'] = item['supplier_article']
            # Deterministic price headers seed semantics, but no money is sent
            # back from the model or accepted from a previous source.
            source_field = next((f for f in ('unit_price_with_vat', 'unit_price_without_vat',
                'box_price_with_vat', 'box_price_without_vat') if item.get(f) is not None), None)
            item['_source_price_field'] = source_field
            seed = normalize_imported_offer(item, source_field=source_field,
                source_value=item.get(source_field) if source_field else None,
                price_header=item['_source_prices'].get(source_field, {}).get('header'),
                count_header=headers[mapping['packs_per_box']] if 'packs_per_box' in mapping else None)
            if seed['source_price_basis'] == 'UNKNOWN' and source_field:
                from llm_price_enricher import BASIS_PATTERNS
                price_header = item['_source_prices'][source_field]['header']
                bases = [b for b, pattern in BASIS_PATTERNS.items() if re.search(pattern, price_header, re.I)]
                if len(bases) == 1:
                    seed['source_price_basis'] = bases[0]
            item['_price_basis'] = seed['source_price_basis']
            item['_price_basis_quantity'] = seed['price_basis_quantity']
            if item['_price_basis'] in {'ITEM', 'KILOGRAM', 'LITER'} and source_field:
                quantity = re.search(r'\b(?:за|per)\s+([-+]?\d+(?:[.,]\d+)?)\s*(?:шт|штук|единиц|кг|килограмм|л|литр)\b',
                                     item['_source_prices'][source_field]['header'], re.I)
                if quantity:
                    item['_price_basis_quantity'] = parse_strict_number(quantity[1])
            item['_count_header'] = headers[mapping['packs_per_box']] if 'packs_per_box' in mapping else None
            item['_basis_conflict'] = seed['price_normalization_status'] == 'REVIEW'
            item['_equivalent_context'] = {}
            items.append(item)
            report['accepted'] += 1
    if not items:
        raise ValueError('no identifiable product rows in semantic source')
    return items, report


def calculate_semantic_prices(item: dict[str, Any]) -> None:
    """All arithmetic uses source money and Decimal, never model money."""
    from price_multiplicity import decimal_value
    def source_decimal(field):
        raw = item['_source_prices'].get(field, {}).get('raw')
        return decimal_value(raw) if raw is not None else decimal_value(item.get(field))
    # Proven source headings were already filtered before extraction. A model
    # label is not new source evidence, including for products without prices.
    if item.get('_row_kind') == 'category':
        item['_row_kind'] = 'product'
        item.setdefault('_review_reasons', []).append('_row_kind: unproven model category; source product retained')
    for field in item.get('_invalid_details', []):
        item.setdefault('_details', {})[field] = None
    for field in SOURCE_PRICE_FIELDS:
        entry = item['_source_prices'].get(field)
        try:
            item[field] = validate_price(entry['raw']) if entry and clean_text(entry['raw']) is not None and field not in item['_invalid_fields'] else None
        except ValueError:
            item[field] = None
    item['supplier_article'] = item['_original_supplier_article']
    for field in item['_invalid_fields']:
        item[field] = None
    item['vat_rate'] = normalize_vat_rate(item.get('vat_rate'))
    rate = item['vat_rate']
    for prefix in ('unit', 'box'):
        net, gross = prefix + '_price_without_vat', prefix + '_price_with_vat'
        if item[net] is not None and rate is not None:
            try:
                expected = gross_from_net(source_decimal(net), rate)
            except ValueError as error:
                semantic_warning(item, gross, str(error))
                continue
            if gross in item['_invalid_fields']:
                continue
            if item[gross] is None:
                item[gross] = expected
            elif abs(Decimal(str(item[gross])) - Decimal(str(expected))) > MONEY:
                semantic_warning(item, net, 'net/gross/VAT conflict; source retained')
                semantic_warning(item, gross, 'net/gross/VAT conflict; source retained')
    packs = item.get('packs_per_box')
    if isinstance(packs, int) and packs > 0:
        for suffix in ('with_vat', 'without_vat'):
            unit, box = 'unit_price_' + suffix, 'box_price_' + suffix
            if item[unit] is not None and item[box] is not None:
                expected = (source_decimal(box) / Decimal(packs)).quantize(MONEY, rounding=ROUND_HALF_UP)
                if abs(Decimal(str(item[unit])) - expected) > MONEY:
                    semantic_warning(item, unit, 'unit/box/count conflict; raw retained')
                    semantic_warning(item, box, 'unit/box/count conflict; raw retained')
    source_field = item.get('_source_price_field')
    basis = item.get('_price_basis') or 'UNKNOWN'
    source = item.get(source_field) if source_field else None
    exact_source = source_decimal(source_field) if source_field and source is not None else None
    if source_field and source_field.endswith('without_vat'):
        source = item.get(source_field.replace('without_vat', 'with_vat')) if rate is not None else None
        exact_source = Decimal(str(source)) if source is not None else None
    item.update(source_price_with_vat=source, source_price_basis=basis,
        price=None, normalized_price_with_vat=None, normalized_price_unit=None,
        price_basis_quantity=None, price_normalization_status='BLOCKED',
        price_normalization_reason='Нет подтверждённой цены с НДС и сопоставимой базы',
        price_normalization_rules_version=SEMANTIC_VERSION)
    if basis == 'UNKNOWN':
        item['price'] = source
        # A generic price column is not proof of a unit price.
        if source_field and source_field.startswith('unit_'):
            item['unit_price_with_vat'] = None
        item['price_normalization_reason'] = 'Исходная цена сохранена; база цены неизвестна'
    elif basis in {'ITEM', 'KILOGRAM', 'LITER'}:
        quantity = item.get('_price_basis_quantity')
        quantity = quantity if quantity is not None else 1
        from price_multiplicity import decimal_value
        if decimal_value(quantity) is None:
            item['_basis_conflict'] = True
        if source is not None and decimal_value(quantity) is not None:
            try:
                unit = validate_price((exact_source / Decimal(str(quantity))).quantize(MONEY, rounding=ROUND_HALF_UP))
            except ValueError as error:
                semantic_warning(item, 'unit_price_with_vat', str(error))
            else:
                item.update(price=unit, unit_price_with_vat=unit, normalized_price_with_vat=unit, normalized_price_unit=basis,
                            price_basis_quantity=quantity, price_normalization_status='READY',
                            price_normalization_reason='Явная цена и количество в базе сопоставимой единицы')
                if item['unit_price_without_vat'] is not None and quantity != 1:
                    item['unit_price_without_vat'] = float((source_decimal('unit_price_without_vat') /
                        Decimal(str(quantity))).quantize(MONEY, rounding=ROUND_HALF_UP))
    elif basis in {'CASE', 'PACKAGE'}:
        if source_field and source_field.startswith('unit_'):
            if basis == 'CASE':
                item['box_price_with_vat'] = item['unit_price_with_vat']
                item['box_price_without_vat'] = item['unit_price_without_vat']
            item['unit_price_with_vat'] = item['unit_price_without_vat'] = None
        if basis == 'PACKAGE':
            item['price'] = source
        count = item.get('_price_basis_quantity')
        if count_matches_price_basis(basis, item.get('_count_header')):
            count = count if count is not None else item.get('packs_per_box')
        else:
            cited = ' '.join(e['text'] for e in item.get('_llm_evidence', [])
                             if e.get('field') == '_price_basis_quantity')
            if not count_matches_price_basis(basis, cited):
                count = None
        if source is not None and isinstance(count, (int, float)) and not isinstance(count, bool) and count > 0 and float(count).is_integer():
            item.setdefault('_equivalent_context', {})['contents_items'] = count
            try:
                unit = validate_price((exact_source / Decimal(str(count))).quantize(MONEY, rounding=ROUND_HALF_UP))
            except ValueError as error:
                semantic_warning(item, 'unit_price_with_vat', str(error))
            else:
                item.update(price=unit, unit_price_with_vat=unit, normalized_price_with_vat=unit,
                            normalized_price_unit='ITEM', price_basis_quantity=int(count),
                            price_normalization_status='READY', price_normalization_reason='Расчёт по явному количеству в базе цены')
        elif item['unit_price_with_vat'] is not None and source_field and source_field.startswith('box_'):
            # An independently explicit unit price may coexist with unknown box count.
            item['price'] = item['unit_price_with_vat']
        else:
            item.setdefault('_equivalent_context', {})['contents_items'] = None
            item['price_normalization_reason'] = 'Цена короба/упаковки сохранена; количество для пересчёта неизвестно'
    if item.get('main_product'):
        measurements = json.loads(item.setdefault('_details', {}).get('measurements_json') or '[]')
        if not any(isinstance(x, dict) and x.get('name') == 'main_product' for x in measurements):
            measurements.append({'name': 'main_product', 'value': item['main_product'], 'unit': None})
        item['_details']['measurements_json'] = json.dumps(measurements, ensure_ascii=False)
    if item.get('_basis_conflict'):
        item.update(price_normalization_status='REVIEW', normalized_price_with_vat=None, normalized_price_unit=None)
    item['price_normalization_reason'] = None
