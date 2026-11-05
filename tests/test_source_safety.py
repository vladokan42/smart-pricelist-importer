"""Reader safety and synthetic demo checks, with no real network or database."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from openpyxl import Workbook
from examples.create_demo import create_demo
import import_pricelist as cli
import llm_price_enricher as llm
import price_download as reader
import pricelist_source_contract as contract
import update_price_registry as registry


class SourceSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'book.xlsx'
        self.addCleanup(patch.stopall)
        patch('socket.socket', side_effect=AssertionError('network forbidden')).start()

    def book(self, name='Творог', price=120):
        book = Workbook()
        sheet = book.active
        assert sheet is not None
        sheet.append(['Артикул', 'Наименование', 'Цена за шт с НДС'])
        sheet.append(['0001', name, price])
        book.save(self.path)
        book.close()
        return self.path

    def test_demo_real_cli_mocked_network_and_incomplete_reference(self):
        create_demo(self.path)
        ref = Path(__file__).resolve().parents[1] / 'examples/demo_reference.json'
        self.assertIn('НЕПОЛНЫЙ ТЕСТОВЫЙ', json.loads(ref.read_text())['_notice'])
        expected_ref = {'10.51.40.300': 'Творог', '10.84.30': 'Соль пищевая'}
        self.assertEqual(cli.read_reference(ref), expected_ref)
        def model(request, **kwargs):
            user = json.loads(json.loads(request.data)['messages'][1]['content'])
            self.assertEqual(user['okpd2_reference'], expected_ref)
            answer = []
            for row in user['rows']:
                phrase = 'Творог' if 'Творог' in row['structured']['name'] else 'Соль пищевая'
                code = next(code for code, label in user['okpd2_reference'].items() if label == phrase)
                answer.append({'idx': row['idx'], 'okpd2_code': code,
                               '_llm_evidence': [{'field': 'okpd2_code', 'text': phrase, 'kind': 'classification'}]})
            return io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(answer)}}]}).encode())
        out = self.root / 'demo-result.json'
        with patch.dict(os.environ, {'LLM_BASE_URL': 'https://offline.example.test/v1', 'LLM_MODEL': 'mock', 'LLM_API_KEY': 'offline'}, clear=True), \
             patch.object(llm.urllib.request, 'urlopen', side_effect=model), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli.main([str(self.path), '--supplier', 'Демо', '--reference', str(ref), '--out', str(out)]), 0)
        payload = json.loads(out.read_text())
        self.assertEqual(len(payload['items']), 4)
        self.assertEqual(payload['report']['classified'], 4)
        self.assertEqual([x['okpd2_code'] for x in payload['items']],
                         ['10.51.40.300', '10.84.30', '10.51.40.300', '10.84.30'])
        self.assertEqual([x['vat_rate'] for x in payload['items']], [.1, 0, None, None])
        self.assertEqual(payload['report']['skipped_categories'], 1)
        self.assertEqual({x['_source']['sheet_index'] for x in payload['items']}, {0, 1})
        self.assertIsNone(payload['items'][-1]['price'])

    def test_demo_never_replaces_existing_file(self):
        self.path.write_bytes(b'KEEP')
        with self.assertRaises(FileExistsError):
            create_demo(self.path)
        self.assertEqual(self.path.read_bytes(), b'KEEP')

    def test_formula_name_rejected_and_formula_price_retained_only_as_raw(self):
        self.book(name='=1+1')
        with self.assertRaisesRegex(ValueError, 'no identifiable'):
            registry.parse_semantic_pricelist(self.path, 'Демо', '2026-11-06')
        self.book(price='=100+20')
        rows, _ = registry.parse_semantic_pricelist(self.path, 'Демо', '2026-11-06')
        registry.calculate_semantic_prices(rows[0])
        self.assertIsNone(rows[0]['price'])
        self.assertEqual(rows[0]['_source']['cells'][2]['original']['value'], '=100+20')

    def test_extension_signature_mismatch(self):
        self.path.write_bytes(b'not a workbook')
        with self.assertRaisesRegex(ValueError, 'signature'):
            reader._read(self.path)
        self.path.with_suffix('.xls').write_bytes(b'not OLE')
        with self.assertRaisesRegex(ValueError, 'signature'):
            reader._read(self.path.with_suffix('.xls'))

    def test_symlink_hardlink_and_size_limits(self):
        self.book()
        link = self.root / 'link.xlsx'
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            reader._read(link)
        link.unlink()
        os.link(self.path, link)
        with self.assertRaisesRegex(ValueError, 'hardlinked'):
            reader._read(link)
        link.unlink()
        with patch.object(contract, 'MAX_BYTES', 1), self.assertRaisesRegex(ValueError, 'size'):
            reader._read(self.path)

    def test_cell_limit_preserved(self):
        self.book()
        with patch.object(reader, 'MAX_CELLS', 1), self.assertRaisesRegex(ValueError, 'cell limit'):
            reader._read(self.path)
        self.assertEqual(reader.MAX_CELLS, 1_000_000)
        self.assertEqual(contract.MAX_BYTES, 50 * 1024 * 1024)

    def test_archive_macros_traversal_expansion_and_entry_limits(self):
        for member, issue in [('xl/vbaProject.bin', 'macro_parts'), ('../escape.xml', 'unsafe_member')]:
            with self.subTest(member=member):
                with zipfile.ZipFile(self.path, 'w') as archive:
                    archive.writestr(member, b'x')
                with self.assertRaisesRegex(ValueError, issue):
                    reader._read(self.path)
        self.book()
        for name, value, issue in [('MAX_ZIP_ENTRIES', 1, 'too_many_entries'),
                                   ('MAX_ZIP_UNCOMPRESSED_BYTES', 1, 'uncompressed_too_large'),
                                   ('MAX_ZIP_ENTRY_BYTES', 1, 'entry_too_large'),
                                   ('MAX_ZIP_COMPRESSION_RATIO', 1, 'compression_ratio')]:
            with self.subTest(limit=name), patch.object(contract, name, value), self.assertRaisesRegex(ValueError, issue):
                reader._read(self.path)

    def test_external_links_block_semantic_reader(self):
        with patch.object(reader, '_read', return_value=(self.path, 'xlsx', [], {'external_links': 1})):
            with self.assertRaisesRegex(ValueError, 'external workbook'):
                registry.semantic_source_sheets(self.path)

    def test_csv_quoted_multiline_and_doubled_quote_survives(self):
        path = self.root / 'synthetic.csv'
        path.write_text('Наименование;Цена за шт с НДС\n"Творог ""Марка""\n200 г";120\n')
        rows, _ = registry.parse_semantic_pricelist(path, 'Демо', '2026-11-06')
        self.assertEqual(rows[0]['full_name'], 'Творог "Марка" 200 г')
        self.assertEqual(rows[0]['_source']['raw_row'][0], 'Творог "Марка"\n200 г')

    def test_invalid_prices_and_bad_vat_do_not_become_current(self):
        for price in (True, 0, 1, -3, 1_000_000, 'bad', float('nan'), float('inf')):
            with self.subTest(price=price), self.assertRaises(ValueError):
                registry.validate_price(price)
        for rate in (None, 'bad', 55):
            self.assertIsNone(registry.normalize_vat_rate(rate))

    def test_large_id_and_text_identifiers_roundtrip_through_xlsx(self):
        book = Workbook()
        sheet = book.active
        assert sheet is not None
        sheet.append(['ID', 'Артикул', 'Наименование', 'Штрихкод'])
        sheet.append(['9007199254740993', '000123.0', 'Творог', '000456.0'])
        book.save(self.path)
        book.close()
        rows, _ = registry.parse_semantic_pricelist(self.path, 'Демо', '2026-11-06')
        self.assertEqual(rows[0]['_existing_id'], 9007199254740993)
        self.assertEqual(rows[0]['article'], '000123.0')
        self.assertEqual(rows[0]['barcode'], '000456.0')
