"""End-to-end checks: build the planted 'shop' repo, run arch.py --json, assert the planted facts.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ARCH = os.path.join(HERE, '..', 'skills', 'archaeologist', 'scripts', 'arch.py')
sys.path.insert(0, HERE)
import fixture_shop  # noqa: E402

SVC = 'api/src/invoice/invoice.service.ts'


class ShopRepo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='arch-shop-')
        cls.repo = fixture_shop.build(os.path.join(cls.tmp, 'shop'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_arch(self, *args, ok=(0,)):
        r = subprocess.run([sys.executable, ARCH, *args, '--repo', self.repo, '--json'], capture_output=True, text=True)
        self.assertIn(r.returncode, ok, r.stderr)
        return json.loads(r.stdout)

    def routes_by_name(self):
        return {r['route']: r for r in self.run_arch('routes', '--reach')['routes']}

    # ---- structure
    def test_impact_of_deleting_invoice_service(self):
        r = self.run_arch('impact', SVC, '--delete')
        direct = {d['file']: d for d in r['direct']}
        self.assertEqual(len(direct), 8, sorted(direct))
        self.assertEqual(direct['api/src/reports/reports.service.ts']['via'], 'api/src/invoice/index.ts')  # alias + barrel
        self.assertEqual(r['tier'], 'HIGH')
        self.assertEqual({x['route'] for x in r['routes']},
                         {'POST /api/invoices', 'GET /api/invoices/:id', 'POST /api/payments/reconcile', 'GET /api/reports/revenue'})
        self.assertIn('api/prisma/schema.prisma', [h['file'] for h in r['hidden_coupling']])
        self.assertEqual(r['tests'], ['api/src/invoice/invoice.service.spec.ts'])
        self.assertIn('Do not delete', r['recommendation'])

    def test_impact_of_dead_file_is_low(self):
        r = self.run_arch('impact', 'api/src/common/legacy-format.ts', '--delete')
        self.assertEqual((r['tier'], r['direct']), ('LOW', []))

    def test_python_dependents(self):
        r = self.run_arch('dependents', 'worker/app/tax.py')
        self.assertEqual(sorted(d['file'] for d in r['direct']), ['worker/app/main.py', 'worker/app/report.py'])

    def test_routes_prefixes_guards_and_frameworks(self):
        routes = self.routes_by_name()
        self.assertEqual(set(routes), {
            'POST /api/auth/login', 'POST /api/invoices', 'GET /api/invoices/:id', 'POST /api/payments/reconcile',
            'GET /api/reports/revenue', 'POST /api/webhooks/stripe', 'GET /worker/tax/:param', 'POST /worker/reports/run'})
        self.assertEqual(routes['POST /api/invoices']['guards'], ['TenantGuard'])
        self.assertEqual(routes['POST /api/webhooks/stripe']['guards'], ['verifySignature'])
        self.assertEqual(routes['GET /worker/tax/:param']['guards'], ['get_tenant'])
        self.assertEqual(routes['POST /api/auth/login']['callers'], ['web/src/pages/Login.tsx:9'])
        self.assertEqual(routes['POST /api/auth/login']['db'], ['user'])
        self.assertEqual(routes['POST /api/invoices']['external'], ['stripe'])

    def test_missing_tenant_guard_flags_only_reports(self):
        r = self.run_arch('routes', '--missing-guard', 'TenantGuard')
        self.assertEqual([f['route'] for f in r['flagged']], ['GET /api/reports/revenue'])
        self.assertEqual(r['protected_data'], ['invoice', 'payment'])

    def test_sinks_env_models_jobs(self):
        sinks = self.run_arch('sinks', 'api/src/invoice')['sinks']
        self.assertTrue(any(s['target'] == 'invoice' and 'prisma.invoice.create' in s['code'] for s in sinks))
        env = {e['var']: e for e in self.run_arch('env')['env']}
        self.assertEqual(env['REPORTS_CACHE_TTL']['note'], 'declared but never read')
        self.assertIn('read but not declared', env['WORKER_TOKEN']['note'])
        self.assertIn('api/src/invoice/invoice.service.ts:5', env['STRIPE_SECRET_KEY']['read_at'])
        models = {m['name'] for m in self.run_arch('models')['models']}
        self.assertEqual(models, {'User', 'Invoice', 'Payment', 'StripeEvent'})
        jobs = self.run_arch('jobs')['jobs']
        self.assertEqual([(j['kind'], j['handler']) for j in jobs], [('cron', 'flagOverdue')])

    def test_map_modules_and_edges(self):
        r = self.run_arch('map')
        mods = {m['module']: m for m in r['modules']}
        self.assertIn('api/src/invoice', mods)
        self.assertIn('api/src/invoice', mods['api/src/payments']['uses'])
        self.assertEqual(r['hubs'][0]['file'], 'api/src/prisma/prisma.service.ts')
        detail = self.run_arch('map', '--module', 'payments')
        self.assertEqual([x['route'] for x in detail['routes_list']], ['POST /api/payments/reconcile'])

    def test_unused_and_dupes(self):
        r = self.run_arch('unused')
        self.assertEqual(sorted(f['file'] for f in r['files']), ['api/src/common/legacy-format.ts', 'worker/app/unused_mod.py'])
        d = self.run_arch('dupes')['duplicates']
        self.assertTrue(any(SVC in x['a'] and 'reports.service.ts' in x['b'] for x in d), d)

    # ---- history
    def test_why_markpaid_finds_the_double_payment_fix(self):
        r = self.run_arch('why', SVC + ':markPaid')
        subjects = [c['subject'] for c in r['commits']]
        self.assertIn('fix: prevent double payment on webhook retry (#42)', subjects)
        self.assertIn('Stripe retries webhook deliveries', r['commits'][0]['body'])
        self.assertEqual(r['origin']['subject'], 'feat(payments): reconcile payments against invoices (#20)')

    def test_blame_lines(self):
        why = self.run_arch('why', SVC + ':markPaid')
        start = int(why['lines'].split('-')[0])
        r = self.run_arch('blame', SVC, '%d-%d' % (start + 2, start + 4))
        self.assertTrue(any('#42' in c.get('refs', []) for c in r['commits']))

    def test_decisions_timeline_people_orphans(self):
        d = self.run_arch('decisions')['decisions']
        kinds = {x['subject']: x['kind'] for x in d}
        self.assertEqual(kinds['fix: prevent double payment on webhook retry (#42)'], 'fix')
        revert = next(x for x in d if x['kind'] == 'revert')
        self.assertTrue(revert.get('reverts'))
        t = self.run_arch('timeline')
        self.assertEqual(t['periods'][0]['period'], '2023-Q1')
        self.assertIn('module api/src/payments created', [m['event'] for m in t['milestones']])
        people = {p['name']: p for p in self.run_arch('people')['people']}
        self.assertFalse(people['Bob Martin']['active'])
        self.assertTrue(people['Alice Chen']['active'])
        orphans = {m['area']: m for m in self.run_arch('orphans')['modules']}
        self.assertEqual(orphans['api/src/payments']['main_author'], 'Bob Martin')

    def test_cochange_and_changed_since(self):
        co = self.run_arch('cochange', SVC)
        p = {x['file']: x for x in co['partners']}
        self.assertFalse(p['api/prisma/schema.prisma']['import_link'])
        r = self.run_arch('changed-since', 'HEAD~2')
        self.assertIn('worker/app', [m['module'] for m in r['modules']])
        self.assertIn('POST /worker/reports/run', ['%s %s' % (x['method'], x['path']) for x in r['routes_affected']])

    # ---- verification
    def test_verify_refs_and_docs(self):
        why = self.run_arch('why', SVC + ':markPaid')
        sha = why['commits'][0]['sha']
        good = self.run_arch('verify', SVC + ':1-3', sha, '#42')
        self.assertTrue(good['ok'])
        bad = self.run_arch('verify', 'api/src/nope.ts:3', SVC + ':999', '#999', ok=(1,))
        self.assertEqual([x['ok'] for x in bad['refs']], [False, False, False])
        docs = os.path.join(self.repo, 'docs', 'brain')
        os.makedirs(docs, exist_ok=True)
        with open(os.path.join(docs, 'README.md'), 'w') as f:
            f.write('See `%s:34-44` and commit `%s`.\n\n[good](../../README.md) [bad](missing.md) '
                    'and `api/src/gone.ts:3`, a host like localhost:3000, and `1234567`.\n' % (SVC, sha))
        r = self.run_arch('verify', '--docs', 'docs/brain', ok=(1,))
        self.assertEqual(sorted(p['ref'] for p in r['problems']), ['api/src/gone.ts:3', 'missing.md'])
        shutil.rmtree(os.path.join(self.repo, 'docs'))


class Parsing(unittest.TestCase):
    def test_strippers_keep_offsets(self):
        sys.path.insert(0, os.path.dirname(ARCH))
        from codemap import parse_import_clause, strip_js, strip_py
        t = 'const a = "x // no"; // gone\n/* a\nb */ const u = `${b} // keep`;\n'
        r = strip_js(t)
        self.assertEqual((len(r), r.count('\n')), (len(t), t.count('\n')))
        self.assertNotIn('gone', r)
        self.assertIn('// keep', r)
        self.assertNotIn('gone', strip_py('x = "a # b"  # gone\n'))
        self.assertEqual(parse_import_clause('Foo, { a as b, type C }'), (['a', 'C', 'default'], {'b': 'a', 'C': 'C', 'Foo': 'default'}))


if __name__ == '__main__':
    unittest.main()
