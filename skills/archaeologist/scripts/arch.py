#!/usr/bin/env python3
"""arch.py - deterministic facts for code archaeology. Stdlib only (Python 3.8+), needs git.

Run from the project you are investigating (or pass --repo DIR):
    python3 arch.py overview
    python3 arch.py impact src/invoice/invoice.service.ts --delete
    python3 arch.py routes --missing-guard TenantGuard
Every command prints compact text; add --json for structured output. Nothing is written to disk.
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import posixpath
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import codemap  # noqa: E402
import githist  # noqa: E402
from codemap import Repo, find_root, impact_score, is_test, module_of  # noqa: E402


class UsageError(Exception):
    pass


def rel(repo, path):
    if not path:
        raise UsageError('missing file argument')
    full = os.path.abspath(path) if os.path.isabs(path) or os.path.exists(path) else os.path.join(repo.root, path)
    r = os.path.relpath(full, repo.root).replace(os.sep, '/')
    if r in repo.fileset:
        return r
    guess = difflib.get_close_matches(r, repo.files, n=3, cutoff=0.5) or \
        [f for f in repo.files if posixpath.basename(f) == posixpath.basename(r)][:3]
    raise UsageError('%s is not a tracked file in %s%s' % (path, repo.root, ('; did you mean: ' + ', '.join(guess)) if guess else ''))


def need_git(repo):
    if not repo.is_git:
        raise UsageError('this command needs git history, and %s is not a git repository' % repo.root)


# ------------------------------------------------------------------ commands

def cmd_overview(repo, a):
    langs, loc = Counter(), Counter()
    for p in repo.code:
        langs[repo.lang(p)] += 1
        loc[repo.lang(p)] += repo.loc(p)
    manifests = []
    for d, pkg in sorted(repo.packages.items()):
        deps = dict(pkg.get('dependencies') or {}, **(pkg.get('devDependencies') or {}))
        manifests.append({'manifest': posixpath.join(d, 'package.json'), 'name': pkg.get('name', ''),
                          'frameworks': sorted({codemap.FRAMEWORK_DEPS[k] for k in deps if k in codemap.FRAMEWORK_DEPS}),
                          'scripts': pkg.get('scripts') or {}})
    for p in repo.files:
        b = posixpath.basename(p)
        if re.match(r'^requirements.*\.txt$|^pyproject\.toml$|^Pipfile$|^setup\.(py|cfg)$', b):
            names = {m.lower() for m in re.findall(r'(?m)^\s*["\']?([A-Za-z][\w.-]*)', repo.text(p))}
            manifests.append({'manifest': p, 'frameworks': sorted({codemap.FRAMEWORK_DEPS[n] for n in names if n in codemap.FRAMEWORK_DEPS})})
    infra = [p for p in repo.files if re.search(r'(^|/)(Dockerfile[^/]*|docker-compose[^/]*\.ya?ml|compose\.ya?ml|Makefile|Procfile|'
                                                r'\.github/workflows/[^/]+\.ya?ml|\.gitlab-ci\.yml|serverless\.ya?ml|vercel\.json|netlify\.toml|'
                                                r'[^/]+\.tf|fly\.toml|render\.yaml|app\.yaml)$', p)]
    docs = [p for p in repo.files if re.search(r'(^|/)(README|CONTRIBUTING|ARCHITECTURE|CHANGELOG)[^/]*$', p, re.I)]
    tests = [p for p in repo.code if is_test(p)]
    out = {'root': repo.root, 'files': len(repo.files), 'code_files': len(repo.code),
           'languages': [{'lang': k, 'files': n, 'loc': loc[k]} for k, n in langs.most_common()],
           'manifests': manifests, 'entry_files': [{'file': f, 'why': w} for f, w in sorted(repo.entry_files.items())],
           'modules': len(repo.modules), 'routes': len(repo.routes), 'tests': {'files': len(tests),
           'dirs': sorted({posixpath.dirname(t) for t in tests})[:10]}, 'infra': infra[:20], 'docs': docs[:20]}
    if repo.is_git:
        commits = githist.log(repo.root)
        idx = githist.authors_index(commits)
        out['history'] = {'commits': len(commits), 'first': githist.day(commits[-1]['ts']) if commits else None,
                          'last': githist.day(commits[0]['ts']) if commits else None,
                          'contributors': [{'name': p['name'], 'commits': p['commits'], 'active': p['active']}
                                           for p in sorted(idx.values(), key=lambda p: -p['commits'])[:8]],
                          'hot_files_last_year': githist.hot_files(repo.root, repo, top=10)}
    return out


def module_stats(repo):
    medges = repo.module_edges()
    routes = Counter(module_of(r['file']) for r in repo.routes)
    commits = githist.log(repo.root) if repo.is_git else []
    idx = githist.authors_index(commits) if commits else {}
    mod_commits, mod_auth, mod_first, mod_last = Counter(), defaultdict(Counter), {}, {}
    for c in commits:
        seen = set()
        for _, p, _ in c['files']:
            if p in repo.codeset:
                seen.add(module_of(p))
        for m in seen:
            mod_commits[m] += 1
            mod_auth[m][c['email'] or c['author']] += 1
            mod_first[m] = c['ts']
            mod_last.setdefault(m, c['ts'])
    stats = []
    for m, files in repo.modules.items():
        deps = {b: n for (x, b), n in medges.items() if x == m}
        users = {x: n for (x, b), n in medges.items() if b == m}
        db, ext = set(), set()
        for f in files:
            for s in repo.sinks_in(f):
                (db if s['kind'] == 'db' else ext).add(s['target'])
        loc = sum(repo.loc(f) for f in files)
        importance = 2 * math.log2(1 + sum(users.values())) + routes[m] + math.log2(1 + mod_commits[m]) + math.log2(1 + loc) / 2
        stats.append({'module': m, 'files': len(files), 'loc': loc, 'tests': sum(1 for f in files if is_test(f)),
                      'routes': routes[m], 'uses': sorted(deps, key=lambda k: -deps[k]), 'used_by': sorted(users, key=lambda k: -users[k]),
                      'db': sorted(db), 'external': sorted(ext), 'commits': mod_commits[m],
                      'owners': ['%s%s' % (idx[k]['name'], '' if idx[k]['active'] else ' (inactive)') for k, _ in mod_auth[m].most_common(3)],
                      'first': githist.day(mod_first[m]) if m in mod_first else None,
                      'last': githist.day(mod_last[m]) if m in mod_last else None, 'importance': round(importance, 1)})
    stats.sort(key=lambda s: -s['importance'])
    return stats


def find_module(repo, name):
    if name in repo.modules:
        return name
    hits = [m for m in repo.modules if m.endswith('/' + name.strip('/')) or posixpath.basename(m) == name.strip('/')]
    if len(hits) == 1:
        return hits[0]
    raise UsageError('module %r %s. Modules: %s' % (name, 'is ambiguous' if hits else 'not found', ', '.join(sorted(hits or repo.modules))))


def cmd_map(repo, a):
    if a.module:
        return module_detail(repo, find_module(repo, a.module))
    stats = module_stats(repo)
    hubs = sorted(((f, len(repo.direct_dependents(f))) for f in repo.code), key=lambda x: -x[1])[:10]
    return {'modules': stats, 'module_edges': [{'from': x, 'to': b, 'imports': n} for (x, b), n in repo.module_edges().most_common()],
            'hubs': [{'file': f, 'direct_dependents': n} for f, n in hubs if n], 'entry_files': sorted(repo.entry_files),
            'jobs': len(repo.jobs()), 'routes': len(repo.routes)}


def module_detail(repo, m):
    files = repo.modules[m]
    stat = next(s for s in module_stats(repo) if s['module'] == m)
    ext_pkgs = Counter(e['external'] for f in files for e in repo.out_edges.get(f, []) if e['external'])
    env = [e['var'] for e in repo.env() if any(r.split(':')[0] in files for r in e['read_at'])]
    models = [x for x in repo.models()['models'] if x['file'] in files or any(u in files for u in x['used_by'])]
    out = dict(stat)
    out['file_list'] = [{'file': f, 'loc': repo.loc(f), 'dependents': len(repo.direct_dependents(f)),
                         'exports': [n for n, _ in repo.exports(f)][:8], 'test': is_test(f)} for f in sorted(files)]
    out['packages'] = [k for k, _ in ext_pkgs.most_common(12)]
    out['routes_list'] = [{'route': '%s %s' % (r['method'], r['path']), 'at': '%s:%d' % (r['file'], r['line']),
                           'handler': r['handler'], 'guards': r['guards']} for r in repo.routes if r['file'] in files]
    out['sinks'] = [{'kind': s['kind'], 'target': s['target'], 'at': '%s:%d' % (s['file'], s['line'])} for f in files for s in repo.sinks_in(f)]
    out['env'] = env
    out['models'] = [{'name': x['name'], 'defined_at': '%s:%d' % (x['file'], x['line'])} for x in models]
    out['jobs'] = [j for j in repo.jobs() if j['file'] in files]
    if repo.is_git:
        out['decisions'] = githist.decisions(repo.root, m, limit=6)['decisions']
    return out


def cmd_deps(repo, a):
    f = rel(repo, a.file)
    return {'file': f, 'imports': [{'line': e['line'], 'target': e['dst'] or ('pkg:' + e['external'] if e['external'] else 'unresolved:' + e['spec']),
                                    'names': e['names'], 'kind': e['kind']} for e in sorted(repo.out_edges.get(f, []), key=lambda e: e['line'])]}


def cmd_dependents(repo, a):
    f = rel(repo, a.file)
    direct = repo.direct_dependents(f)
    dist = repo.transitive_dependents(f, a.depth)
    return {'file': f, 'direct': [dict(v, via=v['via']) for v in sorted(direct.values(), key=lambda v: v['file'])],
            'transitive': [{'file': x, 'depth': d} for x, d in sorted(dist.items(), key=lambda kv: (kv[1], kv[0])) if d > 1]}


def cmd_impact(repo, a):
    f = rel(repo, a.file)
    direct = repo.direct_dependents(f)
    dist = repo.transitive_dependents(f, 5)
    trans = [x for x, d in dist.items() if d > 1]
    affected = set(dist) | {f}
    routes = [r for r in repo.routes if r['file'] in affected]
    tests = sorted(x for x in dist if is_test(x))
    linked = set(dist) | {e['dst'] for e in repo.out_edges.get(f, []) if e['dst']}
    co, owners, hidden = {'partners': []}, [], []
    if repo.is_git:
        co = githist.cochange(repo.root, f)
        commits = githist.log(repo.root)
        owners = githist.owners_of(commits, githist.aliases(commits, f), githist.authors_index(commits))
        hidden = [p for p in co['partners'] if p['coupling'] >= 0.5 and p['file'] not in linked]
    stem = posixpath.splitext(posixpath.basename(f))[0]
    needle = stem if stem not in ('index', 'main', '__init__', 'mod', 'utils', 'types') else posixpath.splitext(f)[0]
    noncode = []
    if repo.is_git:
        for line in codemap.git(repo.root, 'grep', '--untracked', '-n', '-I', '-F', '-e', needle, '--', '.').splitlines()[:400]:
            path = line.split(':', 1)[0]
            if path != f and path not in repo.codeset:
                noncode.append(':'.join(line.split(':', 2)[:2]))
    features = Counter(module_of(x) for x in dist if not is_test(x))
    score, tier = impact_score(len(direct), len(trans), len(routes), bool(tests), len(hidden))
    reasons = ['%d direct importer%s' % (len(direct), '' if len(direct) == 1 else 's'),
               '%d transitive' % len(trans), '%d route%s' % (len(routes), '' if len(routes) == 1 else 's')]
    if hidden:
        reasons.append('%d hidden coupling%s' % (len(hidden), '' if len(hidden) == 1 else 's'))
    reasons.append('covered by %d test file%s' % (len(tests), '' if len(tests) == 1 else 's') if tests else 'no tests import it')
    if f in repo.entry_files:
        reasons.append('it is an entry file (%s)' % repo.entry_files[f])
    if a.delete:
        rec = ('Do not delete without replacing these dependencies: update the %d direct importers first, then re-run impact.' % len(direct)
               if tier == 'HIGH' else 'Deleting breaks %d file(s); migrate them first.' % len(direct) if direct else
               'No code imports this file. Check the non-code references and any dynamic loading before deleting.')
    else:
        rec = ('Keep its exports backward-compatible or update every importer in the same change; run %s.' % (', '.join(tests) or 'the full test suite')
               if direct else 'Changes here are local; nothing imports this file.')
    return {'file': f, 'tier': tier, 'score': score, 'reasons': reasons, 'action': 'delete' if a.delete else 'modify',
            'direct': [{'file': v['file'], 'line': v['line'], 'uses': v['names'], 'via': v['via']} for v in sorted(direct.values(), key=lambda v: v['file'])],
            'transitive': sorted(trans), 'features': [m for m, _ in features.most_common()],
            'routes': [{'route': '%s %s' % (r['method'], r['path']), 'at': '%s:%d' % (r['file'], r['line']),
                        'callers': ['%s:%d' % (c['file'], c['line']) for c in r['callers']]} for r in routes],
            'tests': tests, 'cochange': co['partners'][:8], 'hidden_coupling': hidden, 'noncode_refs': noncode[:20],
            'owners': owners[:5], 'recommendation': rec}


def cmd_routes(repo, a):
    if a.missing_guard:
        res = repo.missing_guard(a.missing_guard)
        res['flagged'] = [{'route': '%s %s' % (r['method'], r['path']), 'at': '%s:%d' % (r['file'], r['line']), 'handler': r['handler'],
                           'guards': r['guards'], 'reaches_protected_data': r['shared_data']} for r in res['flagged']]
        res['global_guards'] = repo.global_guards
        res['middleware'] = repo.middleware
        return res
    out = []
    for r in repo.routes:
        row = {'route': '%s %s' % (r['method'], r['path']), 'at': '%s:%d' % (r['file'], r['line']), 'handler': r['handler'],
               'guards': r['guards'], 'framework': r['framework'],
               'callers': ['%s:%d%s' % (c['file'], c['line'], '' if c['match'] == 'exact' else ' (suffix match)') for c in r['callers']]}
        if a.reach:
            reach = repo.route_reach(r)
            row['db'], row['external'] = reach['db'], reach['external']
        out.append(row)
    return {'routes': out, 'global_guards': repo.global_guards, 'middleware': repo.middleware}


def cmd_sinks(repo, a):
    paths = [p.rstrip('/') for p in (a.paths or [])]
    files = [f for f in repo.code if not paths or any(f == p or f.startswith(p + '/') for p in paths)]
    rows = [s for f in files for s in repo.sinks_in(f)]
    summary = Counter((s['kind'], s['target']) for s in rows)
    return {'summary': [{'kind': k, 'target': t, 'calls': n} for (k, t), n in summary.most_common()],
            'sinks': [{'kind': s['kind'], 'target': s['target'], 'at': '%s:%d' % (s['file'], s['line']), 'code': s['code']} for s in rows]}


def cmd_env(repo, a):
    return {'env': repo.env()}


def cmd_models(repo, a):
    return repo.models()


def cmd_jobs(repo, a):
    return {'jobs': repo.jobs()}


def cmd_timeline(repo, a):
    need_git(repo)
    return githist.timeline(repo.root, repo, a.by)


def cmd_decisions(repo, a):
    need_git(repo)
    path = a.path.rstrip('/') if a.path else None
    return githist.decisions(repo.root, path, a.limit)


def cmd_people(repo, a):
    need_git(repo)
    return githist.people(repo.root, repo, a.inactive_days)


def cmd_orphans(repo, a):
    need_git(repo)
    return githist.orphans(repo.root, repo, a.inactive_days)


def cmd_why(repo, a):
    need_git(repo)
    target, _, spec = a.target.partition(':') if ':' in a.target and not os.path.exists(os.path.join(repo.root, a.target)) else (a.target, '', '')
    f = rel(repo, target)
    return githist.why(repo.root, repo, f, spec or None, a.prs)


def cmd_blame(repo, a):
    need_git(repo)
    f = rel(repo, a.file)
    m = re.match(r'^(\d+)(?:-(\d+))?$', a.lines)
    if not m:
        raise UsageError('lines must look like 40-60')
    return githist.blame(repo.root, f, int(m.group(1)), int(m.group(2) or m.group(1)))


def cmd_cochange(repo, a):
    need_git(repo)
    f = rel(repo, a.file)
    res = githist.cochange(repo.root, f)
    linked = set(repo.transitive_dependents(f, 5)) | {e['dst'] for e in repo.out_edges.get(f, []) if e['dst']}
    for p in res['partners']:
        p['import_link'] = p['file'] in linked
    return res


def cmd_changed_since(repo, a):
    need_git(repo)
    return githist.changed_since(repo.root, repo, a.ref)


def cmd_unused(repo, a):
    res = repo.unused()
    if repo.is_git:
        last = githist.last_commits(repo.root)
        for row in res['files'] + res['exports']:
            c = last.get(row['file'])
            if c:
                row['last_change'] = '%s by %s' % (githist.day(c['ts']), c['author'])
    return res


def cmd_dupes(repo, a):
    return {'min_tokens': a.min_tokens, 'duplicates': repo.dupes(a.min_tokens)}


def cmd_verify(repo, a):
    ce = (lambda r: githist.commit_exists(repo.root, r)) if repo.is_git else (lambda r: False)
    pe = (lambda r: githist.pr_exists(repo.root, r)) if repo.is_git else (lambda r: False)
    if a.docs:
        res = repo.verify_docs(a.docs, ce, pe)
        res['ok'] = not res['problems']
        return res
    if not a.refs:
        raise UsageError('give refs (path:12, path:12-30, a commit sha, #42) or --docs DIR')
    rows = repo.verify_refs(a.refs, ce, pe)
    return {'ok': all(r['ok'] for r in rows), 'refs': rows}


# ------------------------------------------------------------------ rendering

def fmt(v, width=70):
    if isinstance(v, bool):
        return 'yes' if v else 'no'
    if isinstance(v, list):
        v = ', '.join(fmt(x, 1000) for x in v) if v else '-'
    elif isinstance(v, dict):
        v = ', '.join('%s=%s' % (k, fmt(x, 1000)) for k, x in v.items())
    v = '' if v is None else str(v)
    return v if len(v) <= width else v[:width - 1] + '…'


def table(rows, limit, indent='  '):
    if not rows:
        return indent + '(none)'
    shown = rows[:limit]
    cols = []
    for r in shown:
        for k in r:
            if k not in cols:
                cols.append(k)
    cells = [[fmt(r.get(c)) for c in cols] for r in shown]
    widths = [min(max([len(c)] + [len(row[i]) for row in cells]), 70) for i, c in enumerate(cols)]
    lines = [indent + '  '.join(c.ljust(widths[i]) for i, c in enumerate(cols)).rstrip()]
    lines += [indent + '  '.join(row[i].ljust(widths[i]) for i in range(len(cols))).rstrip() for row in cells]
    if len(rows) > limit:
        lines.append(indent + '… %d more (use --limit or --json)' % (len(rows) - limit))
    return '\n'.join(lines)


def render(obj, limit, indent=''):
    lines = []
    for k, v in obj.items():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            lines.append('%s%s (%d):' % (indent, k, len(v)))
            lines.append(table(v, limit, indent + '  '))
        elif isinstance(v, list) and v and len(v) > 6:
            lines.append('%s%s (%d):' % (indent, k, len(v)))
            lines += ['%s  %s' % (indent, fmt(x, 160)) for x in v[:limit]]
            if len(v) > limit:
                lines.append('%s  … %d more' % (indent, len(v) - limit))
        elif isinstance(v, dict) and v and any(isinstance(x, (dict, list)) for x in v.values()):
            lines.append('%s%s:' % (indent, k))
            lines.append(render(v, limit, indent + '  '))
        else:
            lines.append('%s%s: %s' % (indent, k, fmt(v, 200)))
    return '\n'.join(lines)


def render_impact(r, limit):
    out = ['%s: %s IMPACT (score %s, if you %s it)' % (r['file'], r['tier'], r['score'], r['action']),
           'Why: ' + ' · '.join(r['reasons']), '']
    out.append('Imported by (%d):' % len(r['direct']))
    for d in r['direct'][:limit]:
        via = ' (via %s)' % d['via'] if d['via'] else ''
        out.append('  %-60s uses %s%s' % ('%s:%d' % (d['file'], d['line']), ', '.join(d['uses']) or '(side effect)', via))
    if r['transitive']:
        out.append('Transitive dependents (%d): %s' % (len(r['transitive']), ', '.join(r['transitive'][:limit])))
    if r['features']:
        out.append('Modules affected: ' + ', '.join(r['features']))
    out.append('Routes that depend on it (%d):' % len(r['routes']))
    for x in r['routes'][:limit]:
        callers = ('  <- called from ' + ', '.join(x['callers'])) if x['callers'] else ''
        out.append('  %-32s %s%s' % (x['route'], x['at'], callers))
    out.append('Tests: ' + (', '.join(r['tests']) or 'none import it'))
    if r['cochange']:
        out.append('Changes together with:')
        hidden = {h['file'] for h in r['hidden_coupling']}
        for p in r['cochange']:
            out.append('  %-60s %d commits (%d%%)%s' % (p['file'], p['together'], p['coupling'] * 100, '  HIDDEN: no import link' if p['file'] in hidden else ''))
    out.append('Non-code references: ' + (', '.join(r['noncode_refs']) or 'none'))
    if r['owners']:
        out.append('Owners: ' + ', '.join('%s (%d commit%s, last touched it %s%s)' % (
            o['name'], o['commits'], '' if o['commits'] == 1 else 's', o['last'], '' if o['active'] else ', now inactive') for o in r['owners']))
    out += ['', 'Recommendation: ' + r['recommendation']]
    return '\n'.join(out)


def render_why(r, limit):
    if 'error' in r:
        return 'error: ' + r['error']
    head = '%s%s%s' % (r['file'], (':' + r['symbol']) if r.get('symbol') else '', (' (lines %s)' % r['lines']) if r.get('lines') else '')
    out = [head]
    if r.get('origin'):
        o = r['origin']
        out.append('Introduced: %s %s by %s: %s' % (o['sha'], o['date'], o['author'], o['subject']))
    if r.get('file_created'):
        c = r['file_created']
        out.append('File created: %s %s by %s: %s' % (c['sha'], c['date'], c['author'], c['subject']))
    out.append('History of these lines (newest first, %d commits):' % len(r['commits']))
    for c in r['commits'][:limit]:
        out.append('  %s %s %s: %s%s' % (c['sha'], c['date'], c['author'], c['subject'], ('  ' + ' '.join(c['refs'])) if c.get('refs') else ''))
        if c.get('body'):
            out += ['      ' + ln for ln in c['body'].splitlines()[:6] if ln.strip()]
    for p in r.get('prs') or []:
        out.append('PR #%s: %s' % (p.get('number'), p.get('title') or p.get('error')))
        if p.get('body'):
            out += ['      ' + ln for ln in p['body'].splitlines()[:12] if ln.strip()]
    return '\n'.join(out)


def render_verify(r, limit):
    if 'problems' in r:
        head = 'checked %d references and links: %s' % (r['checked'], 'all OK' if r['ok'] else '%d broken' % len(r['problems']))
        return head + ('\n' + table(r['problems'], limit) if r['problems'] else '')
    out = []
    for x in r['refs']:
        out.append('%s %s%s' % ('OK  ' if x['ok'] else 'FAIL', x['ref'], '' if x['ok'] else '  (' + x.get('reason', '') + ')'))
        out += ['      ' + ln for ln in x.get('lines', [])]
    return '\n'.join(out)


RENDERERS = {'impact': render_impact, 'why': render_why, 'verify': render_verify}


# ------------------------------------------------------------------ cli

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--repo', help='project root (default: git toplevel of the current directory)')
    common.add_argument('--json', action='store_true', help='structured JSON output')
    common.add_argument('--limit', type=int, default=40, help='max rows per list in text output (default 40)')
    p = argparse.ArgumentParser(prog='arch.py', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', metavar='command')
    sub.required = True

    def add(name, fn, help_):
        sp = sub.add_parser(name, parents=[common], help=help_, description=help_)
        sp.set_defaults(fn=fn)
        return sp

    add('overview', cmd_overview, 'stack, manifests, entry files, tests, infra, contributors, hot files')
    sp = add('map', cmd_map, 'modules, how they connect, hubs; --module M for one module in depth')
    sp.add_argument('--module', help='module path or name, e.g. api/src/invoice or invoice')
    sp = add('deps', cmd_deps, 'what a file imports')
    sp.add_argument('file')
    sp = add('dependents', cmd_dependents, 'who imports a file (direct, via barrels, transitive)')
    sp.add_argument('file')
    sp.add_argument('--depth', type=int, default=5)
    sp = add('impact', cmd_impact, 'what breaks if a file is modified or deleted')
    sp.add_argument('file')
    sp.add_argument('--delete', action='store_true', help='assess deleting instead of modifying')
    sp = add('routes', cmd_routes, 'HTTP routes, guards, frontend callers; --reach adds data touched')
    sp.add_argument('--reach', action='store_true', help='add DB tables and external services reachable from each handler file')
    sp.add_argument('--missing-guard', metavar='NAME', help='routes that reach data protected by NAME but do not use it')
    sp = add('sinks', cmd_sinks, 'DB and external API calls (optionally limited to files/dirs)')
    sp.add_argument('paths', nargs='*')
    add('env', cmd_env, 'environment variables: where read, where declared')
    add('models', cmd_models, 'data models/entities, migrations, and which files use each')
    add('jobs', cmd_jobs, 'cron jobs, queues, scheduled and background tasks')
    sp = add('timeline', cmd_timeline, 'project history by period, milestones (when each module appeared)')
    sp.add_argument('--by', choices=['quarter', 'month'], default='quarter')
    sp = add('decisions', cmd_decisions, 'commits that carry the why: rationale, fixes, reverts, breaking changes')
    sp.add_argument('path', nargs='?', help='limit to a file or directory')
    sp = add('people', cmd_people, 'contributors: when active, what they worked on')
    sp.add_argument('--inactive-days', type=int, default=180)
    sp = add('orphans', cmd_orphans, 'knowledge at risk: areas whose main author is no longer active')
    sp.add_argument('--inactive-days', type=int, default=180)
    sp = add('why', cmd_why, 'history of a file, symbol (file:name) or line range (file:10-20)')
    sp.add_argument('target', help='path, path:symbol, path:Class.method or path:start-end')
    sp.add_argument('--prs', action='store_true', help='also fetch PR descriptions with gh (network)')
    sp = add('blame', cmd_blame, 'which commits wrote these lines, with their messages')
    sp.add_argument('file')
    sp.add_argument('lines', help='start-end, e.g. 40-60')
    sp = add('cochange', cmd_cochange, 'files that change together with this one')
    sp.add_argument('file')
    sp = add('changed-since', cmd_changed_since, 'what changed since a commit: files, modules, routes affected')
    sp.add_argument('ref')
    add('unused', cmd_unused, 'potentially unused files and exports')
    sp = add('dupes', cmd_dupes, 'duplicated code blocks')
    sp.add_argument('--min-tokens', type=int, default=50)
    sp = add('verify', cmd_verify, 'check citations: path:12, path:12-30, commit sha, #42; or every ref/link in --docs DIR')
    sp.add_argument('refs', nargs='*')
    sp.add_argument('--docs', metavar='DIR')
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        repo = Repo(find_root(a.repo))
        result = a.fn(repo, a)
    except UsageError as e:
        print('error: %s' % e, file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(result, indent=1, default=str))
    else:
        print(RENDERERS.get(a.cmd, render)(result, a.limit))
    if a.cmd == 'verify' and not result.get('ok'):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
