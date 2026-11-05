"""Offline boundaries for the standalone CLI and explicit model settings."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import import_pricelist as cli
import llm_price_enricher as llm
import update_price_registry as registry
from openpyxl import Workbook

ENV = {'LLM_BASE_URL': 'https://model.example.test/v1', 'LLM_MODEL': 'demo-model', 'LLM_API_KEY': 'offline-key'}
REF = {'10.51.52.140': 'Творог'}


def response(rows):
    return io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(rows)}}]}).encode())


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.input = self.root / 'synthetic.xlsx'
        self.output = self.root / 'result.json'
        self.ref = self.root / 'reference.json'
        self.ref.write_text(json.dumps(REF))
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, ENV, clear=True).start()
        patch('socket.socket', side_effect=AssertionError('network forbidden')).start()
        self.network = patch.object(llm.urllib.request, 'urlopen', side_effect=self.model).start()
        patch.object(llm.time, 'sleep').start()
        self.workbook()

    def workbook(self, count=3):
        book = Workbook()
        sheet = book.active
        assert sheet is not None
        sheet.title = 'Первый'
        sheet.append(['Синтетический прайс'])
        sheet.merge_cells('A1:F1')
        sheet.append(['Артикул', 'Наименование', 'Цена за шт с НДС', 'НДС', 'Примечание', 'ID'])
        for i in range(count):
            sheet.append([f'{i:04}', 'Творог 0%', 120 + i, ['10%', None, '0%'][i % 3], 'без догадок', i + 10])
        second = book.create_sheet('Второй')
        second.append(['Артикул', 'Наименование', 'Цена за шт с НДС'])
        second.append(['000Z', 'Творог', 150])
        book.save(self.input)
        book.close()

    def model(self, request, **kwargs):
        rows = json.loads(json.loads(request.data)['messages'][1]['content'])['rows']
        return response([{'idx': row['idx'], 'okpd2_code': '10.51.52.140',
                          'vat_rate': .22, 'price': 9999, 'article': 'WRONG',
                          '_llm_evidence': [{'field': 'okpd2_code', 'text': 'Творог', 'kind': 'classification'}]}
                         for row in rows])

    def run_import(self):
        return cli.run_import(self.input, 'Демо поставщик', self.ref, self.output)

    def test_cli_success_multisheet_source_values_vat_zero_and_no_path_leak(self):
        before = self.input.read_bytes()
        result = self.run_import()
        disk = json.loads(self.output.read_text())
        self.assertEqual(result, disk)
        self.assertEqual(len(disk['items']), 4)
        self.assertEqual([x['vat_rate'] for x in disk['items']], [.1, None, 0, None])
        self.assertEqual([x['article'] for x in disk['items']], ['0000', '0001', '0002', '000Z'])
        self.assertEqual([x['_existing_id'] for x in disk['items'][:3]], [10, 11, 12])
        self.assertEqual([x['price'] for x in disk['items']], [120, 121, 122, 150])
        self.assertEqual(disk['items'][0]['okpd2_name'], REF['10.51.52.140'])
        self.assertTrue(disk['evidence'])
        self.assertEqual(self.input.read_bytes(), before)
        for call in self.network.call_args_list:
            self.assertNotIn(str(self.root), call.args[0].data.decode())
        self.assertEqual(disk['source']['sha256'], cli.registry.file_sha256(self.input))

    def test_batches_ten_and_tail(self):
        self.workbook(22)
        self.run_import()
        sizes = [len(json.loads(json.loads(c.args[0].data)['messages'][1]['content'])['rows']) for c in self.network.call_args_list]
        self.assertEqual(sizes, [10, 10, 3])

    def test_later_batch_failure_leaves_no_partial_output(self):
        self.workbook(12)
        self.network.side_effect = [response([{'idx': i} for i in range(10)]), RuntimeError('failed')]
        with self.assertRaises(RuntimeError):
            self.run_import()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.result.json.*')), [])

    def test_invalid_response_has_no_output(self):
        self.network.side_effect = lambda *a, **k: response([{'idx': 99}])
        with self.assertRaises(ValueError):
            self.run_import()
        self.assertEqual(self.network.call_count, 3)
        self.assertFalse(self.output.exists())

    def test_existing_output_input_reference_and_symlink_never_overwritten(self):
        self.output.write_bytes(b'KEEP')
        with self.assertRaises(FileExistsError):
            self.run_import()
        self.assertEqual(self.output.read_bytes(), b'KEEP')
        for target in (self.input, self.ref):
            before = target.read_bytes()
            with self.assertRaises((ValueError, FileExistsError)):
                cli.run_import(self.input, 'Демо', self.ref, target)
            self.assertEqual(target.read_bytes(), before)
        self.output.unlink()
        self.output.symlink_to(self.root / 'missing')
        with self.assertRaises((ValueError, FileExistsError)):
            self.run_import()
        self.network.assert_not_called()

    def test_atomic_publish_race_preserves_other_writer(self):
        original = cli.os.link
        def race(source, target):
            Path(target).write_bytes(b'OTHER')
            return original(source, target)
        with patch.object(cli.os, 'link', side_effect=race), self.assertRaises(FileExistsError):
            self.run_import()
        self.assertEqual(self.output.read_bytes(), b'OTHER')
        self.assertEqual(list(self.root.glob('.result.json.*')), [])

    def test_json_serialization_failure_cleans_temp(self):
        with self.assertRaises(ValueError):
            cli.atomic_json(self.output, {'not_finite': float('nan')})
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.result.json.*')), [])

    def test_fsync_failure_does_not_publish_output(self):
        with patch.object(cli.os, 'fsync', side_effect=OSError('failed')), self.assertRaises(OSError):
            self.run_import()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.result.json.*')), [])

    def test_directory_fsync_failure_does_not_claim_failed_import(self):
        with patch.object(cli.os, 'fsync', side_effect=[None, OSError('directory fsync unavailable')]):
            result = self.run_import()
        self.assertEqual(json.loads(self.output.read_text()), result)

    def test_identifier_strings_and_large_integer_id_preserved(self):
        self.assertEqual(registry.normalize_article('000123.0'), '000123.0')
        self.assertEqual(registry.normalize_article(123.0), '123')
        self.assertEqual(registry.parse_strict_number('9007199254740993', integer=True), 9007199254740993)

    def test_money_rounding_uses_source_decimal_not_rounded_float(self):
        source = self.root / 'decimal.csv'
        source.write_text('Наименование;Цена за шт с НДС\nТворог;2.6749999999999999\n')
        result = cli.run_import(source, 'Демо', self.ref, self.output)
        self.assertEqual(result['items'][0]['price'], 2.67)

    def test_vat_cannot_join_tax_word_with_fat_or_price_digits(self):
        item = {'name': 'Творог 20%', '_source': {'headers': ['Наименование', 'Цена за шт с НДС'],
                'raw_row': ['Творог 20%', 120]}}
        proposal = {'vat_rate': .2, '_llm_evidence': [
            {'field': 'vat_rate', 'text': 'НДС'}, {'field': 'vat_rate', 'text': '20'}]}
        self.assertIsNone(llm._extract(item, proposal, REF)['vat_rate'])

    def test_reference_malformed_duplicate_and_nonexact_keys_block_before_network(self):
        for text in ('[]', '{"bad":"name"}', '{"10.51.52.140":""}', '{"10.51.52.140":5}',
                     '{" 10.51.52.140 ":"name"}', '{"10.51.52.140":"a","10.51.52.140":"b"}'):
            with self.subTest(text=text):
                self.ref.write_text(text)
                with self.assertRaises(ValueError):
                    self.run_import()
                self.assertFalse(self.output.exists())
        self.network.assert_not_called()

    def test_main_real_argument_parser_and_exit_codes(self):
        with patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli.main([str(self.input), '--supplier', 'Демо', '--reference', str(self.ref), '--out', str(self.output)]), 0)
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            self.assertEqual(cli.main([str(self.input), '--supplier', 'Демо', '--reference', str(self.ref), '--out', str(self.output)]), 2)
        self.assertNotIn(ENV['LLM_API_KEY'], err.getvalue())


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.network = patch.object(llm.urllib.request, 'urlopen', side_effect=AssertionError('no network')).start()
        patch('socket.socket', side_effect=AssertionError('network forbidden')).start()

    def test_explicit_settings_no_file_or_db_access(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(Path, 'read_text', side_effect=AssertionError('no files')):
            self.assertEqual(llm.get_settings(), ('https://model.example.test/v1/chat/completions', 'demo-model', 'offline-key'))
        for name in ENV:
            env = dict(ENV)
            env.pop(name)
            with patch.dict(os.environ, env, clear=True), self.assertRaises(RuntimeError):
                llm.get_settings()
        self.network.assert_not_called()

    def test_bad_url_no_implicit_endpoint(self):
        for url in ('file:///tmp/model', 'https://user:password@host/v1', 'https://host/v1?key=secret', 'https://host/v1#fragment', 'not-url'):
            with patch.dict(os.environ, {**ENV, 'LLM_BASE_URL': url}, clear=True), self.assertRaises(ValueError):
                llm.get_settings()

    def test_reference_required_no_sqlite_fallback(self):
        with patch.dict(os.environ, ENV, clear=True), self.assertRaises(ValueError):
            llm.enrich_items_batch([{'name': 'Творог'}])
        self.network.assert_not_called()

    def test_request_sanitized_without_mutating_source(self):
        entry = {'name': 'Творог', 'source_file_name': '/private/path/source.xlsx',
                 '_source': {'file': '/private/path/source.xlsx', 'raw_path': '/private/raw.xlsx',
                             'headers': ['Наименование'], 'raw_row': ['Творог']}}
        before = copy.deepcopy(entry)
        with patch.dict(os.environ, ENV, clear=True):
            self.network.side_effect = lambda *a, **k: response([{'idx': 0}])
            llm.enrich_items_batch([entry], okpd_ref=REF)
        request = self.network.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, ENV['LLM_BASE_URL'] + '/chat/completions')
        self.assertEqual(payload['model'], ENV['LLM_MODEL'])
        self.assertNotIn('/private/', request.data.decode())
        self.assertEqual(entry['_source'], before['_source'])
