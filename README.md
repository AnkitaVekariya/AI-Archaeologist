# AI Archaeologist

**Understand any unknown codebase end to end, even after the person who built it has left.**

Every team has one: the developer who knew why the code is the way it is. When they leave, the docs are
stale, nobody knows what a file is for, and every change feels risky. Most of what they knew is still
there, in the code, the import graph and years of commits. It's just scattered.

AI Archaeologist is a skill you install into your coding agent (Claude Code, Cursor, Codex, or any tool that
reads [Agent Skills](https://agentskills.io)). It digs that knowledge out, connects it, and writes it down:

- **A project handbook in your repo** (`docs/project-brain/`): architecture, every module, every user flow from
  UI to API to database, data model, integrations, config, jobs and risks. Every page links to the pages it
  relates to.
- **The important git history**: a timeline, key decisions and *why* they were made (quoted from commits and
  PRs), incidents and reverts, who built what, and **knowledge at risk**: areas whose main author is no longer
  active.
- **Answers with evidence**: every claim cites a `file:line` or a commit, and those citations are checked
  before you see them.

There's no database, no server, no API key, and no extra AI service: your own agent does the reasoning. A small
bundled helper script (`python3` + `git`, standard library only) produces the exact facts.

## The killer question: "What happens if I delete this file?"

```text
$ python3 skills/archaeologist/scripts/arch.py impact api/src/invoice/invoice.service.ts --delete

api/src/invoice/invoice.service.ts: HIGH IMPACT (score 22.4, if you delete it)
Why: 8 direct importers · 5 transitive · 4 routes · 1 hidden coupling · covered by 1 test file

Imported by (8):
  api/src/invoice/invoice.controller.ts:2     uses InvoiceService
  api/src/payments/reconcile.service.ts:3     uses InvoiceService
  api/src/reports/reports.service.ts:2        uses InvoiceService (via api/src/invoice/index.ts)
  ...
Routes that depend on it (4):
  POST /api/invoices            api/src/invoice/invoice.controller.ts:10  <- called from web/src/pages/Invoices.tsx:12
  POST /api/payments/reconcile  api/src/payments/payments.controller.ts:10
  ...
Changes together with:
  api/prisma/schema.prisma      4 commits (67%)  HIDDEN: no import link
Owners: Bob Martin (4 commits, last touched it 2024-05-22, now inactive), ...

Recommendation: Do not delete without replacing these dependencies: update the 8 direct importers first, then re-run impact.
```

Your agent then opens those files and explains *how* each one would break.

## Install

### Claude Code

```bash
claude plugin marketplace add AnkitaVekariya/AI-Archaeologist
claude plugin install ai-archaeologist@ai-archaeologist
```

On Claude Code 2.1.275 or later you can do it in one step inside a session:

```text
/plugin install ai-archaeologist --marketplace AnkitaVekariya/AI-Archaeologist
```

The skill runs as `/ai-archaeologist:archaeologist`, and Claude also picks it up on its own when you ask a
matching question.

### Cursor, Codex and other Agent Skills tools

Copy the skill folder into a skills directory your tool reads:

```bash
git clone https://github.com/AnkitaVekariya/AI-Archaeologist.git

# Cursor and Codex, for all your projects
mkdir -p ~/.agents/skills && cp -r AI-Archaeologist/skills/archaeologist ~/.agents/skills/

# or for one project only (commit it, and your whole team gets it)
mkdir -p .agents/skills && cp -r AI-Archaeologist/skills/archaeologist .agents/skills/
```

Cursor also reads `~/.cursor/skills/` and `.cursor/skills/`. Claude Code without the plugin reads
`~/.claude/skills/`. In Cursor, type `/archaeologist`; in Codex, use `$archaeologist` or the skills picker.
Start a new session after installing.

## Use it

Ask in plain language, inside the repo you want to understand:

- "The developer who built this left. Help me understand the whole project." (builds the handbook)
- "What happens if I delete `api/src/invoice/invoice.service.ts`?"
- "What happens when a user clicks the login button?"
- "Which APIs and tables are touched when an invoice is generated?"
- "Why does `markPaid` check `paidAt` before updating?"
- "Where is tenant isolation implemented, and are there gaps?"
- "Which code is unused or duplicated?"
- "Update the handbook" (refreshes only what changed since it was built)

### What the handbook looks like

```text
docs/project-brain/
  README.md          start here: what the system does, module map, reading order, how to run it
  architecture.md    modules and how they connect (Mermaid diagram)
  modules/*.md       purpose, key files, who uses it, what it uses, data, gotchas, owners
  flows/*.md         click -> handler -> route -> guards -> service -> table, step by step
  data-model.md      entities, and which code reads or writes each one
  integrations.md    Stripe, email, S3...: where they're called and what config they need
  config.md          env vars: where they're read, and which are declared but unused
  jobs.md            crons, queues, webhooks
  history.md         timeline, key decisions and why, incidents and reverts, knowledge at risk
  risks.md           hub files, auth/tenant gaps, dead and duplicated code, hidden coupling
  glossary.md        business terms -> code names
  .meta.json         the commit it was built from, used for refresh
```

## The helper script

`skills/archaeologist/scripts/arch.py` also works on its own from any repo:
`python3 arch.py <command> [--json]`.

| Command | Answers |
| --- | --- |
| `overview` | stack, entry files, tests, infra, contributors, hot files |
| `map [--module M]` | modules, how they depend on each other, hub files |
| `deps` / `dependents FILE` | imports in and out (follows tsconfig paths, barrels, workspaces, Python packages) |
| `impact FILE [--delete]` | what breaks, with a risk tier |
| `routes [--reach] [--missing-guard G]` | every route with guards, frontend callers, data touched; routes that skip a guard |
| `sinks`, `env`, `models`, `jobs` | DB and external calls, env vars, data models, background jobs |
| `timeline`, `decisions`, `people`, `orphans` | project history, the why behind changes, who knows what, knowledge at risk |
| `why FILE[:symbol]`, `blame FILE a-b`, `cochange FILE` | origin and history of code, hidden coupling |
| `changed-since SHA` | what changed and what it affects |
| `unused`, `dupes` | potentially dead and duplicated code |
| `verify REF... \| --docs DIR` | checks that cited `file:line` refs, commits and links are real |

**Coverage.** The full import graph works for TypeScript/JavaScript and Python. Routes are detected for NestJS,
Express-style routers, Next.js, FastAPI, Flask and Django. Data access is detected for Prisma, TypeORM, Mongoose,
Sequelize, knex, raw SQL, Django ORM and SQLAlchemy. The git history commands work for any language. Static
analysis has limits (dynamic dispatch, runtime-computed routes, event buses), and the skill says when it hits one.

**Privacy.** Everything runs locally. The script reads files and git history and writes nothing; only
`why --prs` contacts GitHub, through your own `gh` login. Env var *names* are reported, never their values.

## Develop

```bash
python3 -m unittest discover -s tests -v   # builds a sample repo with planted facts, checks every command
claude plugin validate .                    # checks the plugin and marketplace manifests
claude plugin eval . --scaffold             # with/without-skill comparison on evals/ (real, billed model runs)
```

```text
.claude-plugin/        plugin.json + marketplace.json (this repo is its own marketplace)
skills/archaeologist/  SKILL.md, references/, scripts/ (the part you copy into other tools)
tests/                 fixture_shop.py (sample repo with history) + test_arch.py
evals/                 end-to-end skill evals in the `claude plugin eval` format
```
