import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parents[1] / 'checker.py'


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.materials = self.base / 'materials'
        self.materials.mkdir()
        self.requirements = self.base / 'requirements.csv'
        self.out = self.base / 'report'

    def rows(self, rows):
        with self.requirements.open('w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'title', 'required', 'pattern'])
            writer.writerows(rows)

    def run_cli(self, expected):
        result = subprocess.run([sys.executable, str(CLI), '--requirements',
                                 str(self.requirements), '--materials',
                                 str(self.materials), '--output', str(self.out)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        if expected != 2:
            self.assertEqual(result.stderr, '')
        if expected == 2:
            self.assertIn('错误', result.stderr)
            self.assertFalse(self.out.exists())
        return result

    def test_missing_and_multiple_versions_are_not_passed(self):
        self.rows([['A', '资格', 'yes', '资格*.pdf'],
                   ['B', '授权', 'yes', '授权.pdf'],
                   ['C', '说明', 'no', '说明.txt'],
                   ['D', '报价', 'yes', '报价.pdf']])
        for name in ['资格v1.pdf', '资格v2.pdf', '报价.pdf']:
            (self.materials / name).touch()
        self.run_cli(1)
        report = json.loads((self.out / 'report.json').read_text())
        self.assertEqual([r['status'] for r in report['items']],
                         ['review', 'missing', 'optional_missing', 'found'])
        self.assertEqual(report['items'][0]['candidates'], ['资格v1.pdf', '资格v2.pdf'])
        self.assertTrue((self.out / 'report.html').exists())
        self.assertTrue((self.out / 'report.csv').exists())

    def test_recursive_filename_only_and_no_content_read(self):
        self.rows([['A', '材料', 'yes', '附件?.pdf']])
        nested = self.materials / '子目录'
        nested.mkdir()
        attachment = nested / '附件1.pdf'
        attachment.write_bytes(b'\xff\x00not a pdf')
        self.run_cli(0)
        report = json.loads((self.out / 'report.json').read_text())
        self.assertEqual(report['items'][0]['candidates'], ['子目录/附件1.pdf'])
        self.assertEqual(attachment.read_bytes(), b'\xff\x00not a pdf')

    def test_shared_candidate_requires_review(self):
        self.rows([['A', '一', 'yes', '*.pdf'], ['B', '二', 'yes', '同一.pdf']])
        (self.materials / '同一.pdf').touch()
        self.run_cli(1)
        report = json.loads((self.out / 'report.json').read_text())
        self.assertEqual([r['status'] for r in report['items']], ['review', 'review'])

    def test_html_escape_and_formula_protection(self):
        self.rows([['=1+1', '<script>alert(1)</script>', 'yes', '*.pdf']])
        (self.materials / '=2+2.pdf').touch()
        self.run_cli(0)
        html = (self.out / 'report.html').read_text()
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        with (self.out / 'report.csv').open(encoding='utf-8-sig', newline='') as f:
            row = next(csv.DictReader(f))
        self.assertEqual(row['id'], "'=1+1")
        self.assertEqual(row['candidates'], "'=2+2.pdf")

    def test_invalid_rows_rejected(self):
        cases = [[], [['A', 'a', 'yes', 'x'], ['A', 'b', 'no', 'y']],
                 [['A', 'a', 'maybe', 'x']], [['', 'a', 'yes', 'x']],
                 [['A', 'a', 'yes', '../x']], [['A', 'a', 'yes', '/tmp/x']],
                 [['A', 'a', 'yes', '[ab].pdf']], [['A', 'a', 'yes', '']],
                 [['A', '', 'yes', 'x']], [['A', 'a', 'yes', '**.pdf']]]
        for rows in cases:
            with self.subTest(rows=rows):
                self.rows(rows)
                self.run_cli(2)

    def test_malformed_csv_and_encoding_rejected(self):
        for content in [b'id,title,required,pattern\nA,x,yes,y,extra\n',
                        b'id,title,required,pattern\nA,x,yes\n',
                        b'id,title,required,pattern\n"unterminated', b'\xff']:
            with self.subTest(content=content):
                self.requirements.write_bytes(content)
                self.run_cli(2)

    def test_symlinks_rejected(self):
        self.rows([['A', 'a', 'yes', '*.pdf']])
        outside = self.base / 'outside'
        outside.mkdir()
        link = self.materials / 'linked'
        link.symlink_to(outside, target_is_directory=True)
        self.run_cli(2)
        link.unlink()
        link.symlink_to(self.base / 'absent.pdf')
        self.run_cli(2)

    def test_input_symlink_component_rejected(self):
        self.rows([['A', 'a', 'yes', '*.pdf']])
        link = self.base / 'alias'
        link.symlink_to(self.materials, target_is_directory=True)
        self.materials = link
        self.run_cli(2)

    def test_output_inside_materials_rejected(self):
        self.rows([['A', 'a', 'yes', '*.pdf']])
        self.out = self.materials / 'output'
        self.run_cli(2)

    def test_double_slash_output_cannot_bypass_material_boundary(self):
        self.rows([['A', 'a', 'yes', '*.pdf']])
        self.out = Path('//' + str(self.materials / 'output').lstrip('/'))
        self.run_cli(2)

    def test_case_alias_output_cannot_bypass_material_boundary(self):
        alias = self.base / 'MATERIALS'
        if not alias.exists() or not alias.samefile(self.materials):
            self.skipTest('requires a case-insensitive filesystem')
        self.rows([['A', 'a', 'yes', '*.pdf']])
        self.out = alias / 'output'
        self.run_cli(2)

    def test_existing_output_is_never_overwritten(self):
        self.rows([['A', 'a', 'yes', '*.pdf']])
        self.out.mkdir()
        sentinel = self.out / 'report.html'
        sentinel.write_text('keep')
        result = subprocess.run([sys.executable, str(CLI), '--requirements',
                                 str(self.requirements), '--materials', str(self.materials),
                                 '--output', str(self.out)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(sentinel.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
