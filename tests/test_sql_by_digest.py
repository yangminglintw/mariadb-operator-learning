"""Run the documented Bash script with a local kubectl/SQL fixture.

Uses SQLite to evaluate the SELECT expressions, not a MariaDB integration test.
Run: python3 -m unittest discover -s tests -v
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MOCK = r'''#!/usr/bin/env python3
import json, os, sqlite3, sys
args = sys.argv[1:]
assert '--context=test-context' in args and args[args.index('-n')+1] == 'test-ns'
if 'get' in args:
    print('1' if 'sts' in args else 'db-0')
    sys.exit()
if 'printenv' in args:
    print('fixture-password')
    sys.exit()
query = args[args.index('-e')+1].replace('\\G', '')
fixture = json.loads(os.environ['DIGEST_FIXTURE'])
if 'GLOBAL_VARIABLES' in query:
    if fixture.get('limit_error'):
        print('fixture: variable lookup failed', file=sys.stderr)
        sys.exit(1)
    print(fixture.get('limit', ''))
    sys.exit()
db = sqlite3.connect(':memory:')
db.execute("ATTACH DATABASE ':memory:' AS performance_schema")
db.create_function('OCTET_LENGTH', 1, lambda x: None if x is None else len(x.encode('utf-8')))
db.create_function('CONCAT', -1, lambda *xs: ''.join(map(str, xs)))
for suffix in ('history', 'history_long'):
    table = 'performance_schema.events_statements_' + suffix
    db.execute(f'CREATE TABLE {table} (SQL_TEXT TEXT, DIGEST TEXT, CURRENT_SCHEMA TEXT, ROWS_EXAMINED INT, ROWS_SENT INT, EVENT_ID INT)')
    if fixture.get('source', 'history') == suffix:
        db.execute(f'INSERT INTO {table} VALUES (?, ?, ?, 5, 1, 1)', (fixture.get('sql'), 'abc123', 'app'))
db.execute('CREATE TABLE performance_schema.events_statements_summary_by_digest (SCHEMA_NAME TEXT, DIGEST_TEXT TEXT, SUM_NO_INDEX_USED INT, COUNT_STAR INT, DIGEST TEXT)')
if fixture.get('summary'):
    db.execute('INSERT INTO performance_schema.events_statements_summary_by_digest VALUES (?, ?, 5, 5, ?)', ('app', 'INSERT INTO t VALUES (...) , ...', 'abc123'))
cur = db.execute(query)
for i, row in enumerate(cur.fetchall(), 1):
    if '-N' in args:
        print('\t'.join('NULL' if x is None else str(x) for x in row))
    else:
        print(f'*** {i}. row ***')
        for field, value in zip(cur.description, row):
            print(f'{field[0]}: {value if value is not None else "NULL"}')
'''


class DigestOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        doc = (ROOT / 'tools/02-no-index-monitoring-scripts.md').read_text()
        cls.script = cls.directory / 'get_sql_by_digest.sh'
        cls.script.write_text(doc.split('```bash\n', 1)[1].split('```', 1)[0])
        mock = cls.directory / 'kubectl'
        mock.write_text(MOCK)
        mock.chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_script(self, fixture, verbose=False):
        env = dict(os.environ, PATH=str(self.directory) + os.pathsep + os.environ['PATH'],
                   DIGEST_FIXTURE=json.dumps(fixture))
        args = ['bash', str(self.script), 'test-context', 'test-ns', 'db', 'abc123']
        if verbose:
            args.append('--verbose')
        result = subprocess.run(args, env=env, capture_output=True, text=True, check=True)
        return result.stdout

    def test_bash_syntax(self):
        subprocess.run(['bash', '-n', str(self.script)], check=True)

    def test_short_sql_and_literal_dots(self):
        sql = "SELECT '...';"
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                output = self.run_script({'sql': sql, 'limit': '1024'}, verbose)
                self.assertIn(sql, output)
                self.assertIn('capture_status' + (': ' if verbose else '=') + 'below_limit', output)

    def test_utf8_bytes_at_and_above_limit(self):
        sql = "SELECT '中文';"
        for limit in (len(sql.encode()), len(sql.encode()) - 1):
            for source in ('history', 'history_long'):
                for verbose in (False, True):
                    with self.subTest(limit=limit, source=source, verbose=verbose):
                        output = self.run_script({'sql': sql, 'limit': str(limit), 'source': source}, verbose)
                        self.assertIn(sql, output)
                        self.assertIn('capture_status' + (': ' if verbose else '=') + 'possibly_truncated', output)
                        self.assertIn(('sql_text_bytes: ' if verbose else 'bytes=') + str(len(sql.encode())), output)

    def test_unavailable_or_invalid_limit(self):
        for fixture in ({'limit': ''}, {'limit': 'not-a-number'}, {'limit_error': True}):
            for verbose in (False, True):
                with self.subTest(fixture=fixture, verbose=verbose):
                    output = self.run_script(dict(fixture, sql='SELECT 1'), verbose)
                    self.assertIn('SELECT 1', output)
                    self.assertIn('capture_status' + (': ' if verbose else '=') + 'unknown', output)

    def test_null_sql(self):
        for verbose in (False, True):
            output = self.run_script({'sql': None, 'limit': '1024'}, verbose)
            self.assertIn('capture_status' + (': ' if verbose else '=') + 'unknown', output)

    def test_summary_is_labeled_as_template(self):
        output = self.run_script({'source': 'none', 'summary': True})
        self.assertIn('=== DIGEST_TEXT：正規化模板', output)
        self.assertIn('INSERT INTO t VALUES (...) , ...', output)
        self.assertNotIn('[SQL_TEXT;', output)

    def test_missing_digest(self):
        output = self.run_script({'source': 'none'})
        self.assertIn('Digest abc123 not found on any pod.', output)


if __name__ == '__main__':
    unittest.main()
