"""Standalone pure extraction from the existing importer."""
from __future__ import annotations
import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

RULES_VERSION = "price-multiplicity-v1"


MONEY = Decimal("0.01")


NORMALIZATION_COLUMNS = (
    "source_price_with_vat",
    "source_price_basis",
    "price_basis_quantity",
    "normalized_price_with_vat",
    "normalized_price_unit",
    "price_normalization_status",
    "price_normalization_reason",
    "price_normalization_rules_version",
)


def _norm(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value)).replace("\u00a0", " ").casefold()
    return re.sub(r"\s+", " ", text).strip().replace("ё", "е")


def _money(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = decimal_value(value)
        if result is None:
            return None
        result = result.quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    return result if result > 0 else None


def _basis_from_text(value: Any) -> str | None:
    text = _norm(value)
    if not text:
        return None
    if re.search(r"(?:^|\W)(?:кор|короб[а-я]*|ящик[а-я]*)(?:\W|$)", text):
        return "CASE"
    if re.search(r"(?:^|\W)(?:упак|упаков[а-я]*|пачк[а-я]*|пакет[а-я]*)(?:\W|$)", text):
        return "PACKAGE"
    if re.search(r"(?:^|\W)(?:шт|штук|единиц)(?:\W|$)", text):
        return "ITEM"
    if re.search(r"(?:^|\W)(?:кг|килограмм)(?:\W|$)", text):
        return "KILOGRAM"
    if re.search(r"(?:^|\W)(?:л|литр)(?:\W|$)", text):
        return "LITER"
    return None


def _count_unit(header: Any) -> str | None:
    text = _norm(header)
    if not text:
        return None
    if re.search(r"(?:шт|штук|единиц)", text):
        return "ITEM"
    if "упак" in text or "пач" in text:
        return "PACKAGE"
    return None


def _gross_source_price(item: dict[str, Any], source_field: str | None, source_value: Any) -> Decimal | None:
    source = _money(source_value)
    if source is None:
        return _money(item.get("price")) if source_value is None else None
    if source_field and source_field.endswith("without_vat"):
        rate = item.get("vat_rate")
        try:
            if rate is None:
                return None
            rate = Decimal(str(rate))
            if rate > 1:
                rate /= 100
            if rate not in {Decimal('0'), Decimal('.1'), Decimal('.2'), Decimal('.22')}:
                return None
            source = (source * (Decimal("1") + rate)).quantize(MONEY, rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError):
            return None
    return source


def count_matches_price_basis(basis: str, header: Any) -> bool:
    """Transport-case contents do not prove the contents of a paid package."""
    return _count_unit(header) == 'ITEM' and _basis_from_text(header) == basis


def normalize_imported_offer(
    item: dict[str, Any], *, source_field: str | None, source_value: Any,
    price_header: Any = None, count_header: Any = None,
) -> dict[str, Any]:
    """Вернуть сохраняемые поля нормализации для одной строки нового прайса."""
    source_price = _gross_source_price(item, source_field, source_value)
    basis = "CASE" if source_field and source_field.startswith("box_price_") else _basis_from_text(price_header)
    if basis is None:
        basis = _basis_from_text(item.get("unit_of_measure"))
    basis = basis or "UNKNOWN"
    unit_basis = _basis_from_text(item.get('unit_of_measure'))
    header_basis = _basis_from_text(price_header)
    # A per-case money column legitimately coexists with the contents' unit.
    basis_conflict = bool(header_basis and unit_basis and header_basis != unit_basis
                          and header_basis not in {'CASE', 'PACKAGE'})
    quantity: Decimal | int | None = None
    normalized: Decimal | None = None
    normalized_unit: str | None = None
    status = "BLOCKED"
    reason = "Не подтверждено, за что поставщик указал цену"

    if basis_conflict:
        status = 'REVIEW'
    elif basis in {"ITEM", "KILOGRAM", "LITER"} and source_price is not None:
        match = re.search(r'\b(?:за|per)\s+([-+]?\d+(?:[.,]\d+)?)\s*(?:шт|штук|единиц|кг|килограмм|л|литр)\b', _norm(price_header))
        quantity = decimal_value(match[1]) if match else Decimal(1)
        normalized = (source_price / quantity).quantize(MONEY, rounding=ROUND_HALF_UP) if quantity else None
        normalized_unit = basis
        status = "READY"
        reason = "Цена уже указана за одну сопоставимую единицу"
        if normalized is None:
            status, normalized_unit = 'REVIEW', None
    elif basis in {"PACKAGE", "CASE"}:
        raw_count = item.get("packs_per_box")
        count = raw_count if isinstance(raw_count, int) and raw_count > 0 else None
        if source_price is None:
            reason = "Не подтверждена исходная цена с НДС"
        elif count is None:
            reason = "Не подтверждено количество товара в упаковке или коробе"
        elif not count_matches_price_basis(basis, count_header):
            reason = "Кратность не подтверждает количество штук в базе цены"
        else:
            quantity = count
            normalized = (source_price / Decimal(count)).quantize(MONEY, rounding=ROUND_HALF_UP)
            normalized_unit = "ITEM"
            status = "READY"
            reason = "Цена разделена на подтверждённое количество штук"

    return {
        "source_price_with_vat": float(source_price) if source_price is not None else None,
        "source_price_basis": basis,
        "price_basis_quantity": float(quantity) if quantity is not None else None,
        "normalized_price_with_vat": float(normalized) if normalized is not None else None,
        "normalized_price_unit": normalized_unit,
        "price_normalization_status": status,
        "price_normalization_reason": None,
        "price_normalization_rules_version": RULES_VERSION,
    }


def decimal_value(value: Any, *, allow_zero: bool = False) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace('\u00a0', ' ')
    text = re.sub(r'(?i)\s*(?:₽|руб\.?|р\.)\s*$', '', text)
    if not re.fullmatch(r'[+]?(?:\d{1,3}(?: \d{3})+|\d+)(?:[.,]\d+)?', text):
        return None
    try:
        number = Decimal(text.replace(' ', '').replace(',', '.'))
    except InvalidOperation:
        return None
    return number if number.is_finite() and (number >= 0 if allow_zero else number > 0) else None
