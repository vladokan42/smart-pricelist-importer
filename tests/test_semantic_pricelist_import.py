"""Existing pure semantic regressions; no database or network."""
import csv
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import llm_price_enricher as llm
import update_price_registry as registry


class SemanticImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rows = {}
        self.addCleanup(patch.stopall)
        patch('socket.socket', side_effect=AssertionError('network forbidden')).start()
        patch.object(llm.urllib.request, 'urlopen', side_effect=AssertionError('network forbidden')).start()

    def source(self, headers, rows, filename='price.csv'):
        path = self.root / filename
        with path.open('w', encoding='utf-8', newline='') as out:
            writer = csv.writer(out, delimiter=';')
            writer.writerow(headers)
            writer.writerows(rows)
        return path

    def parse(self, source, supplier='Рабочий поставщик', loaded='2026-10-26'):
        return registry.parse_semantic_pricelist(source, supplier, loaded)[0]

    def model(self, batch, **kwargs):
        for entry in batch:
            entry.update(llm._extract(entry, {'idx': 0}, kwargs['okpd_ref']))
        return batch

    def import_rows(self, rows, model=None):
        (model or self.model)(rows, okpd_ref={'10.51.11': 'Official title'})
        for entry in rows:
            registry.calculate_semantic_prices(entry)
            self.rows[entry['article']] = entry

    def actual(self, article=None):
        return self.rows[article] if article else next(reversed(self.rows.values()))

    def test_partial_price_bases_and_decimal_calculations(self):
        from decimal import Decimal
        cases = [
            ('Цена за короб с НДС', 120, None, dict(price=None, unit_price_with_vat=None, box_price_with_vat=120, normalized_price_with_vat=None)),
            ('Цена за шт без НДС', 100, None, dict(price=None, unit_price_without_vat=100, unit_price_with_vat=None, source_price_with_vat=None)),
            ('Цена', 25, None, dict(price=25, source_price_with_vat=25, source_price_basis='UNKNOWN', unit_price_with_vat=None, normalized_price_with_vat=None)),
            ('Цена за штуку с НДС', 25, None, dict(price=25, unit_price_with_vat=25, normalized_price_with_vat=25)),
            ('Цена за 100 шт с НДС', 120, None, dict(price=1.2, source_price_with_vat=120, unit_price_with_vat=1.2, price_basis_quantity=100)),
            ('Цена за короб с НДС', 120, 7, dict(price=17.14, source_price_with_vat=120, box_price_with_vat=120, price_basis_quantity=7)),
        ]
        for index, (header, price, packs, expected) in enumerate(cases):
            with self.subTest(header=header, packs=packs):
                source = self.source(['Артикул', 'Наименование', header, 'Шт в коробе'], [[str(index), 'Молоко', price, packs]])
                self.import_rows(self.parse(source))
                actual = self.actual(str(index))
                self.assertEqual({f: actual[f] for f in expected}, expected)
        source = self.source(['Артикул', 'Наименование', 'Цена за шт без НДС', 'НДС'], [['NET', 'Молоко', '10,05', '20%']])
        self.import_rows(self.parse(source))
        self.assertEqual(self.actual('NET')['price'], 12.06)

    def test_invalid_fields_are_null_not_model_repaired(self):
        source = self.source(['Артикул', 'Наименование', 'Цена', 'Шт в коробе', 'НДС', 'Масса нетто кг'],
            [[str(i), 'Молоко', price, 0, '55%', -2] for i, price in enumerate([0, 1, -3, 1000000, 'bad'])])
        def bad_model(batch, **kwargs):
            self.model(batch, **kwargs)
            for entry in batch:
                entry.update(price=999, unit_price_with_vat=999, packs_per_box=12, vat_rate=.2, net_weight_kg=1)
        rows = self.parse(source)
        self.import_rows(rows, model=bad_model)
        for entry in rows:
            actual = self.actual(entry['article'])
            for field in ('price', 'unit_price_with_vat', 'packs_per_box', 'vat_rate', 'net_weight_kg'):
                self.assertIsNone(actual[field], field)
            self.assertTrue(entry['_review_reasons'])
            self.assertIn(entry['_source']['raw_row'][2], ['0', '1', '-3', '1000000', 'bad'])

    def test_package_price_does_not_use_transport_case_count(self):
        source = self.source(['Артикул', 'Наименование', 'Цена за упаковку с НДС', 'Шт в коробе'],
                             [['PACK', 'Молоко', 120, 12]])
        rows = self.parse(source)
        self.import_rows(rows)
        actual = self.actual('PACK')
        self.assertEqual((actual['price'], actual['source_price_with_vat'], actual['source_price_basis']),
                         (120, 120, 'PACKAGE'))
        self.assertIsNone(actual['price_basis_quantity'])
        self.assertIsNone(actual['normalized_price_with_vat'])
        self.assertIsNone(actual['unit_price_with_vat'])
        self.assertEqual(actual['price_normalization_status'], 'BLOCKED')
        self.assertEqual(rows[0]['_source']['raw_row'], ['PACK', 'Молоко', '120', '12'])

    def test_package_count_requires_items_in_same_paid_package(self):
        for index, (quote, count, expected) in enumerate((
            ('Цена за 1 упаковку с НДС', 1, None),
            ('Цена за упаковку с НДС (в коробе 12 шт)', 12, None),
            ('Цена за упаковку с НДС (в упаковке 6 шт)', 6, 20),
        )):
            with self.subTest(quote=quote):
                source = self.source(['Артикул', 'Наименование', 'Цена за упаковку с НДС', 'Примечание'],
                                     [[str(index), 'Молоко', 120, quote]])
                def model(batch, **kwargs):
                    for entry in batch:
                        entry.update(llm._extract(entry, {'_price_basis_quantity': count,
                            '_llm_evidence': [{'field': '_price_basis_quantity', 'text': quote, 'kind': 'literal'}]},
                            kwargs['okpd_ref']))
                self.import_rows(self.parse(source), model=model)
                actual = self.actual(str(index))
                self.assertEqual(actual['normalized_price_with_vat'], expected)
                self.assertEqual(actual['price'], expected if expected is not None else 120)

    def test_ambiguous_price_columns_cannot_bypass_semantic_header(self):
        source = self.source(['Артикул', 'Наименование', 'Цена за шт с НДС предоплата',
                              'Цена за шт с НДС отсрочка'], [['A', 'Молоко', 100, 130]])
        with self.assertRaisesRegex(ValueError, 'REVIEW'):
            self.parse(source)
        self.assertEqual(registry.csv_rows(source)[1], ['A', 'Молоко', '100', '130'])

    def test_cross_tax_columns_with_different_terms_are_ambiguous(self):
        for first, second in [
            ('Цена за шт без НДС предоплата', 'Цена за шт с НДС отсрочка'),
            ('Цена за шт с НДС предоплата', 'Цена за шт без НДС отсрочка'),
            ('Цена за короб с НДС самовывоз', 'Цена за шт без НДС доставка'),
            ('Цена за шт без НДС условие А', 'Цена за шт с НДС условие Б'),
            ('Цена за шт без НДС скидка 5%', 'Цена за шт с НДС скидка 10%'),
        ]:
            with self.subTest(first=first, second=second):
                source = self.source(['Артикул', 'Наименование', first, second], [['A', 'Молоко', 100, 130]])
                with self.assertRaisesRegex(ValueError, 'ambiguous_price_columns'):
                    self.parse(source)

    def test_same_condition_net_gross_prices_are_not_ambiguous(self):
        source = self.source(['Артикул', 'Наименование', 'Цена за шт без НДС предоплата',
                              'Цена за шт с НДС предоплата', 'НДС'], [['A', 'Молоко', 100, 120, '20%']])
        self.import_rows(self.parse(source))
        self.assertEqual(self.actual()['price'], 120)
        self.assertEqual(self.actual()['price_normalization_status'], 'READY')

    def test_formula_money_is_unknown_but_raw_formula_survives(self):
        from openpyxl import Workbook
        source = self.root / 'formula.xlsx'
        wb = Workbook()
        sheet = wb.active
        assert sheet is not None
        sheet.append(['Артикул', 'Наименование', 'Цена за шт с НДС'])
        sheet.append(['A', 'Молоко', '=100+20'])
        wb.save(source)
        wb.close()
        rows = self.parse(source)
        self.import_rows(rows)
        self.assertIsNone(self.actual()['price'])
        self.assertEqual(rows[0]['_source']['cells'][2]['original']['value'], '=100+20')
        self.assertTrue(any('formula' in reason for reason in rows[0]['_review_reasons']))

    def test_conflicting_commercial_fields_are_partial_not_current_prices(self):
        cases = [(['Цена за шт с НДС', 'Цена за шт без НДС', 'НДС'], [120, 90, '20%']),
                 (['Цена за шт с НДС', 'Цена за короб с НДС', 'Шт в коробе'], [25, 120, 6])]
        for index, (headers, values) in enumerate(cases):
            source = self.source(['Артикул', 'Наименование'] + headers, [[str(index), 'Молоко'] + values])
            self.import_rows(self.parse(source))
            actual = self.actual(str(index))
            self.assertIsNone(actual['price'])
            self.assertIsNone(actual['unit_price_with_vat'])
            self.assertIsNone(actual['normalized_price_with_vat'])
