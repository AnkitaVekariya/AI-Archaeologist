"""Static code map for TS/JS and Python repos.

Files, imports/exports (with tsconfig paths, barrels, workspaces), modules, routes + frontend callers,
DB/external sinks, env vars, models, jobs, unused code, duplicates, and citation verification.
Stdlib only, Python 3.8+. Everything is recomputed per call; nothing is written to disk.
"""
from __future__ import annotations

import bisect
import json
import math
import os
import posixpath
import re
import subprocess
from collections import Counter, defaultdict
from functools import cached_property

CODE_EXT = {'.ts': 'ts', '.tsx': 'ts', '.mts': 'ts', '.cts': 'ts',
            '.js': 'js', '.jsx': 'js', '.mjs': 'js', '.cjs': 'js', '.py': 'py'}
JS_RESOLVE_EXT = ['.ts', '.tsx', '.d.ts', '.js', '.jsx', '.mjs', '.cjs', '.mts', '.cts']
SKIP_DIRS = {'node_modules', 'dist', 'build', 'out', '.next', '.nuxt', 'coverage', 'vendor', '.venv', 'venv',
             'env', '__pycache__', '.git', 'target', '.turbo', '.cache', 'site-packages', 'bower_components',
             '.svelte-kit', '.output', 'storybook-static'}
LOCKFILES = {'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'poetry.lock', 'Pipfile.lock', 'bun.lockb',
             'composer.lock', 'Gemfile.lock', 'Cargo.lock', 'uv.lock'}
MAX_BYTES = 500_000
MODULE_MARKERS = {'src', 'app', 'lib', 'pkg', 'internal', 'server', 'client', 'apps', 'packages', 'services', 'modules'}


def git(root, *args, check=False):
    try:
        r = subprocess.run(['git', '-C', root, *args], capture_output=True, text=True, errors='replace')
    except FileNotFoundError:
        return ''
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or 'git %s failed' % args[0])
    return r.stdout if r.returncode == 0 else ''


def find_root(start=None):
    start = os.path.abspath(start or os.getcwd())
    top = git(start, 'rev-parse', '--show-toplevel').strip()
    return top or start


# ------------------------------------------------------------------ text helpers

RE_JS_LEX = re.compile(r"""//[^\n]*|/\*.*?(?:\*/|\Z)|'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`""", re.S)
RE_PY_LEX = re.compile('|'.join([
    r'#[^\n]*',
    r'"""(?:\\.|[^\\])*?"""',
    r"'''(?:\\.|[^\\])*?'''",
    r"'(?:\\.|[^'\\\n])*'",
    r'"(?:\\.|[^"\\\n])*"',
]), re.S)


def _blank(m):
    t = m.group(0)
    if t[0] in '/#':
        return re.sub(r'[^\n]', ' ', t)
    return t


def strip_js(text):
    """Blank out // and /* */ comments; keep strings and newlines so offsets and line numbers stay valid."""
    # ponytail: regex literals like /'/ can confuse this; harmless for import/route scanning in practice
    return RE_JS_LEX.sub(_blank, text)


def strip_py(text):
    return RE_PY_LEX.sub(_blank, text)


def strip_jsonc(text):
    return re.sub(r',(\s*[}\]])', r'\1', strip_js(text))


