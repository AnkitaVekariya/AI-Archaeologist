"""Git history for code archaeology: one-pass log, timeline, decisions, people, knowledge at risk,
why (git log -L), blame, co-change, changed-since. Stdlib only; reads git, writes nothing."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter, defaultdict

from codemap import CODE_EXT, git, module_of, skip_balanced

FORMAT = '%x1e%H%x1f%aN%x1f%aE%x1f%at%x1f%P%x1f%s%x1f%b%x1d'
REF_RE = re.compile(r'(?<![\w&/])#(\d+)\b')
DAY = 86400
_LOG = {}


def day(ts):
    return time.strftime('%Y-%m-%d', time.gmtime(ts))


def parse_log(out, with_files=True):
    commits = []
    for chunk in out.split('\x1e')[1:]:
        head, _, rest = chunk.partition('\x1d')
        f = head.split('\x1f')
        if len(f) < 7:
            continue
        sha, name, email, ts, parents, subject, body = f[:7]
        files = []
        if with_files:
            for line in rest.strip('\n').splitlines():
                parts = line.split('\t')
                if len(parts) >= 2 and parts[0]:
                    st = parts[0][0]
                    files.append((st, parts[2], parts[1]) if st in 'RC' and len(parts) >= 3 else (st, parts[-1], None))
        commits.append({'sha': sha, 'author': name, 'email': email.lower(), 'ts': int(ts or 0), 'parents': parents.split(),
                        'subject': subject, 'body': body.strip(), 'files': files,
                        'refs': sorted(set(REF_RE.findall(subject + '\n' + body)), key=int)})
    return commits


def log(root):
    """All commits reachable from HEAD, newest first, with name-status file lists. Cached per process."""
    if root not in _LOG:
        _LOG[root] = parse_log(git(root, 'log', '-M', '--name-status', '--format=' + FORMAT))
        # ponytail: whole history in memory; add --max-count / --since if a 1M-commit repo ever needs it
    return _LOG[root]


def short(c, files=False):
    out = {'sha': c['sha'][:9], 'date': day(c['ts']), 'author': c['author'], 'subject': c['subject']}
    if c['refs']:
        out['refs'] = ['#' + r for r in c['refs']]
    if files:
        out['files'] = [p for _, p, _ in c['files']][:8] + (['… +%d more' % (len(c['files']) - 8)] if len(c['files']) > 8 else [])
    return out


def head_ts(commits):
    return max((c['ts'] for c in commits), default=0)


def aliases(commits, path):
    """path plus every older name it had (follows renames backwards)."""
    names = {path}
    for c in commits:
        for st, p, old in c['files']:
            if st == 'R' and p in names and old:
                names.add(old)
    return names


def commit_exists(root, ref):
    return bool(git(root, 'rev-parse', '--verify', '--quiet', ref + '^{commit}').strip())


def pr_exists(root, ref):
    n = ref.lstrip('#')
    return any(n in c['refs'] for c in log(root))


# ------------------------------------------------------------------ people / ownership

def authors_index(commits, inactive_days=180):
    """email -> {name, commits, first, last, active}"""
    by = {}
    for c in commits:
        p = by.setdefault(c['email'] or c['author'], {'names': Counter(), 'commits': 0, 'first': c['ts'], 'last': c['ts']})
        p['names'][c['author']] += 1
        p['commits'] += 1
        p['first'] = min(p['first'], c['ts'])
        p['last'] = max(p['last'], c['ts'])
    cutoff = head_ts(commits) - inactive_days * DAY
    for p in by.values():
        p['name'] = p['names'].most_common(1)[0][0]
        p['active'] = p['last'] >= cutoff
    return by


def people(root, repo, inactive_days=180):
    commits = log(root)
    idx = authors_index(commits, inactive_days)
    mods = defaultdict(Counter)
    for c in commits:
        for _, path, _ in c['files']:
            if path in repo.codeset:
                mods[c['email'] or c['author']][module_of(path)] += 1
    out = []
    for key, p in sorted(idx.items(), key=lambda kv: -kv[1]['commits']):
        out.append({'name': p['name'], 'commits': p['commits'], 'first': day(p['first']), 'last': day(p['last']),
                    'active': p['active'], 'top_modules': [m for m, _ in mods[key].most_common(4)]})
    return dict(idle_note(commits), inactive_after_days=inactive_days, people=out)


def owners_of(commits, paths, idx):
    cnt, last = Counter(), {}
    for c in commits:
        if any(p in paths for _, p, _ in c['files']):
            key = c['email'] or c['author']
            cnt[key] += 1
            last.setdefault(key, c['ts'])
    return [{'name': idx[k]['name'], 'commits': n, 'last': day(last[k]), 'active': idx[k]['active']} for k, n in cnt.most_common()]


def orphans(root, repo, inactive_days=180, min_share=0.4):
    """Areas whose dominant author is no longer active = knowledge at risk."""
    commits = log(root)
    idx = authors_index(commits, inactive_days)
    file_auth, mod_auth = defaultdict(Counter), defaultdict(Counter)
    for c in commits:
        key = c['email'] or c['author']
        for _, path, _ in c['files']:
            if path in repo.codeset:
                file_auth[path][key] += 1
                mod_auth[module_of(path)][key] += 1

    def rows(table, share_floor):
        out = []
        for area, cnt in table.items():
            total = sum(cnt.values())
            dom, n = cnt.most_common(1)[0]
            share = n / total
            if idx[dom]['active'] or share < share_floor:
                continue
            others = [idx[k]['name'] for k, _ in cnt.most_common() if k != dom and idx[k]['active']][:3]
            out.append({'area': area, 'main_author': idx[dom]['name'], 'share': round(share, 2), 'touches': total,
                        'author_last_commit': day(idx[dom]['last']), 'active_contributors': others or ['nobody active']})
        return sorted(out, key=lambda r: (-r['share'] * r['touches']))

    return dict(idle_note(commits), inactive_after_days=inactive_days,
                modules=rows(mod_auth, min_share), files=rows(file_auth, 0.5)[:30])


def idle_note(commits):
    """'Active' is measured against the newest commit, so a dormant repo still shows its last team as active."""
    h = head_ts(commits)
    idle = int((time.time() - h) // DAY) if h else 0
    out = {'head': day(h), 'repo_idle_days': idle}
    if idle > 180:
        out['note'] = 'no commits for %d days: "active" means active when the repo was last worked on' % idle
    return out


# ------------------------------------------------------------------ timeline / decisions

def timeline(root, repo, by='quarter'):
    commits = log(root)
    periods, first_seen, milestones = {}, {}, []
    for c in reversed(commits):
        t = time.gmtime(c['ts'])
        key = '%d-Q%d' % (t.tm_year, (t.tm_mon - 1) // 3 + 1) if by == 'quarter' else '%d-%02d' % (t.tm_year, t.tm_mon)
        p = periods.setdefault(key, {'period': key, 'commits': 0, 'authors': Counter(), 'modules': Counter(), 'cands': []})
        p['commits'] += 1
        p['authors'][c['author']] += 1
        for st, path, _ in c['files']:
            if os.path.splitext(path)[1] not in CODE_EXT:
                continue
            m = module_of(path)
            p['modules'][m] += 1
            if m not in first_seen and m in repo.modules:
                first_seen[m] = c
                milestones.append({'date': day(c['ts']), 'event': 'module %s created' % m, 'sha': c['sha'][:9],
                                   'author': c['author'], 'subject': c['subject']})
        p['cands'].append(c)
    out = []
    for key in sorted(periods):
        p = periods[key]
        notable = sorted(p['cands'], key=lambda c: (bool(c['refs']) or len(c['parents']) > 1, len(c['files'])), reverse=True)[:4]
        out.append({'period': key, 'commits': p['commits'],
                    'authors': ['%s (%d)' % a for a in p['authors'].most_common(4)],
                    'modules': [m for m, _ in p['modules'].most_common(4)],
                    'notable': ['%s %s' % (c['sha'][:9], c['subject']) for c in notable]})
    return {'first': day(commits[-1]['ts']) if commits else None, 'last': day(commits[0]['ts']) if commits else None,
            'commits': len(commits), 'periods': out, 'milestones': milestones}


RATIONALE = re.compile(r"\b(because|since|so that|in order to|to prevent|prevents?|prevented|avoids?|instead of|workaround|hack|"
                       r"temporar\w*|regression|incident|outage|root cause|compliance|security|vulnerab\w*|deprecat\w*|"
                       r"migrat\w*|breaking|rollback|race|duplicate\w*|double|retr(y|ies|ied)|idempoten\w*|must|never|"
                       r"do not|don't|reason|caused|stale|leak\w*|so\b)", re.I)


def classify(c):
    subj, text = c['subject'], c['subject'] + '\n' + c['body']
    score, kind = 0, None
    if subj.startswith('Revert "') or subj.lower().startswith('revert'):
        kind, score = 'revert', 3
    elif re.match(r'^(hot)?fix(\(.*?\))?!?:|^bugfix|^hotfix', subj, re.I):
        kind, score = 'fix', 2
    if 'BREAKING' in text or re.match(r'^\w+(\(.*?\))?!:', subj):
        kind, score = 'breaking', score + 3
    if re.search(r'\b(security|vulnerab|CVE-\d+|xss|csrf|injection)', text, re.I):
        kind, score = kind or 'security', score + 3
    if re.search(r'\bmigrat', subj, re.I):
        kind, score = kind or 'migration', score + 2
    hits = len(RATIONALE.findall(c['body'])) + len(RATIONALE.findall(subj))
    score += min(hits, 4) + (1 if len(c['body']) > 60 else 0)
    if not kind and hits:
        kind = 'decision'
    return kind, score


def decisions(root, path=None, limit=30):
    commits = log(root)
    by_subject = {c['subject']: c for c in commits}
    out = []
    for c in commits:
        if path and not any(p == path or p.startswith(path.rstrip('/') + '/') for _, p, _ in c['files']):
            continue
        kind, score = classify(c)
        if not kind or score < 2:
            continue
        row = dict(short(c, files=True), kind=kind, score=score)
        if c['body']:
            row['why'] = re.sub(r'\s+', ' ', c['body'])[:400]
        if kind == 'revert':
            m = re.search(r'This reverts commit ([0-9a-f]{7,40})', c['body'])
            orig = re.match(r'^Revert "(.*)"$', c['subject'])
            target = m.group(1)[:9] if m else (by_subject[orig.group(1)]['sha'][:9] if orig and orig.group(1) in by_subject else None)
            if target:
                row['reverts'] = target
        out.append(row)
    out.sort(key=lambda r: (r['score'], r['date']), reverse=True)
    return {'path': path or 'all', 'decisions': out[:limit], 'total': len(out)}


# ------------------------------------------------------------------ why / blame / cochange

def symbol_range(repo, path, name):
    """(start, end) lines of a function/class/method/const definition, or None."""
    s = repo.stripped(path)
    parts = name.split('.')
    target, cls = parts[-1], (parts[0] if len(parts) > 1 else None)
    lo, hi = 0, len(s)
    if repo.lang(path) == 'py':
        if cls:
            m = re.search(r'(?m)^([ \t]*)class\s+%s\b' % re.escape(cls), s)
            if m:
                lo = m.start()
        m = re.compile(r'(?m)^([ \t]*)(?:async\s+def|def|class)\s+%s\b' % re.escape(target)).search(s, lo)
        if not m:
            return None
        indent = len(m.group(1).expandtabs())
        lines = repo.text(path).splitlines()
        start = repo.line_of(path, m.start())
        end = start
        for i in range(start, len(lines)):
            ln = lines[i]
            if not ln.strip() or ln.lstrip().startswith('#'):
                continue
            if len(ln) - len(ln.lstrip()) <= indent and not ln.lstrip().startswith((')', ']', '}')):
                break
            end = i + 1
        return start, end
    if cls:
        m = re.search(r'\bclass\s+%s\b' % re.escape(cls), s)
        if m:
            brace = s.find('{', m.end())
            if brace >= 0:
                lo, hi = m.start(), skip_balanced(s, brace, '{', '}')
    t = re.escape(target)
    patterns = [r'\bfunction\*?\s+%s\s*[<(]' % t, r'\b(?:class|interface|enum|type)\s+%s\b' % t,
                r'\b(?:const|let|var)\s+%s\s*[=:]' % t,
                r'(?m)^[ \t]*(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)*%s\s*(?:<[^>]*>)?\s*\(' % t,
                r'\b%s\s*:\s*(?:async\s*)?(?:function\b|\()' % t]
    for pat in patterns:
        m = re.compile(pat).search(s, lo, hi)
        if not m:
            continue
        i, end = m.end(), None
        while i < hi:
            ch = s[i]
            if ch == '(':
                i = skip_balanced(s, i)
                continue
            if ch == '{':
                end = skip_balanced(s, i, '{', '}')
                break
            if ch == ';':
                end = i + 1
                break
            i += 1
        start_off = m.start() + (len(m.group(0)) - len(m.group(0).lstrip()))
        return repo.line_of(path, start_off), repo.line_of(path, max((end or i) - 1, start_off))
    return None


def why(root, repo, path, spec=None, prs=False):
    rng, symbol = None, None
    if spec and re.match(r'^\d+(-\d+)?$', spec):
        a, _, b = spec.partition('-')
        rng = (int(a), int(b or a))
    elif spec:
        symbol = spec
        rng = symbol_range(repo, path, spec)
        if not rng:
            return {'error': 'symbol %r not found in %s' % (spec, path)}
    out = {'file': path, 'symbol': symbol}
    if rng:
        out['lines'] = '%d-%d' % rng
        raw = git(root, 'log', '-L', '%d,%d:%s' % (rng[0], rng[1], path), '-s', '--format=' + FORMAT)
    else:
        raw = git(root, 'log', '--follow', '-M', '--format=' + FORMAT, '--', path)
    commits = parse_log(raw, with_files=False)
    full = {c['sha']: c for c in log(root)}
    rows = []
    for c in commits:
        r = short(c)
        body = (full.get(c['sha']) or c)['body']
        if body:
            r['body'] = body[:800]
        rows.append(r)
    out['commits'] = rows
    out['origin'] = rows[-1] if rows else None
    created = parse_log(git(root, 'log', '--follow', '--diff-filter=A', '--format=' + FORMAT, '--', path), with_files=False)
    out['file_created'] = short(created[-1]) if created else None
    if prs:
        refs = []
        for c in commits:
            refs += [r for r in c['refs'] if r not in refs]
        out['prs'] = [fetch_pr(root, r) or {'number': int(r), 'error': 'not available (gh missing, not authenticated, or not a GitHub PR)'}
                      for r in refs[:6]]
    return out


def fetch_pr(root, number):
    if not shutil.which('gh'):
        return None
    try:
        r = subprocess.run(['gh', 'pr', 'view', str(number), '--json', 'number,title,body,url,mergedAt,author'],
                           cwd=root, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    data['body'] = (data.get('body') or '')[:2000]
    return data


def blame(root, path, start, end):
    args = ['blame', '-w', '-C', '-C', '--line-porcelain', '-L', '%d,%d' % (start, end)]
    if os.path.exists(os.path.join(root, '.git-blame-ignore-revs')):
        args += ['--ignore-revs-file', '.git-blame-ignore-revs']
    out = git(root, *args, '--', path)
    full = {c['sha']: c for c in log(root)}
    groups, cur = {}, None
    for line in out.splitlines():
        m = re.match(r'^([0-9a-f]{40}) \d+ (\d+)', line)
        if m:
            cur = groups.setdefault(m.group(1), {'sha': m.group(1)[:9], 'lines': [], 'author': '', 'date': '', 'subject': ''})
            cur['lines'].append(int(m.group(2)))
        elif cur is not None and line.startswith('author '):
            cur['author'] = line[7:]
        elif cur is not None and line.startswith('author-time '):
            cur['date'] = day(int(line[12:]))
        elif cur is not None and line.startswith('summary '):
            cur['subject'] = line[8:]
    rows = []
    for sha, g in groups.items():
        c = full.get(sha)
        if c and c['body']:
            g['body'] = c['body'][:600]
        if c and c['refs']:
            g['refs'] = ['#' + r for r in c['refs']]
        g['lines'] = _ranges(g['lines'])
        rows.append(g)
    rows.sort(key=lambda g: g['date'])
    return {'file': path, 'lines': '%d-%d' % (start, end), 'commits': rows}


def _ranges(nums):
    nums, out = sorted(nums), []
    for n in nums:
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return ', '.join('%d' % a if a == b else '%d-%d' % (a, b) for a, b in out)


def cochange(root, path, max_files=30):
    commits = log(root)
    names = aliases(commits, path)
    touching = [c for c in commits if any(p in names for _, p, _ in c['files'])]
    cnt = Counter()
    for c in touching:
        if len(c['files']) > max_files:
            continue
        for _, p, _ in c['files']:
            if p not in names:
                cnt[p] += 1
    n = len(touching) or 1
    partners = [{'file': p, 'together': k, 'coupling': round(k / n, 2)} for p, k in cnt.most_common() if k >= 2]
    return {'file': path, 'commits': len(touching), 'partners': partners[:20]}


def last_commits(root):
    """path -> newest commit touching it"""
    res = {}
    for c in log(root):
        for _, p, _ in c['files']:
            res.setdefault(p, c)
    return res


def hot_files(root, repo, days=365, top=12):
    commits = log(root)
    cutoff = head_ts(commits) - days * DAY
    cnt = Counter()
    for c in commits:
        if c['ts'] < cutoff:
            break
        for _, p, _ in c['files']:
            if p in repo.codeset:
                cnt[p] += 1
    return [{'file': p, 'commits': n} for p, n in cnt.most_common(top)]


def changed_since(root, repo, ref):
    if not commit_exists(root, ref):
        return {'error': 'commit %s not found' % ref}
    files = []
    for line in git(root, 'diff', '--name-status', '-M', ref, 'HEAD').splitlines():
        parts = line.split('\t')
        if len(parts) >= 2:
            files.append({'status': parts[0][0], 'file': parts[-1]})
    for line in git(root, 'status', '--porcelain').splitlines():
        if len(line) > 3:
            files.append({'status': 'uncommitted ' + line[:2].strip(), 'file': line[3:].split(' -> ')[-1]})
    changed = {f['file'] for f in files}
    mods = Counter(module_of(f) for f in changed if os.path.splitext(f)[1] in CODE_EXT)
    routes = []
    for r in repo.routes:
        touched = sorted(set(repo.forward(r['file'], 3)) & changed)
        if touched:
            routes.append({'method': r['method'], 'path': r['path'], 'file': '%s:%d' % (r['file'], r['line']), 'via': touched[:4]})
    new = parse_log(git(root, 'log', '%s..HEAD' % ref, '--format=' + FORMAT), with_files=False)
    return {'since': ref, 'files': files, 'modules': [{'module': m, 'files': n} for m, n in mods.most_common()],
            'routes_affected': routes, 'new_commits': [short(c) for c in new]}