def skip_balanced(text, i, open_ch='(', close_ch=')'):
    """text[i] == open_ch; return index just past the matching close_ch (string-aware)."""
    depth, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in '\'"`':
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == '\\' else 1
            i = j + 1
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def split_top_level(s):
    parts, depth, cur, i = [], 0, [], 0
    while i < len(s):
        c = s[i]
        if c in '\'"`':
            j = i + 1
            while j < len(s) and s[j] != c:
                j += 2 if s[j] == '\\' else 1
            cur.append(s[i:j + 1])
            i = j + 1
            continue
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        if c == ',' and depth == 0:
            parts.append(''.join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    if ''.join(cur).strip():
        parts.append(''.join(cur).strip())
    return parts


class Lines:
    def __init__(self, text):
        self.starts = [0] + [m.end() for m in re.finditer('\n', text)]

    def line(self, offset):
        return bisect.bisect_right(self.starts, offset)


def norm_url(path):
    path = re.sub(r'^https?://[^/]+', '', path.strip())
    path = path.split('?')[0].split('#')[0]
    path = re.sub(r'\$\{[^}]*\}', ':param', path)
    path = re.sub(r'\{[^}/]*\}', ':param', path)
    path = re.sub(r'<(?:\w+:)?\w+>', ':param', path)
    path = re.sub(r'\[\.\.\.\w+\]', '*', path)
    path = re.sub(r'\[(\w+)\]', r':\1', path)
    path = re.sub(r'/+', '/', '/' + path.lstrip('/'))
    return path.rstrip('/') or '/'


def join_url(*parts):
    return norm_url('/'.join(p.strip('/') for p in parts if p and p.strip('/')))


def module_of(path):
    """Directory-cluster heuristic: the directory just under the last source-root marker."""
    dirs = path.split('/')[:-1]
    if not dirs:
        return '.'
    idx = max((i for i, d in enumerate(dirs) if d in MODULE_MARKERS), default=None)
    if idx is None:
        return '/'.join(dirs[:2])
    # ponytail: directory heuristic; cluster by import density only if modules look wrong on real repos
    return '/'.join(dirs[:idx + 2])


TEST_RE = re.compile(r'(^|/)(tests?|__tests__|spec|e2e|cypress|playwright)/|\.(test|spec|e2e)\.\w+$|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$')
CONFIG_RE = re.compile(r'(^|/)[^/]*\.config\.\w+$|(^|/)(vite|webpack|rollup|jest|babel|eslint|prettier|tailwind|postcss|next|nuxt|svelte|tsup|vitest|playwright|cypress|karma|gulpfile|gruntfile)[^/]*\.\w+$|(^|/)(setup|manage|wsgi|asgi|settings|conftest|noxfile|fabfile)\.py$|\.d\.ts$')


def is_test(path):
    return bool(TEST_RE.search(path))


def is_config(path):
    return bool(CONFIG_RE.search(path))


# ------------------------------------------------------------------ regexes

RE_IMPORT_FROM = re.compile(r'''\bimport\s+(type\s+)?([\w$*{}\s,]+?)\s*from\s*(['"])([^'"\n]+)\3''')
RE_IMPORT_BARE = re.compile(r'''(?<![\w$.])import\s*(['"])([^'"\n]+)\1''')
RE_EXPORT_FROM = re.compile(r'''\bexport\s+(type\s+)?(\*(?:\s+as\s+[\w$]+)?|\{[\w$\s,]*\})\s*from\s*(['"])([^'"\n]+)\3''')
RE_REQUIRE = re.compile(r'''(?<![\w$.])require\s*\(\s*(['"])([^'"\n]+)\1\s*\)''')
RE_DYN_IMPORT = re.compile(r'''(?<![\w$.])import\s*\(\s*(['"])([^'"\n]+)\1\s*\)''')
RE_REQUIRE_BIND = re.compile(r'''(?:const|let|var)\s+(\{[^}]*\}|[\w$]+)\s*=\s*require\s*\(\s*(['"])([^'"\n]+)\2''')

RE_EXPORT_DECL = re.compile(r'^[ \t]*export\s+(?:declare\s+)?(default\s+)?(?:async\s+)?(?:abstract\s+)?(function\*?|class|const|let|var|interface|type|enum|namespace)\s+([\w$]+)', re.M)
RE_EXPORT_LIST = re.compile(r'^[ \t]*export\s*\{([^}]*)\}\s*(?!\s*from)', re.M)
RE_EXPORT_DEFAULT = re.compile(r'^[ \t]*export\s+default\s+(?!function|class|async|abstract)', re.M)
RE_CJS_EXPORT = re.compile(r'(?<![\w$.])(?:module\.)?exports\.([\w$]+)\s*=|module\.exports\s*=')

RE_PY_FROM = re.compile(r'^[ \t]*from\s+(\.*[\w.]*)\s+import\s+(\([^)]*\)|[^\n]+)', re.M)
RE_PY_IMPORT = re.compile(r'^[ \t]*import\s+([\w.]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w.]+(?:\s+as\s+\w+)?)*)', re.M)
RE_PY_DEF = re.compile(r'^(?:async\s+def|def|class)\s+([A-Za-z]\w*)|^([A-Za-z]\w*)\s*(?::[^=\n]*)?=(?!=)', re.M)

HTTP_METHODS = 'get|post|put|patch|delete|all|options|head'
RE_NEST_CTRL = re.compile(r'@Controller\(')
RE_DECORATOR = re.compile(r'@([\w$.]+)')
RE_EXPRESS_ROUTE = re.compile(r'(?<![\w$.])([\w$]+)\.(' + HTTP_METHODS + r')\(\s*([\'"`])(/[^\'"`]*|\*)\3\s*,')
RE_EXPRESS_USE = re.compile(r'(?<![\w$.])([\w$]+)\.use\(\s*([\'"`])(/[^\'"`]*)\2\s*,\s*([^)]*)\)')
RE_EXPRESS_FILE = re.compile(r'''from\s+['"](express|koa-router|@koa/router|fastify|hono)['"]|require\(\s*['"](express|koa-router|@koa/router|fastify)['"]\s*\)|\bRouter\(\)|\bexpress\(\)|\bfastify\(|new Hono\(''')
RE_GLOBAL_PREFIX = re.compile(r'''setGlobalPrefix\(\s*(['"`])([^'"`]*)\1''')
RE_GLOBAL_GUARDS = re.compile(r'useGlobalGuards\(([^;]*)\)|provide\s*:\s*APP_GUARD\s*,\s*useClass\s*:\s*([\w$]+)')
RE_NEXT_ROUTE_FILE = re.compile(r'(^|/)app/(.*/)?route\.(ts|js|tsx|jsx|mjs)$')
RE_NEXT_PAGES_API = re.compile(r'(^|/)pages/api/(.*)\.(ts|js|tsx|jsx|mjs)$')
RE_NEXT_METHOD = re.compile(r'export\s+(?:async\s+)?(?:function|const)\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b')

RE_PY_ROUTER = re.compile(r'''^[ \t]*(\w+)\s*=\s*(?:\w+\.)?(APIRouter|Blueprint)\(([^)]*)\)''', re.M)
RE_PY_ROUTE = re.compile(r'''^[ \t]*@(\w+)\.(get|post|put|patch|delete|api_route|route|websocket)\(\s*[rbuf]*(['"])([^'"]*)\3([^\n]*)''', re.M)
RE_DJANGO_PATH = re.compile(r'''\b(?:re_)?path\(\s*r?(['"])([^'"]*)\1\s*,\s*([\w.]+)''')

RE_FETCH = re.compile(r'''(?<![\w$.])fetch\(\s*([\'"`])([^\'"`]*)\1''')
RE_HTTP_CLIENT = re.compile(r'''\.(get|post|put|patch|delete)\(\s*([\'"`])([^\'"`]*)\2''')
RE_RECEIVER = re.compile(r'([\w$]+(?:\.[\w$]+)*)$')
RE_AXIOS_CREATE = re.compile(r'''(?:const|let|var)\s+([\w$]+)\s*=\s*axios\.create\(\s*\{[^}]*?baseURL\s*:\s*([\'"`])([^\'"`]*)\2''', re.S)
NOT_HTTP_CLIENTS = {'map', 'params', 'searchParams', 'headers', 'cache', 'store', 'localStorage', 'sessionStorage',
                    'formData', 'url', 'URLSearchParams', 'Reflect', 'config', 'configService', 'process.env'}

DB_OPS = 'findUnique|findUniqueOrThrow|findFirst|findFirstOrThrow|findMany|create|createMany|update|updateMany|upsert|delete|deleteMany|aggregate|count|groupBy'
SDKS = ('stripe|twilio|sgMail|sendgrid|mailgun|ses|sesClient|s3|s3Client|sqs|sns|openai|anthropic|slack|slackClient|'
        'octokit|firebase|redis|redisClient|kafka|resend|postmark|pusher|algolia|sentry|Sentry|mixpanel|'
        'paypal|razorpay|shopify|hubspot|salesforce|braintree|plaid|cloudinary|mailchimp|intercom|zendesk|auth0|clerk|supabase')
SINK_PATTERNS = {
    'js': [
        ('db', re.compile(r'(?<![\w$])(?:this\.)?([\w$]*(?:prisma|Prisma|db|Db|tx))\.(\w+)\.(' + DB_OPS + r')\('), lambda m: m.group(2)),
        ('db', re.compile(r'(?<![\w$])(?:this\.)?([\w$]+?)(?:Repository|Repo)\.(find\w*|save|insert|update|delete|remove|softDelete|count\w*|query|createQueryBuilder|upsert|increment|decrement)\('), lambda m: m.group(1)),
        ('db', re.compile(r'getRepository\(\s*([\w$]+)\s*\)'), lambda m: m.group(1)),
        ('db', re.compile(r'(?<![\w$.])(?:knex|db|trx)\(\s*[\'"](\w+)[\'"]'), lambda m: m.group(1)),
        ('db', re.compile(r'\.collection\(\s*[\'"](\w+)[\'"]'), lambda m: m.group(1)),
        ('db', re.compile(r'(?<![\w$.])([A-Z][\w$]*)\.(findOne|findById|findByIdAndUpdate|findByIdAndDelete|findOneAndUpdate|updateOne|updateMany|deleteOne|deleteMany|countDocuments|insertMany|aggregate|bulkCreate|findAll|findByPk)\('), lambda m: m.group(1)),
        ('db', re.compile(r'(?:query|execute|raw|\$queryRaw|\$executeRaw|\$queryRawUnsafe|sql)\s*[(`]\s*[\'"`]?\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b([^`\'"]{0,400})', re.I), lambda m: sql_table(m.group(1) + m.group(2))),
        ('external', re.compile(r'(?<![\w$.])(?:fetch|got|ky|axios(?:\.\w+)?|superagent\.\w+|request)\(\s*[\'"`](https?://[^/\'"`$]+)'), lambda m: re.sub(r'^https?://', '', m.group(1))),
        ('external', re.compile(r'(?<![\w$])(?:this\.)?(' + SDKS + r')\.([\w$.]+)\(', re.I), lambda m: m.group(1).lower().replace('client', '')),
    ],
    'py': [
        ('db', re.compile(r'\b([A-Z]\w*)\.objects\.(\w+)\('), lambda m: m.group(1)),
        ('db', re.compile(r'\bsession\.(query|add|add_all|delete|merge|get|scalars|execute)\(\s*([A-Z]\w*)'), lambda m: m.group(2)),
        ('db', re.compile(r'\bselect\(\s*([A-Z]\w*)\s*\)'), lambda m: m.group(1)),
        ('db', re.compile(r'''\.execute\(\s*[rbuf]*['"]{1,3}\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b([^'"]{0,400})''', re.I), lambda m: sql_table(m.group(1) + m.group(2))),
        ('db', re.compile(r'''\b(?:db|database|mongo)\[\s*['"](\w+)['"]\s*\]'''), lambda m: m.group(1)),
        ('external', re.compile(r'''\b(?:requests|httpx|session|client)\.(?:get|post|put|patch|delete|request)\(\s*[rbuf]*['"](https?://[^/'"{]+)'''), lambda m: re.sub(r'^https?://', '', m.group(1))),
        ('external', re.compile(r'''\bboto3\.(?:client|resource)\(\s*['"](\w+)['"]'''), lambda m: 'aws-' + m.group(1)),
        ('external', re.compile(r'(?<![\w.])(' + SDKS + r')\.([\w.]+)\(', re.I), lambda m: m.group(1).lower()),
    ],
}


_SDK_NEEDLES = tuple(n.lower() + '.' for n in SDKS.split('|'))
SINK_NEEDLES = {
    'js': [('prisma', 'db.', 'tx.'), ('repository.', 'repo.'), ('getrepository',), ('knex(', 'db(', 'trx('), ('.collection(',),
           ('.findone', '.findbyid', '.updateone', '.updatemany', '.deleteone', '.deletemany', '.countdocuments', '.insertmany',
            '.aggregate', '.bulkcreate', '.findall', '.findbypk', '.findoneandupdate'),
           ('select', 'insert', 'update', 'delete', 'with'), ('http://', 'https://'), _SDK_NEEDLES],
    'py': [('.objects.',), ('session.',), ('select(',), ('.execute(',), ('db[', 'database[', 'mongo['), ('http://', 'https://'),
           ('boto3.',), _SDK_NEEDLES],
}


def sql_table(sql):
    m = re.search(r'\b(?:FROM|INTO|UPDATE|JOIN)\s+["`\[]?([\w.]+)', sql, re.I)
    return m.group(1) if m else 'sql'


ENV_PATTERNS = {
    'js': [re.compile(r'process\.env\.([A-Z_][A-Z0-9_]*)'), re.compile(r'''process\.env\[\s*['"]([A-Z_][A-Z0-9_]*)['"]\s*\]'''),
           re.compile(r'import\.meta\.env\.([A-Z_][A-Z0-9_]*)'), re.compile(r'''(?:configService|config)\.get(?:OrThrow)?(?:<[^>]*>)?\(\s*['"]([A-Z_][A-Z0-9_]*)['"]''')],
    'py': [re.compile(r'''os\.environ\.get\(\s*['"](\w+)['"]'''), re.compile(r'''os\.environ\[\s*['"](\w+)['"]\s*\]'''),
           re.compile(r'''os\.getenv\(\s*['"](\w+)['"]'''), re.compile(r'''\benv(?:\.\w+)?\(\s*['"]([A-Z_][A-Z0-9_]*)['"]''')],
}
ENV_DECL_FILE = re.compile(r'(^|/)(\.env|\.env\.(example|sample|template|defaults|dist)|example\.env|\.env\.local\.example)$')

JOB_PATTERNS = [
    ('cron', re.compile(r'''@Cron\(\s*([^)]*)\)''')), ('interval', re.compile(r'''@Interval\(\s*([^)]*)\)''')),
    ('cron', re.compile(r'''cron\.schedule\(\s*(['"][^'"]+['"])''')), ('cron', re.compile(r'''new CronJob\(\s*(['"][^'"]+['"])''')),
    ('queue', re.compile(r'''@Processor\(\s*([^)]*)\)''')), ('queue', re.compile(r'''new Worker\(\s*(['"][^'"]+['"])''')),
    ('queue', re.compile(r'''\.process\(\s*(['"][^'"]+['"])''')),
    ('task', re.compile(r'''@(?:shared_task|app\.task|celery\.task|\w+\.task)\b(\([^)]*\))?''')),
    ('cron', re.compile(r'''add_job\(\s*([\w.]+)''')), ('cron', re.compile(r'''@periodic_task\(([^)]*)\)''')),
]

FRAMEWORK_DEPS = {
    'react': 'React', 'next': 'Next.js', 'vue': 'Vue', 'nuxt': 'Nuxt', 'svelte': 'Svelte', '@angular/core': 'Angular',
    'express': 'Express', '@nestjs/core': 'NestJS', 'fastify': 'Fastify', 'koa': 'Koa', 'hono': 'Hono',
    '@prisma/client': 'Prisma', 'typeorm': 'TypeORM', 'sequelize': 'Sequelize', 'mongoose': 'Mongoose', 'knex': 'Knex',
    'drizzle-orm': 'Drizzle', 'graphql': 'GraphQL', '@apollo/server': 'Apollo', 'bullmq': 'BullMQ', 'bull': 'Bull',
    'jest': 'Jest', 'vitest': 'Vitest', '@playwright/test': 'Playwright', 'cypress': 'Cypress', 'tailwindcss': 'Tailwind',
    'redux': 'Redux', '@reduxjs/toolkit': 'Redux Toolkit', 'react-query': 'React Query', '@tanstack/react-query': 'React Query',
    'axios': 'axios', 'stripe': 'Stripe', 'socket.io': 'Socket.IO', 'electron': 'Electron', 'react-native': 'React Native',
    'django': 'Django', 'flask': 'Flask', 'fastapi': 'FastAPI', 'sqlalchemy': 'SQLAlchemy', 'celery': 'Celery',
    'pydantic': 'Pydantic', 'pytest': 'pytest', 'alembic': 'Alembic', 'djangorestframework': 'Django REST Framework',
    'starlette': 'Starlette', 'pandas': 'pandas', 'numpy': 'NumPy', 'torch': 'PyTorch', 'tensorflow': 'TensorFlow',
}


# ------------------------------------------------------------------ the repo model

class Repo:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.is_git = bool(git(self.root, 'rev-parse', '--is-inside-work-tree').strip())
        self._text, self._stripped, self._lines, self._sinks, self._tsconfigs, self._barrel = {}, {}, {}, {}, {}, {}

    # ---- files
    @cached_property
    def files(self):
        if self.is_git:
            raw = git(self.root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard')
            paths = [p for p in raw.split('\0') if p]
        else:
            paths = []
            for d, dirs, names in os.walk(self.root):
                dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith('.')]
                rel = os.path.relpath(d, self.root)
                paths += [posixpath.normpath(posixpath.join(rel.replace(os.sep, '/'), n)) for n in names]
        out = []
        for p in sorted(set(paths)):
            parts = p.split('/')
            if any(x in SKIP_DIRS for x in parts[:-1]) or parts[-1] in LOCKFILES:
                continue
            full = os.path.join(self.root, p)
            if not os.path.isfile(full):
                continue
            out.append(p)
        return out

    @cached_property
    def fileset(self):
        return set(self.files)

    @cached_property
    def code(self):
        res = []
        for p in self.files:
            ext = posixpath.splitext(p)[1]
            if ext not in CODE_EXT or p.endswith('.min.js'):
                continue
            try:
                if os.path.getsize(os.path.join(self.root, p)) > MAX_BYTES:
                    continue
            except OSError:
                continue
            res.append(p)
        return res

    @cached_property
    def generated(self):
        """Generated code stays in the import graph (it often wires real files together) but is never
        reported as unused or duplicated."""
        res = set()
        for p in self.code:
            head = self.text(p)[:400].lower()
            if re.search(r'@generated|code generated|do not edit|auto-?generated|generated by', head) \
                    or re.search(r'(^|/)(__generated__|generated)/|\.gen\.\w+$|\.generated\.\w+$|_pb2\.py$', p):
                res.add(p)
        return res

    @cached_property
    def codeset(self):
        return set(self.code)

    def lang(self, path):
        return CODE_EXT.get(posixpath.splitext(path)[1])

    def text(self, path):
        if path not in self._text:
            try:
                with open(os.path.join(self.root, path), encoding='utf-8', errors='replace') as f:
                    self._text[path] = f.read()
            except OSError:
                self._text[path] = ''
        return self._text[path]

    def stripped(self, path):
        if path not in self._stripped:
            t = self.text(path)
            self._stripped[path] = strip_py(t) if self.lang(path) == 'py' else strip_js(t)
        return self._stripped[path]

    def line_of(self, path, offset):
        if path not in self._lines:
            self._lines[path] = Lines(self.text(path))
        return self._lines[path].line(offset)

    def loc(self, path):
        return sum(1 for ln in self.text(path).splitlines() if ln.strip())

    # ---- package.json / tsconfig
    @cached_property
    def packages(self):
        """dir -> parsed package.json"""
        res = {}
        for p in self.files:
            if posixpath.basename(p) == 'package.json':
                try:
                    res[posixpath.dirname(p)] = json.loads(self.text(p))
                except ValueError:
                    res[posixpath.dirname(p)] = {}
        return res

    def package_root(self, path):
        d = posixpath.dirname(path)
        while True:
            if d in self.packages:
                return d
            if not d:
                return ''
            d = posixpath.dirname(d)

    @cached_property
    def workspace_pkgs(self):
        return {pkg.get('name'): d for d, pkg in self.packages.items() if pkg.get('name') and d}

    def tsconfig_for(self, path):
        d = posixpath.dirname(path)
        while True:
            for name in ('tsconfig.json', 'jsconfig.json'):
                cand = posixpath.join(d, name) if d else name
                if cand in self.fileset:
                    return self._load_tsconfig(cand)
            if not d:
                return None
            d = posixpath.dirname(d)

    def _load_tsconfig(self, cfg, depth=0):
        if cfg in self._tsconfigs:
            return self._tsconfigs[cfg]
        result = {'base': None, 'paths': {}, 'paths_base': None}
        try:
            data = json.loads(strip_jsonc(self.text(cfg)))
        except ValueError:
            data = {}
        cdir = posixpath.dirname(cfg)
        ext = data.get('extends')
        if isinstance(ext, str) and ext.startswith('.') and depth < 5:
            parent = posixpath.normpath(posixpath.join(cdir, ext))
            if not parent.endswith('.json'):
                parent += '.json'
            if parent in self.fileset:
                result = dict(self._load_tsconfig(parent, depth + 1))
        opts = data.get('compilerOptions') or {}
        if 'baseUrl' in opts:
            result['base'] = posixpath.normpath(posixpath.join(cdir, opts['baseUrl']))
        if 'paths' in opts:
            result['paths'] = opts['paths']
            result['paths_base'] = result['base'] or cdir
        self._tsconfigs[cfg] = result
        return result

    # ---- module resolution
    def _probe(self, base):
        base = posixpath.normpath(base)
        if base.startswith('..'):
            return None
        if base in self.codeset:
            return base
        stem, ext = posixpath.splitext(base)
        if ext in ('.js', '.jsx', '.mjs', '.cjs'):
            for e in ('.ts', '.tsx', '.mts', '.cts'):
                if stem + e in self.codeset:
                    return stem + e
        for e in JS_RESOLVE_EXT:
            if base + e in self.codeset:
                return base + e
        for e in JS_RESOLVE_EXT:
            cand = posixpath.join(base, 'index' + e)
            if cand in self.codeset:
                return cand
        return None

    def resolve_js(self, src, spec):
        """-> (path, None) | (None, external_pkg) | (None, None) for unresolvable relative/asset"""
        if spec.startswith('.'):
            return self._probe(posixpath.join(posixpath.dirname(src), spec)), None
        if spec.startswith('/'):
            return self._probe(spec.lstrip('/')), None
        cfg = self.tsconfig_for(src)
        if cfg:
            for pattern, targets in (cfg.get('paths') or {}).items():
                if '*' in pattern:
                    pre, _, suf = pattern.partition('*')
                    if not (spec.startswith(pre) and spec.endswith(suf) and len(spec) >= len(pre) + len(suf)):
                        continue
                    star = spec[len(pre):len(spec) - len(suf) if suf else None]
                elif spec != pattern:
                    continue
                else:
                    star = ''
                for t in targets:
                    hit = self._probe(posixpath.join(cfg['paths_base'] or '', t.replace('*', star)))
                    if hit:
                        return hit, None
            if cfg.get('base') is not None:
                hit = self._probe(posixpath.join(cfg['base'], spec))
                if hit:
                    return hit, None
        for name, d in self.workspace_pkgs.items():
            if spec == name or spec.startswith(name + '/'):
                rest = spec[len(name):].lstrip('/')
                pkg = self.packages.get(d, {})
                cands = [posixpath.join(d, rest), posixpath.join(d, 'src', rest)] if rest else \
                    [posixpath.join(d, pkg.get('source') or pkg.get('main') or 'index'), posixpath.join(d, 'src/index'), posixpath.join(d, 'index')]
                for c in cands:
                    hit = self._probe(c)
                    if hit:
                        return hit, None
        if re.match(r'^[\w@][\w@./-]*$', spec) and not re.search(r'\.(css|scss|sass|less|svg|png|jpe?g|gif|json|html)$', spec):
            parts = spec.split('/')
            return None, ('/'.join(parts[:2]) if spec.startswith('@') else parts[0])
        return None, None

    @cached_property
    def py_modules(self):
        """dotted module name -> [paths]; source root = first ancestor dir without __init__.py"""
        res = defaultdict(list)
        for p in self.code:
            if self.lang(p) != 'py':
                continue
            parts = p[:-3].split('/')
            d = parts[:-1]
            while d and '/'.join(d + ['__init__.py']) in self.fileset:
                d = d[:-1]
            mod = parts[len(d):]
            if mod[-1] == '__init__':
                mod = mod[:-1]
            if mod:
                res['.'.join(mod)].append(p)
        return res

    @cached_property
    def _py_names(self):
        return {p: name for name, paths in self.py_modules.items() for p in paths}

    def py_module_name(self, path):
        return self._py_names.get(path)

    def resolve_py(self, src, mod, level, names):
        """-> list of (path, external, names)"""
        def pick(name):
            cands = self.py_modules.get(name) or []
            if not cands:
                return None
            same = [c for c in cands if c.split('/')[0] == src.split('/')[0]]
            return (same or cands)[0]

        if level:
            me = self.py_module_name(src) or ''
            pkg = me.split('.') if src.endswith('__init__.py') else me.split('.')[:-1]
            pkg = pkg[:len(pkg) - (level - 1)] if level > 1 else pkg
            base = '.'.join([x for x in pkg + (mod.split('.') if mod else []) if x])
        else:
            base = mod
        out, rest = [], []
        for n in names:
            sub = pick(base + '.' + n) if n != '*' and base else (pick(n) if n != '*' and not base else None)
            if sub:
                out.append((sub, None, ['*']))
            else:
                rest.append(n)
        if rest or not names:
            hit = pick(base) if base else None
            if hit:
                out.append((hit, None, rest or ['*']))
            elif base and not level:
                parts = base.split('.')
                for i in range(len(parts) - 1, 0, -1):
                    hit = pick('.'.join(parts[:i]))
                    if hit:
                        out.append((hit, None, ['*']))
                        break
                else:
                    out.append((None, parts[0], rest or ['*']))
        return out

    # ---- import graph
    @cached_property
    def edges(self):
        edges = []
        for p in self.code:
            if self.lang(p) == 'py':
                edges += self._py_edges(p)
            else:
                edges += self._js_edges(p)
        return edges

    def _js_edges(self, src):
        s = self.stripped(src)
        out = []

        def add(kind, spec, off, names, locals_=None, exposed=None):
            dst, ext = self.resolve_js(src, spec)
            if dst == src:
                return
            out.append({'src': src, 'dst': dst, 'external': ext, 'spec': spec, 'kind': kind, 'names': names,
                        'locals': locals_ or {}, 'exposed': exposed or [], 'line': self.line_of(src, off)})

        for m in RE_IMPORT_FROM.finditer(s):
            names, locals_ = parse_import_clause(m.group(2))
            add('import', m.group(4), m.start(), names, locals_)
        for m in RE_EXPORT_FROM.finditer(s):
            clause = m.group(2)
            if clause.startswith('*'):
                add('reexport', m.group(4), m.start(), ['*'], exposed=[('*', '*')])
            else:
                pairs = []
                for part in clause.strip('{} \n').split(','):
                    part = re.sub(r'^\s*type\s+', '', part).strip()
                    if part:
                        a, _, b = part.partition(' as ')
                        pairs.append((b.strip() or a.strip(), a.strip()))
                add('reexport', m.group(4), m.start(), [b for _, b in pairs], exposed=pairs)
        seen = {(e['spec'], e['line']) for e in out}
        for m in RE_IMPORT_BARE.finditer(s):
            if (m.group(2), self.line_of(src, m.start())) not in seen:
                add('import', m.group(2), m.start(), [])
        binds = {}
        for m in RE_REQUIRE_BIND.finditer(s):
            binds[(m.group(3), self.line_of(src, m.start()))] = m.group(1)
        for m in RE_REQUIRE.finditer(s):
            ln = self.line_of(src, m.start())
            bind = binds.get((m.group(2), ln))
            if bind and bind.startswith('{'):
                names = [x.split(':')[0].strip() for x in bind.strip('{} ').split(',') if x.strip()]
                locals_ = {x.split(':')[-1].strip(): x.split(':')[0].strip() for x in bind.strip('{} ').split(',') if x.strip()}
            else:
                names, locals_ = ['*'], ({bind: '*'} if bind else {})
            add('require', m.group(2), m.start(), names, locals_)
        for m in RE_DYN_IMPORT.finditer(s):
            add('dynamic', m.group(2), m.start(), ['*'])
        return out

    def _py_edges(self, src):
        s = self.stripped(src)
        out = []
        for m in RE_PY_FROM.finditer(s):
            raw = m.group(1)
            level = len(raw) - len(raw.lstrip('.'))
            names = [x.strip().split(' as ')[0].strip() for x in m.group(2).strip('()\\ \n').replace('\n', ',').split(',')]
            names = [n for n in names if n and re.match(r'^[\w*]+$', n)]
            for dst, ext, nm in self.resolve_py(src, raw.lstrip('.'), level, names):
                if dst != src:
                    out.append({'src': src, 'dst': dst, 'external': ext, 'spec': raw, 'kind': 'import', 'names': nm,
                                'locals': {}, 'exposed': [], 'line': self.line_of(src, m.start())})
        for m in RE_PY_IMPORT.finditer(s):
            for part in m.group(1).split(','):
                mod = part.strip().split(' as ')[0].strip()
                for dst, ext, nm in self.resolve_py(src, mod, 0, []):
                    if dst != src:
                        out.append({'src': src, 'dst': dst, 'external': ext, 'spec': mod, 'kind': 'import', 'names': ['*'],
                                    'locals': {}, 'exposed': [], 'line': self.line_of(src, m.start())})
        return out

    @cached_property
    def out_edges(self):
        d = defaultdict(list)
        for e in self.edges:
            d[e['src']].append(e)
        return d

    @cached_property
    def in_edges(self):
        d = defaultdict(list)
        for e in self.edges:
            if e['dst']:
                d[e['dst']].append(e)
        return d

    def is_barrel(self, path):
        if path not in self._barrel:
            self._barrel[path] = self._is_barrel(path)
        return self._barrel[path]

    def _is_barrel(self, path):
        if self.lang(path) != 'py' and not any(e['kind'] == 'reexport' for e in self.out_edges.get(path, [])):
            return False
        s = self.stripped(path)
        if self.lang(path) == 'py':
            if not path.endswith('__init__.py'):
                return False
            rest = RE_PY_FROM.sub('', RE_PY_IMPORT.sub('', s))
            rest = re.sub(r'^__all__\s*=\s*\[[^\]]*\]', '', rest, flags=re.M)
            return not rest.strip() and bool(self.out_edges.get(path))
        rest = RE_EXPORT_FROM.sub('', s)
        rest = RE_IMPORT_FROM.sub('', rest)
        rest = re.sub(r'export\s*\{[^}]*\}', '', rest)
        return not rest.replace(';', '').strip() and any(e['kind'] == 'reexport' for e in self.out_edges.get(path, []))

    # ---- dependents
    def direct_dependents(self, target):
        res = {}
        for e in self.in_edges.get(target, []):
            r = res.setdefault(e['src'], {'file': e['src'], 'line': e['line'], 'names': [], 'via': None, 'kind': e['kind']})
            r['names'] = sorted(set(r['names']) | set(e['names']))
        frontier = [f for f in res if self.is_barrel(f)]
        seen = set(frontier)
        while frontier:
            b = frontier.pop()
            re_names = set()
            for e in self.out_edges.get(b, []):
                if e['dst'] == target or e['dst'] in seen:
                    re_names |= {x for x, _ in e['exposed']} or set(e['names'])
            for e in self.in_edges.get(b, []):
                if e['src'] == target:
                    continue
                used = set(e['names'])
                names = sorted(used if '*' in re_names or '*' in used else used & re_names)
                if not names and used:
                    continue
                if e['src'] not in res:
                    res[e['src']] = {'file': e['src'], 'line': e['line'], 'names': names, 'via': b, 'kind': e['kind']}
                if self.is_barrel(e['src']) and e['src'] not in seen:
                    seen.add(e['src'])
                    frontier.append(e['src'])
        return res

    def transitive_dependents(self, target, depth=5):
        direct = self.direct_dependents(target)
        dist = {f: 1 for f in direct}
        frontier = list(direct)
        for d in range(2, depth + 1):
            nxt = []
            for f in frontier:
                for e in self.in_edges.get(f, []):
                    if e['src'] not in dist and e['src'] != target:
                        dist[e['src']] = d
                        nxt.append(e['src'])
            frontier = nxt
        return dist

    def forward(self, start, depth=3):
        dist = {start: 0}
        frontier = [start]
        for d in range(1, depth + 1):
            nxt = []
            for f in frontier:
                for e in self.out_edges.get(f, []):
                    if e['dst'] and e['dst'] not in dist:
                        dist[e['dst']] = d
                        nxt.append(e['dst'])
            frontier = nxt
        return dist

    # ---- exports
    def exports(self, path):
        s = self.stripped(path)
        names = []
        if self.lang(path) == 'py':
            for m in RE_PY_DEF.finditer(s):
                n = m.group(1) or m.group(2)
                if n and not n.startswith('_') and n not in names and n not in ('if', 'else', 'try', 'for', 'while', 'with'):
                    names.append((n, self.line_of(path, m.start())))
            return names
        for m in RE_EXPORT_DECL.finditer(s):
            names.append(('default' if m.group(1) else m.group(3), self.line_of(path, m.start())))
        for m in RE_EXPORT_LIST.finditer(s):
            for part in m.group(1).split(','):
                part = re.sub(r'^\s*type\s+', '', part).strip()
                if part:
                    names.append((part.split(' as ')[-1].strip(), self.line_of(path, m.start())))
        for m in RE_EXPORT_DEFAULT.finditer(s):
            names.append(('default', self.line_of(path, m.start())))
        for m in RE_CJS_EXPORT.finditer(s):
            names.append((m.group(1) or 'default', self.line_of(path, m.start())))
        seen, out = set(), []
        for n, ln in names:
            if n not in seen:
                seen.add(n)
                out.append((n, ln))
        return out

    # ---- entry / framework files
    @cached_property
    def entry_files(self):
        res = {}
        for d, pkg in self.packages.items():
            for key in ('main', 'module', 'source', 'types', 'browser'):
                v = pkg.get(key)
                if isinstance(v, str):
                    hit = self._probe(posixpath.join(d, v))
                    if hit:
                        res[hit] = 'package.json ' + key
            bins = pkg.get('bin')
            for v in (bins.values() if isinstance(bins, dict) else [bins] if isinstance(bins, str) else []):
                hit = self._probe(posixpath.join(d, v))
                if hit:
                    res[hit] = 'package.json bin'
            exp = pkg.get('exports')
            for v in _flatten_strings(exp):
                hit = self._probe(posixpath.join(d, v))
                if hit:
                    res[hit] = 'package.json exports'
            for cmd in (pkg.get('scripts') or {}).values():
                for tok in re.findall(r'[\w./-]+\.(?:ts|tsx|js|mjs|cjs|py)\b', str(cmd)):
                    hit = self._probe(posixpath.join(d, tok))
                    if hit:
                        res[hit] = 'package.json script'
        markers = [('NestFactory.create', 'Nest bootstrap'), ('createRoot(', 'React root'), ('ReactDOM.render', 'React root'),
                   ('createApp(', 'Vue app'), ('FastAPI(', 'FastAPI app'), ('Flask(__name__', 'Flask app'),
                   ('__name__ == "__main__"', 'script'), ("__name__ == '__main__'", 'script'), ('.listen(', 'server'),
                   ('new Worker(', 'worker'), ('app.whenReady', 'Electron main'), ('AppRegistry.registerComponent', 'React Native')]
        for p in self.code:
            base = posixpath.basename(p)
            if base in ('manage.py', '__main__.py', 'wsgi.py', 'asgi.py'):
                res.setdefault(p, 'python entry')
            if self.uses_next(p):
                if re.search(r'(^|/)app/(.*/)?(page|layout|route|loading|error|not-found|template|default)\.(tsx|ts|jsx|js)$', p) \
                        or (re.search(r'(^|/)pages/', p) and not is_test(p)):
                    res.setdefault(p, 'Next.js route file')
                if re.search(r'(^|/)middleware\.(ts|js)$', p):
                    res.setdefault(p, 'Next.js middleware')
            s = self.stripped(p)
            for needle, why in markers:
                if needle in s:
                    if needle == '.listen(' and not re.search(r'\b(app|server|fastify|httpServer)\.listen\(', s):
                        continue
                    res.setdefault(p, why)
                    break
        return res

    def uses_next(self, path):
        pkg = self.packages.get(self.package_root(path), {})
        return 'next' in (pkg.get('dependencies') or {}) or 'next' in (pkg.get('devDependencies') or {})

    def framework_registered(self, path):
        s = self.stripped(path)
        if self.lang(path) == 'py':
            base = posixpath.basename(path)[:-3]
            if base in ('models', 'views', 'urls', 'admin', 'apps', 'forms', 'serializers', 'signals', 'tasks', 'routers',
                        'schemas', 'tests', 'migrations', 'consumers', 'context_processors', 'middleware', 'celery') \
                    or '/migrations/' in path or '/alembic/' in path or path.endswith('__init__.py'):
                return True
            return bool(re.search(r'@\w+\.(get|post|put|patch|delete|route|task)\b|@(shared_task|receiver)\b', s))
        return bool(re.search(r'@(Controller|Module|Entity|Resolver|Processor|WebSocketGateway|EventSubscriber|Catch)\(', s))

    # ---- modules
    @cached_property
    def modules(self):
        mods = defaultdict(list)
        for p in self.code:
            mods[module_of(p)].append(p)
        return dict(mods)

    @cached_property
    def _module_edges(self):
        c = Counter()
        for e in self.edges:
            if e['dst']:
                a, b = module_of(e['src']), module_of(e['dst'])
                if a != b:
                    c[(a, b)] += 1
        return c

    def module_edges(self):
        return Counter(self._module_edges)

    # ---- routes
    @cached_property
    def axios_bases(self):
        res = {}
        for p in self.code:
            if self.lang(p) == 'py':
                continue
            for m in RE_AXIOS_CREATE.finditer(self.stripped(p)):
                res[(p, m.group(1))] = m.group(3)
        return res

    @cached_property
    def global_guards(self):
        res = []
        for p in self.code:
            if self.lang(p) == 'py':
                continue
            for m in RE_GLOBAL_GUARDS.finditer(self.stripped(p)):
                names = re.findall(r'new\s+([\w$]+)', m.group(1) or '') + ([m.group(2)] if m.group(2) else [])
                for n in names:
                    res.append({'guard': n, 'file': p, 'line': self.line_of(p, m.start()), 'package': self.package_root(p)})
        return res

    @cached_property
    def routes(self):
        routes = []
        prefixes = {}
        for p in self.code:
            if self.lang(p) != 'py':
                m = RE_GLOBAL_PREFIX.search(self.stripped(p))
                if m:
                    prefixes[self.package_root(p)] = m.group(2)
        mounts = self._express_mounts()
        for p in self.code:
            s = self.stripped(p)
            if self.lang(p) == 'py':
                routes += self._py_routes(p, s)
                continue
            if '@Controller(' in s:
                routes += self._nest_routes(p, s, prefixes.get(self.package_root(p), ''))
            if self.is_express_file(p):
                routes += self._express_routes(p, s, mounts)
            mm = RE_NEXT_ROUTE_FILE.search(p)
            if mm:
                url = norm_url('/'.join(x for x in (mm.group(2) or '').split('/') if x and not x.startswith('(')))
                for m in RE_NEXT_METHOD.finditer(s):
                    routes.append(self._route(m.group(1), url, p, self.line_of(p, m.start()), m.group(1), [], 'Next.js'))
            mm = RE_NEXT_PAGES_API.search(p)
            if mm:
                url = norm_url('api/' + re.sub(r'(^|/)index$', '', mm.group(2)))
                routes.append(self._route('ANY', url, p, 1, 'default', [], 'Next.js'))
        gg = self.global_guards
        for r in routes:
            r['guards'] += ['%s (global)' % g['guard'] for g in gg if g['package'] == self.package_root(r['file'])]
        self._attach_callers(routes)
        return routes

    def _route(self, method, path, file, line, handler, guards, framework):
        return {'method': method.upper(), 'path': norm_url(path), 'file': file, 'line': line, 'handler': handler,
                'guards': guards, 'framework': framework, 'callers': []}

    def _decorator_blocks(self, s):
        """[(decorators[(name, args, offset)], target_kind, target_name, offset)]"""
        blocks = []
        for m in re.finditer(r'(?m)^[ \t]*@', s):
            i = m.end() - 1
            if blocks and i < blocks[-1][3]:
                continue
            decs = []
            while i < len(s) and s[i] == '@':
                nm = RE_DECORATOR.match(s, i)
                if not nm:
                    break
                j = nm.end()
                args = ''
                if j < len(s) and s[j] == '(':
                    k = skip_balanced(s, j)
                    args = s[j + 1:k - 1]
                    j = k
                decs.append((nm.group(1), args, i))
                i = j
                while i < len(s) and s[i] in ' \t\r\n':
                    i += 1
            tail = s[i:i + 300]
            cm = re.match(r'(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([\w$]+)', tail)
            if cm:
                blocks.append((decs, 'class', cm.group(1), i))
                continue
            mm = re.match(r'(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)*([\w$]+)\s*(?:<[^>]*>)?\s*\(', tail)
            if mm:
                blocks.append((decs, 'member', mm.group(1), i))
        return blocks

    def _nest_routes(self, p, s, global_prefix):
        out = []
        blocks = self._decorator_blocks(s)
        ctrl_prefix, class_guards = None, []
        for decs, kind, name, off in blocks:
            names = {d[0]: d[1] for d in decs}
            if kind == 'class':
                if 'Controller' in names:
                    arg = names['Controller']
                    mm = re.search(r'''(['"`])([^'"`]*)\1''', arg)
                    ctrl_prefix = mm.group(2) if mm else ''
                    class_guards = _guard_names(names.get('UseGuards', ''))
                else:
                    ctrl_prefix = None
                continue
            if ctrl_prefix is None:
                continue
            for dname, args, doff in decs:
                if dname in ('Get', 'Post', 'Put', 'Patch', 'Delete', 'All', 'Head', 'Options'):
                    mm = re.search(r'''(['"`])([^'"`]*)\1''', args)
                    url = join_url(global_prefix, ctrl_prefix, mm.group(2) if mm else '')
                    guards = class_guards + _guard_names(names.get('UseGuards', ''))
                    out.append(self._route(dname, url, p, self.line_of(p, doff), name, guards, 'NestJS'))
        return out

    def _express_mounts(self):
        """(dst_file, receiver_name or None) -> prefix"""
        mounts = {}
        for p in self.code:
            if self.lang(p) == 'py':
                continue
            s = self.stripped(p)
            if '.use(' not in s:
                continue
            local_imports = {}
            for e in self.out_edges.get(p, []):
                for loc, src_name in e['locals'].items():
                    if e['dst']:
                        local_imports[loc] = (e['dst'], src_name)
            for m in RE_EXPRESS_USE.finditer(s):
                prefix = m.group(3)
                args = split_top_level(m.group(4))
                ident = args[-1] if args else ''
                if not re.match(r'^[\w$]+$', ident):
                    continue
                if ident in local_imports:
                    dst, src_name = local_imports[ident]
                    mounts[(dst, None if src_name in ('default', '*') else src_name)] = prefix
                else:
                    mounts[(p, ident)] = prefix
        return mounts

    def _express_routes(self, p, s, mounts):
        out = []
        for m in RE_EXPRESS_ROUTE.finditer(s):
            recv, method, url = m.group(1), m.group(2), m.group(4)
            if recv in ('axios', 'api', 'http', 'client', 'request', 'superagent', 'ky', 'got', 'this', 'fetcher'):
                continue
            close = skip_balanced(s, m.end(0) - len(m.group(0)) + m.group(0).index('('))
            args = split_top_level(s[m.end(0):close - 1])
            if not args:
                continue
            handler = args[-1]
            handler = handler if re.match(r'^[\w$.]+$', handler) else '<inline>'
            guards = []
            for a in args[:-1]:
                g = re.match(r'^([\w$.]+)', a)
                if g:
                    guards.append(g.group(1))
            prefix = mounts.get((p, recv)) or mounts.get((p, None)) or ''
            out.append(self._route(method, join_url(prefix, url), p, self.line_of(p, m.start()), handler, guards, 'Express'))
        return out

    def _py_routes(self, p, s):
        out = []
        prefixes = {}
        for m in RE_PY_ROUTER.finditer(s):
            pm = re.search(r'''(?:prefix|url_prefix)\s*=\s*['"]([^'"]*)['"]''', m.group(3))
            prefixes[m.group(1)] = pm.group(1) if pm else ''
        for m in re.finditer(r'''include_router\(\s*(\w+)\s*,[^)]*?prefix\s*=\s*['"]([^'"]*)['"]''', s):
            prefixes[m.group(1)] = m.group(2) + prefixes.get(m.group(1), '')
        for m in RE_PY_ROUTE.finditer(s):
            recv, kind, url, rest = m.group(1), m.group(2), m.group(4), m.group(5)
            if kind in ('route', 'api_route'):
                mm = re.search(r'methods\s*=\s*\[([^\]]*)\]', rest)
                methods = re.findall(r'''['"](\w+)['"]''', mm.group(1)) if mm else ['GET']
            else:
                methods = [kind if kind != 'websocket' else 'WS']
            paren = s.find('(', m.start())
            dec_end = skip_balanced(s, paren)
            after = s[dec_end:dec_end + 2000]
            dm = re.search(r'(?m)^[ \t]*(?:async\s+)?def\s+(\w+)\s*\(', after)
            handler = dm.group(1) if dm else '?'
            guards = re.findall(r'(?:Depends|Security)\(\s*([\w.]+)', s[paren:dec_end])
            if dm:
                block = after[:dm.start()]
                guards += [g for g in re.findall(r'(?m)^[ \t]*@([\w.]+)', block) if '.' not in g or not g.endswith(('.get', '.post', '.route'))]
                sig_start = dec_end + dm.end() - 1
                sig = s[sig_start:skip_balanced(s, sig_start)]
                guards += re.findall(r'(?:Depends|Security)\(\s*([\w.]+)', sig)
                for alias, fn in self.py_dep_aliases.items():
                    if re.search(r':\s*%s\b' % re.escape(alias), sig):
                        guards.append('%s (%s)' % (fn, alias))
            for meth in methods:
                out.append(self._route(meth, join_url(prefixes.get(recv, ''), url), p, self.line_of(p, m.start()), handler,
                                       guards, 'FastAPI' if kind != 'route' else 'Flask'))
        if posixpath.basename(p) == 'urls.py':
            for m in RE_DJANGO_PATH.finditer(s):
                out.append(self._route('ANY', m.group(2), p, self.line_of(p, m.start()), m.group(3), [], 'Django'))
        return out

    def is_express_file(self, p):
        s = self.stripped(p)
        return any(k in s for k in ('express', 'Router(', 'fastify', 'koa', 'hono', 'Hono(')) and bool(RE_EXPRESS_FILE.search(s))

    def is_route_file(self, p):
        return '@Controller(' in self.stripped(p) or self.is_express_file(p)

    @cached_property
    def py_dep_aliases(self):
        """CurrentUser = Annotated[User, Depends(get_current_user)] -> {'CurrentUser': 'get_current_user'}"""
        res = {}
        for p in self.code:
            if self.lang(p) == 'py':
                for m in re.finditer(r'(?m)^(\w+)\s*=\s*Annotated\[[^\n]*?(?:Depends|Security)\(\s*([\w.]+)', self.stripped(p)):
                    res[m.group(1)] = m.group(2)
        return res

    @cached_property
    def middleware(self):
        """App-wide or route-group middleware: Nest consumer.apply(...).forRoutes(...), Express app.use(fn)."""
        out = []
        for p in self.code:
            if self.lang(p) == 'py':
                for m in re.finditer(r'add_middleware\(\s*([\w.]+)', self.stripped(p)):
                    out.append({'middleware': m.group(1), 'scope': 'app', 'file': p, 'line': self.line_of(p, m.start())})
                continue
            s = self.stripped(p)
            for m in re.finditer(r'\.apply\(([^)]*)\)((?:\s*\.exclude\([^;]*?\))?\s*\.forRoutes\(([^;]*?)\))\s*;?', s):
                out.append({'middleware': ', '.join(re.findall(r'[A-Z][\w$]*', m.group(1))) or m.group(1).strip(),
                            'scope': re.sub(r'\s+', ' ', m.group(3)).strip()[:120], 'file': p, 'line': self.line_of(p, m.start())})
            for m in re.finditer(r'(?<![\w$.])(?:app|server|router|api)\.use\(\s*([\w$.]+(?:\([^()]*\))?)\s*\)', s):
                out.append({'middleware': m.group(1), 'scope': 'all routes', 'file': p, 'line': self.line_of(p, m.start())})
        return out

    @cached_property
    def client_calls(self):
        route_files = set()
        calls = []
        for p in self.code:
            if self.lang(p) == 'py':
                continue
            s = self.stripped(p)
            if self.is_route_file(p):
                route_files.add(p)
                continue
            imports = {}
            for e in self.out_edges.get(p, []):
                for loc, src_name in e['locals'].items():
                    imports[loc] = (e['dst'], src_name)
            for m in RE_FETCH.finditer(s):
                url = m.group(2)
                rest = s[m.end():m.end() + 300]
                mm = re.match(r'''\s*,\s*\{[^}]*?method\s*:\s*['"](\w+)['"]''', rest, re.S)
                calls.append(self._call(p, s, m.start(), mm.group(1) if mm else 'GET', url, None))
            for m in RE_HTTP_CLIENT.finditer(s):
                rm = RE_RECEIVER.search(s, max(0, m.start() - 80), m.start())
                if not rm:
                    continue
                recv, method, url = rm.group(1), m.group(1), m.group(3)
                if recv in NOT_HTTP_CLIENTS or recv.split('.')[-1] in NOT_HTTP_CLIENTS:
                    continue
                if not (url.startswith('/') or url.startswith('http') or url.startswith('${')):
                    continue
                base = self.axios_bases.get((p, recv)) or self.axios_bases.get(imports.get(recv, (None, None))) or ''
                calls.append(self._call(p, s, m.start(), method, url, base))
        return [c for c in calls if c]

    def _call(self, p, s, off, method, url, base):
        raw = url
        url = re.sub(r'^\$\{[^}]*\}', '', url)
        if url.startswith('http'):
            return None if not re.match(r'https?://(localhost|127\.0\.0\.1)', url) else \
                {'file': p, 'line': self.line_of(p, off), 'method': method.upper(), 'url': norm_url(url), 'raw': raw}
        if not url.startswith('/'):
            return None
        return {'file': p, 'line': self.line_of(p, off), 'method': method.upper(), 'url': norm_url(join_url(base or '', url)), 'raw': raw}

    def _attach_callers(self, routes):
        def segs(u):
            return [x for x in u.split('/') if x]

        def seg_eq(a, b):
            if len(a) != len(b):
                return False
            return all(x == y or x.startswith(':') or y.startswith(':') or x == '*' for x, y in zip(a, b))

        rsegs = [(r, segs(r['path'])) for r in routes]
        for c in self.client_calls:
            cs = segs(c['url'])
            ok = (c['method'], 'ANY', 'ALL')
            exact = [r for r, rs in rsegs if len(rs) == len(cs) and r['method'] in ok and seg_eq(rs, cs)]
            hits = exact
            if not hits and cs:
                hits = [r for r, rs in rsegs if len(rs) > len(cs) and r['method'] in ok and seg_eq(rs[-len(cs):], cs)]
                # ponytail: suffix match assumes an unseen baseURL prefix; may over-match very short URLs
            for r in hits:
                r['callers'].append({'file': c['file'], 'line': c['line'], 'url': c['raw'], 'match': 'exact' if exact else 'suffix'})

    def route_reach(self, route, depth=3):
        reach = self.forward(route['file'], depth)
        sinks = [s for f in reach for s in self.sinks_in(f)]
        return {'files': sorted(reach), 'db': sorted({s['target'] for s in sinks if s['kind'] == 'db'}),
                'external': sorted({s['target'] for s in sinks if s['kind'] == 'external'}), 'sinks': sinks}

    def missing_guard(self, guard):
        routes = self.routes
        has = lambda r: any(g.split(' ')[0].split('.')[-1] == guard for g in r['guards'])
        guarded = [r for r in routes if has(r)]
        reach = {id(r): self.route_reach(r) for r in routes}
        data = set()
        for r in guarded:
            data |= set(reach[id(r)]['db'])
        flagged, other = [], []
        for r in routes:
            if has(r):
                continue
            shared = sorted(set(reach[id(r)]['db']) & data)
            (flagged if shared else other).append(dict(r, shared_data=shared))
        # ponytail: data-overlap heuristic; a route can be public on purpose, so the agent must read each one
        return {'guard': guard, 'guarded': len(guarded), 'protected_data': sorted(data), 'flagged': flagged,
                'unguarded_other': [{'method': r['method'], 'path': r['path'], 'file': r['file'], 'line': r['line']} for r in other]}

    # ---- sinks / env / models / jobs
    def sinks_in(self, path):
        if path in self._sinks:
            return self._sinks[path]
        res = []
        lang = 'py' if self.lang(path) == 'py' else 'js'
        if path in self.codeset:
            s = self.stripped(path)
            low = s.lower()
            text_lines = self.text(path).splitlines()
            seen = set()
            for (kind, rx, target), needles in zip(SINK_PATTERNS[lang], SINK_NEEDLES[lang]):
                if not any(n in low for n in needles):
                    continue
                for m in rx.finditer(s):
                    ln = self.line_of(path, m.start())
                    t = target(m)
                    if kind == 'db' and t and t != 'sql':
                        t = re.sub(r'(Entity|Model|Schema|Table|Repository|Repo)$', '', t) or t
                        t = t[:1].lower() + t[1:]
                    if (ln, t) in seen or not t:
                        continue
                    seen.add((ln, t))
                    res.append({'kind': kind, 'target': t, 'file': path, 'line': ln,
                                'code': text_lines[ln - 1].strip()[:140] if ln <= len(text_lines) else ''})
        res.sort(key=lambda x: x['line'])
        self._sinks[path] = res
        return res

    def env(self):
        reads, decl = defaultdict(list), defaultdict(list)
        for p in self.code:
            lang = 'py' if self.lang(p) == 'py' else 'js'
            s = self.stripped(p)
            for rx in ENV_PATTERNS[lang]:
                for m in rx.finditer(s):
                    reads[m.group(1)].append('%s:%d' % (p, self.line_of(p, m.start())))
        for p in self.code:
            if self.lang(p) != 'py' or 'BaseSettings' not in self.stripped(p):
                continue
            s = self.stripped(p)
            for cm in re.finditer(r'(?m)^class\s+\w+\([^)]*BaseSettings[^)]*\)\s*:', s):
                body = s[cm.end():cm.end() + 8000]
                end = re.search(r'(?m)^\S', body)
                body = body[:end.start()] if end else body
                first = re.search(r'(?m)^([ \t]+)\S', body)
                if not first:
                    continue
                for fm in re.finditer(r'(?m)^%s([A-Za-z_]\w*)\s*:' % re.escape(first.group(1)), body):
                    if fm.group(1) not in ('model_config', 'class', 'def', 'if', 'else', 'elif', 'try', 'except', 'for', 'while', 'with'):
                        reads[fm.group(1).upper()].append('%s:%d' % (p, self.line_of(p, cm.end() + fm.start(1))))
        for p in self.files:
            if p.endswith('.prisma'):
                for m in re.finditer(r'env\(\s*"(\w+)"\s*\)', self.text(p)):
                    reads[m.group(1)].append('%s:%d' % (p, self.line_of(p, m.start())))
            if ENV_DECL_FILE.search(p):
                for m in re.finditer(r'(?m)^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=', self.text(p)):
                    decl[m.group(1)].append('%s:%d' % (p, self.line_of(p, m.start())))
        out = []
        for k in sorted(set(reads) | set(decl)):
            out.append({'var': k, 'read_at': sorted(set(reads.get(k, []))), 'declared_in': decl.get(k, []),
                        'note': 'declared but never read' if k not in reads else ('read but not declared in an env example' if decl and k not in decl else '')})
        return out

    def models(self):
        out = []
        for p in self.files:
            if p.endswith('.prisma'):
                t = self.text(p)
                for m in re.finditer(r'(?m)^model\s+(\w+)\s*\{([^}]*)\}', t):
                    fields = [ln.split()[0] for ln in m.group(2).splitlines() if ln.strip() and not ln.strip().startswith(('@@', '//'))]
                    out.append({'name': m.group(1), 'kind': 'Prisma', 'file': p, 'line': self.line_of(p, m.start()), 'fields': fields})
        for p in self.code:
            s = self.stripped(p)
            if self.lang(p) == 'py':
                for m in re.finditer(r'(?m)^class\s+(\w+)\(([^)]*)\)\s*:', s):
                    bases = m.group(2)
                    if 'models.Model' in bases:
                        kind = 'Django'
                    elif re.search(r'\b(Base|db\.Model|DeclarativeBase|SQLModel)\b', bases):
                        kind = 'SQLAlchemy'
                    elif re.search(r'\bModel\b', bases) and posixpath.basename(p) == 'models.py':
                        kind = 'Django'
                    else:
                        kind = None
                    if kind:
                        body = s[m.end():m.end() + 3000]
                        body = body[:re.search(r'(?m)^\S', body).start()] if re.search(r'(?m)^\S', body) else body
                        fields = re.findall(r'(?m)^\s+(\w+)\s*(?::[^=\n]*)?=\s*(?:models\.\w+|Column|mapped_column|db\.Column|Field|relationship)', body)
                        out.append({'name': m.group(1), 'kind': kind, 'file': p, 'line': self.line_of(p, m.start()), 'fields': fields})
            else:
                for m in re.finditer(r'@Entity\([^)]*\)\s*(?:export\s+)?class\s+(\w+)', s):
                    out.append({'name': m.group(1), 'kind': 'TypeORM', 'file': p, 'line': self.line_of(p, m.start()), 'fields': []})
                for m in re.finditer(r'''(?:sequelize\.define|mongoose\.model|\bmodel)\(\s*['"](\w+)['"]''', s):
                    out.append({'name': m.group(1), 'kind': 'Sequelize/Mongoose', 'file': p, 'line': self.line_of(p, m.start()), 'fields': []})
        migr = Counter()
        for p in self.files:
            mm = re.search(r'^(.*?/?migrations)/', p)
            if mm:
                migr[mm.group(1)] += 1
        used = defaultdict(set)
        for p in self.code:
            for sk in self.sinks_in(p):
                if sk['kind'] == 'db':
                    used[sk['target'].lower().rstrip('s')].add(p)
        for mdl in out:
            mdl['used_by'] = sorted(used.get(mdl['name'].lower().rstrip('s'), set()))
        return {'models': out, 'migrations': [{'dir': d, 'files': n} for d, n in sorted(migr.items())]}

    def jobs(self):
        out = []
        for p in self.code:
            s = self.stripped(p)
            for kind, rx in JOB_PATTERNS:
                for m in rx.finditer(s):
                    after = s[m.end():m.end() + 400]
                    nm = re.search(r'(?:async\s+)?(?:def\s+)?([\w$]+)\s*\(', after)
                    out.append({'kind': kind, 'schedule': (m.group(1) or '').strip()[:60], 'file': p,
                                'line': self.line_of(p, m.start()), 'handler': nm.group(1) if nm else '?'})
        return out

    # ---- unused / dupes
    def used_names(self):
        """file -> set of imported names ('*' = everything) with barrel propagation"""
        used = defaultdict(set)
        for e in self.edges:
            if e['dst']:
                used[e['dst']] |= set(e['names']) if e['names'] else {'<side-effect>'}
        for _ in range(4):
            changed = False
            for e in self.edges:
                if e['kind'] != 'reexport' or not e['dst']:
                    continue
                b_used = used.get(e['src'], set())
                for exposed, source in e['exposed']:
                    if exposed == '*':
                        add = set(b_used) if '*' in b_used else {n for n in b_used}
                    elif exposed in b_used or '*' in b_used:
                        add = {source}
                    else:
                        add = set()
                    if not add <= used[e['dst']]:
                        used[e['dst']] |= add
                        changed = True
            if not changed:
                break
        return used

    def unused(self):
        used = self.used_names()
        entries = self.entry_files
        files, exports = [], []
        for p in self.code:
            if is_test(p) or is_config(p) or p in entries or p in self.generated or self.framework_registered(p):
                continue
            has_importers = bool(self.in_edges.get(p))
            if not has_importers:
                files.append({'file': p, 'loc': self.loc(p)})
                continue
            u = used.get(p, set())
            if '*' in u:
                continue
            body = self.stripped(p)
            for name, ln in self.exports(p):
                if name in u:
                    continue
                if name != 'default' and len(re.findall(r'(?<![\w$])%s(?![\w$])' % re.escape(name), body)) > 1:
                    continue  # used locally: the export may be unnecessary, but the code is not dead
                exports.append({'file': p, 'line': ln, 'name': name})
        for f in files:
            stem = posixpath.splitext(posixpath.basename(f['file']))[0]
            needle = stem if stem not in ('index', 'main', '__init__', 'mod', 'utils', 'types') else posixpath.dirname(f['file'])
            f['mentions'] = self.grep_count(needle, exclude=f['file'])
            f['confidence'] = 'high' if f['mentions'] == 0 else 'low'
        for x in exports:
            x['mentions'] = self.grep_count(x['name'], exclude=x['file'], word=True) if x['name'] != 'default' else 0
            x['confidence'] = 'high' if x['mentions'] == 0 else 'low'
        return {'files': files, 'exports': exports}

    @cached_property
    def _texts(self):
        """Every text file in the repo (code, docs, config), for mention counts."""
        res = {}
        for p in self.files:
            try:
                if os.path.getsize(os.path.join(self.root, p)) > 2 * MAX_BYTES:
                    continue
            except OSError:
                continue
            t = self.text(p)
            if '\0' not in t[:2048]:
                res[p] = t
        return res

    @cached_property
    def _ident_index(self):
        idx = defaultdict(set)
        for p, t in self._texts.items():
            for tok in set(re.findall(r'[A-Za-z_$][\w$]*', t)):
                idx[tok].add(p)
        return idx

    def grep_count(self, needle, exclude=None, word=False):
        """How many other files mention needle (as an identifier when word=True)."""
        if not needle:
            return 0
        if word and re.fullmatch(r'[A-Za-z_$][\w$]*', needle):
            return len(self._ident_index.get(needle, set()) - {exclude})
        return sum(1 for p, t in self._texts.items() if p != exclude and needle in t)

    def dupes(self, min_tokens=50):
        index = defaultdict(list)
        toks = {}
        for p in self.code:
            if is_test(p) or p in self.generated:
                continue
            t = tokenize(self.stripped(p), self.lang(p))
            toks[p] = t
            norm = [x[0] for x in t]
            for i in range(0, len(norm) - min_tokens + 1):
                index[hash(tuple(norm[i:i + min_tokens]))].append((p, i))
        runs = defaultdict(set)
        for occ in index.values():
            if len(occ) < 2 or len(occ) > 20:
                continue
            occ = sorted(occ)
            for a in range(len(occ)):
                for b in range(a + 1, len(occ)):
                    (fa, ia), (fb, ib) = occ[a], occ[b]
                    if fa == fb and ib - ia < min_tokens:
                        continue
                    runs[(fa, fb, ia - ib)].add(ia)
        out = []
        for (fa, fb, diag), starts in runs.items():
            starts = sorted(starts)
            groups, cur = [], [starts[0]]
            for s_ in starts[1:]:
                if s_ == cur[-1] + 1:
                    cur.append(s_)
                else:
                    groups.append(cur)
                    cur = [s_]
            groups.append(cur)
            for g in groups:
                a0, a1 = g[0], g[-1] + min_tokens - 1
                b0, b1 = a0 - diag, a1 - diag
                ta, tb = toks[fa], toks[fb]
                if b0 < 0 or b1 >= len(tb) or a1 >= len(ta) or (fa == fb and b0 <= a1):
                    continue
                out.append({'a': '%s:%d-%d' % (fa, ta[a0][1], ta[a1][1]), 'b': '%s:%d-%d' % (fb, tb[b0][1], tb[b1][1]),
                            'tokens': a1 - a0 + 1, 'lines': ta[a1][1] - ta[a0][1] + 1})
        out.sort(key=lambda x: -x['tokens'])
        # ponytail: O(n^2) per identical-window group (capped at 20); MinHash/LSH only if huge repos need it
        return out

    # ---- verify
    def verify_refs(self, refs, commit_exists, pr_exists):
        res = []
        for ref in refs:
            ref = ref.strip().strip('`')
            m = re.match(r'^(.+?):(\d+)(?:-(\d+))?$', ref)
            if m:
                path, a, b = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
                path = path[2:] if path.startswith('./') else path
                full = os.path.join(self.root, path)
                if not os.path.isfile(full):
                    res.append({'ref': ref, 'ok': False, 'reason': 'file not found'})
                    continue
                lines = self.text(path).splitlines()
                if a < 1 or b < a or b > len(lines):
                    res.append({'ref': ref, 'ok': False, 'reason': 'line out of range (file has %d lines)' % len(lines)})
                    continue
                res.append({'ref': ref, 'ok': True, 'lines': ['%d| %s' % (i, lines[i - 1]) for i in range(a, min(b, a + 5) + 1)]})
            elif RE_SHA.match(ref):
                res.append({'ref': ref, 'ok': commit_exists(ref), 'reason': '' if commit_exists(ref) else 'commit not found'})
            elif re.match(r'^#\d+$', ref):
                ok = pr_exists(ref)
                res.append({'ref': ref, 'ok': ok, 'reason': '' if ok else 'no commit mentions it'})
            elif os.path.exists(os.path.join(self.root, ref)):
                res.append({'ref': ref, 'ok': True})
            else:
                res.append({'ref': ref, 'ok': False, 'reason': 'unrecognized or missing'})
        return res

    def verify_docs(self, docs_dir, commit_exists, pr_exists):
        problems, checked = [], 0
        base = os.path.join(self.root, docs_dir)
        for d, _, names in os.walk(base):
            for n in names:
                if not n.endswith('.md'):
                    continue
                full = os.path.join(d, n)
                rel = os.path.relpath(full, self.root).replace(os.sep, '/')
                with open(full, encoding='utf-8', errors='replace') as f:
                    text = f.read()
                text_nocode = re.sub(r'```.*?```', lambda m: '\n' * m.group(0).count('\n'), text, flags=re.S)
                for m in RE_DOC_REF.finditer(text):
                    checked += 1
                    r = self.verify_refs([m.group(0)], commit_exists, pr_exists)[0]
                    if not r['ok']:
                        problems.append({'doc': rel, 'line': text.count('\n', 0, m.start()) + 1, 'ref': m.group(0), 'reason': r['reason']})
                for m in re.finditer(r'(?<!!)\[[^\]]*\]\(([^)\s]+)\)', text_nocode):
                    target = m.group(1)
                    if re.match(r'^(https?:|mailto:|#)', target):
                        continue
                    checked += 1
                    path = target.split('#')[0]
                    if not os.path.exists(os.path.normpath(os.path.join(d, path))):
                        problems.append({'doc': rel, 'line': text_nocode.count('\n', 0, m.start()) + 1, 'ref': target, 'reason': 'broken link'})
                for m in re.finditer(r'`((?=[0-9]*[a-f])[0-9a-f]{7,40})`', text):
                    checked += 1
                    if not commit_exists(m.group(1)):
                        problems.append({'doc': rel, 'line': text.count('\n', 0, m.start()) + 1, 'ref': m.group(1), 'reason': 'commit not found'})
        return {'checked': checked, 'problems': problems}


REF_EXTS = sorted(set(e.lstrip('.') for e in CODE_EXT) | {
    'json', 'md', 'prisma', 'sql', 'yml', 'yaml', 'toml', 'html', 'css', 'scss', 'sh', 'txt', 'graphql', 'gql', 'proto',
    'rb', 'go', 'java', 'kt', 'cs', 'php', 'rs', 'swift', 'vue', 'svelte', 'ini', 'cfg', 'xml', 'tf', 'env', 'example'}, key=len, reverse=True)
RE_DOC_REF = re.compile(r'(?<![\w/.:-])((?:[\w@.\[\]()-]+/)*[\w@.\[\]()-]*\.(?:%s)):(\d+)(?:-(\d+))?(?![\w.])' % '|'.join(REF_EXTS))
RE_SHA = re.compile(r'^(?=[0-9]*[a-f])[0-9a-f]{7,40}$')


def _flatten_strings(v):
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _flatten_strings(x)]
    if isinstance(v, list):
        return [s for x in v for s in _flatten_strings(x)]
    return []


def _guard_names(args):
    return re.findall(r'(?<![\w$.])([A-Z][\w$]*)', args)


def parse_import_clause(clause):
    """'Foo, { a as b, type C }' -> (['default','a','C'], {'Foo':'default','b':'a','C':'C'})"""
    names, locals_ = [], {}
    clause = clause.strip()
    m = re.search(r'\{([^}]*)\}', clause)
    if m:
        for part in m.group(1).split(','):
            part = re.sub(r'^\s*type\s+', '', part).strip()
            if part:
                src, _, loc = part.partition(' as ')
                names.append(src.strip())
                locals_[(loc or src).strip()] = src.strip()
        clause = clause[:m.start()] + clause[m.end():]
    ns = re.search(r'\*\s*as\s+([\w$]+)', clause)
    if ns:
        names.append('*')
        locals_[ns.group(1)] = '*'
        clause = clause[:ns.start()] + clause[ns.end():]
    for tok in clause.split(','):
        tok = tok.strip()
        if re.fullmatch(r'[\w$]+', tok):
            names.append('default')
            locals_[tok] = 'default'
    return names, locals_


KEYWORDS = set('''abstract as async await break case catch class const continue debugger default delete do else enum export
extends false finally for from function get if implements import in instanceof interface let new null of package private
protected public readonly return set static super switch this throw true try type typeof undefined var void while with yield
and assert def del elif except global is lambda nonlocal not or pass raise None True False'''.split())
RE_TOKEN = re.compile(r'''[A-Za-z_$][\w$]*|\d[\w.]*|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`|===?|!==?|<=|>=|=>|&&|\|\||\?\?|\?\.|[^\s\w]''')
RE_IMPORT_LINE = re.compile(r'(?m)^[ \t]*(import\b[^\n]*|export\s+[^\n]*\bfrom\b[^\n]*|from\s+\S+\s+import\b[^\n]*)$')


def tokenize(stripped, lang):
    text = RE_IMPORT_LINE.sub('', stripped)
    lines = Lines(text)
    out = []
    for m in RE_TOKEN.finditer(text):
        tok = m.group(0)
        if tok[0].isalpha() or tok[0] in '_$':
            norm = tok if tok in KEYWORDS else 'I'
        elif tok[0].isdigit() or tok[0] in '\'"`':
            norm = 'L'
        else:
            norm = tok
        out.append((norm, lines.line(m.start())))
    return out


def impact_score(direct, transitive, routes, has_tests, hidden):
    score = 3 * math.log2(1 + direct) + math.log2(1 + transitive) + 4 * math.log2(1 + routes) \
        + (2 if direct and not has_tests else 0) + hidden
    # ponytail: hand-tuned weights; calibrate against real breakages (e.g. tsc after deleting) if tiers feel off
    tier = 'HIGH' if score >= 10 else 'MEDIUM' if score >= 5 else 'LOW'
    return round(score, 1), tier
