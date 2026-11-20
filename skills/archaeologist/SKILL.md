---
name: archaeologist
description: Rebuild the project knowledge a departed developer took with them. Investigates an unfamiliar or legacy codebase end to end and writes a connected project handbook (architecture, every module, every user flow from UI to API to database, data model, integrations, config, jobs, risks, and the important git history - timeline, key decisions and their reasons, incidents, reverts, who built what, knowledge at risk). Answers questions with file:line and commit evidence - why a function exists, what happens when a user clicks something or an endpoint is called, which APIs or tables a flow touches, what breaks if a file is changed or deleted, where auth or tenant isolation lives or is missing, what is unused or duplicated, why some logic was added. Use it for onboarding, handover, knowledge transfer, documenting a project, or any how/why/what-depends-on question about code the user did not write, even if they never say archaeologist.
compatibility: Needs python3 (3.8+) and git. No packages to install, no network, no API keys.
---

# Archaeologist

When the one person who understood a project leaves, most of what they knew is still there: in the code, in the
import graph, and in years of commits. It is just scattered. This skill digs it out, connects it, and writes it
down so the next developer can understand the whole system without that person.

Two things make the result trustworthy rather than a confident guess:

- **Facts come from a deterministic helper**, `scripts/arch.py`: who imports what, every route and who calls it,
  which tables and external APIs each part touches, env vars, models, jobs, and the git history. Same input,
  same output.
- **Every claim carries evidence**, either a `path:line` or a commit. You check that evidence with `arch.py verify`
  before presenting it. A handbook full of plausible but wrong statements is worse than no handbook, because
  the next developer will believe it.

## The helper

Run it from the root of the project being investigated, or pass `--repo DIR`:

```bash
python3 <dir-of-this-SKILL.md>/scripts/arch.py <command> [args]
```

It needs only python3 and git. It installs nothing, uses no network (except `why --prs`, which calls `gh`),
and writes nothing. Output is compact text; add `--json` when you want to process it. TS/JS and Python get
the full import graph. In other languages, structure comes from grep and reading code, but the history
commands still work.

| Command | What it gives you | Reach for it when |
| --- | --- | --- |
| `overview` | stack, manifests and scripts, entry files, tests, infra, contributors, hot files | first contact with a repo |
| `map` / `map --module M` | modules, how they depend on each other, hubs; one module in depth | planning the handbook, writing a module page |
| `deps F` / `dependents F` | what a file imports / who imports it (through barrels and aliases) | following structure |
| `impact F [--delete]` | direct and transitive dependents, routes and callers affected, tests, hidden co-change coupling, owners, a risk tier | "what breaks if…", "is it safe to delete…" |
| `routes [--reach] [--missing-guard G]` | every HTTP route with guards and frontend callers; `--reach` adds tables and APIs it can touch; `--missing-guard` lists gaps | tracing requests, auth and tenant checks |
| `sinks [paths]` | DB and external API calls with `path:line` | "which tables/APIs does this touch" |
| `env`, `models`, `jobs` | env vars (read vs declared), data models and who uses them, crons, queues and tasks | config, data and jobs pages |
| `timeline`, `decisions [path]`, `people`, `orphans` | history by quarter and when each module appeared; commits that explain *why*, fixes and reverts; who worked on what; areas whose main author is gone | history page, "who knows this" |
| `why F[:symbol\|:a-b] [--prs]` | the commit that introduced a function or lines, and every change since, with full messages | "why does this exist / why was this added" |
| `blame F a-b` | which commits wrote exactly these lines | a specific odd condition or special case |
| `cochange F` | files that change together, flagged when no import links them | hidden coupling |
| `changed-since SHA` | changed files, then affected modules and routes, plus new commits | refreshing the handbook, freshness checks |
| `unused`, `dupes` | potentially dead files and exports; duplicated blocks | cleanup questions, risks page |
| `verify REF… \| --docs DIR` | checks `path:12`, `path:12-30`, commit shas, `#42`; `--docs` checks every ref and link in a folder | before every answer and after writing the handbook |

## Pick a mode

- **Excavate:** the user wants to understand, document or take over the whole project ("the dev who built this
  left", "help me understand this codebase end to end", "write docs for this repo"). Build the handbook.
- **Ask:** a specific question. Answer it with evidence, using the handbook first if one exists.
- **Refresh:** a handbook exists (`docs/project-brain/.meta.json`) and the code has moved on. Update only what changed.

If someone asks a single question and there is no handbook, answer the question. Offer the handbook at the
end instead of starting a long build they did not ask for.

## Ground rules

1. **Cite or cut.** Every factual sentence gets a `path:line`, a route, or a commit sha. If you cannot point
   at evidence, say it is your inference, or leave it out. People will act on this text when deleting files
   or changing auth, so an unsupported claim can do real damage.
2. **Structure from the tool, behavior from the code.** `arch.py` knows which files are connected; only
   reading the code tells you what they do. Never describe what a function does without opening it.
3. **Say what you could not determine.** Static analysis misses dynamic dispatch, DI by string token,
   reflection, event buses, config-driven routing and prefixes computed at runtime. When the trail goes cold,
   say so, give a confidence (high, medium or low), and name what would settle it. "No recorded rationale"
   is a useful answer; an invented rationale is not.
4. **Look, don't touch.** Do not modify application code, run migrations, install dependencies, or start
   services while investigating. The only files you write are the handbook's.
5. **History is evidence too.** Commit messages, PR descriptions and reverts are where the *why* lives.
   Quote them rather than paraphrasing them into more certainty than they have.

## Excavate: build the project handbook

Write to `docs/project-brain/` unless the user names another place or the repo already has a docs convention.
**Read `references/handbook.md` now.** It has the template for every page and the linking rules that keep the
handbook connected.

1. **Survey.** Run `overview` and `map`. Tell the user what you found (stack, number of modules, routes and
   contributors, the time span of the history) and the page plan. For a large repo (more than about 25
   modules), propose doing the most important modules first, in `map` order, and the rest on request.
2. **Modules, one at a time, in `map` order.** For each module, run `map --module <path>`, open its key files
   (most imported, the ones with routes, the largest), and then **write `modules/<slug>.md` immediately**. Writing
   each page as soon as it is done means a long build survives an interrupted session, and the user can start
   reading early.
3. **Flows.** `routes --reach` lists every entry point with its guards, frontend callers and the data it can
   reach. Group routes into user-facing features (login, checkout, invoicing…). Trace each important flow by
   reading code hop by hop: UI handler → HTTP call → route → guards and middleware → handler → services → DB or
   external call. Write `flows/<slug>.md`. Treat background jobs (`jobs`) and webhooks as flows too.
4. **Data, integrations, config, jobs.** Use `models`, `sinks`, `env` and `jobs` to write `data-model.md`,
   `integrations.md`, `config.md` and `jobs.md`.
5. **History.** Use `timeline`, `decisions`, `people` and `orphans` to write `history.md`, and add history
   highlights to each module page (`decisions <module-path>`). While reading code you will meet a few odd
   conditions, special cases and workarounds. Run `why` or `blame` on the 3 to 5 most surprising ones: those are
   exactly what the departed developer would have explained over coffee.
6. **Risks.** `impact` on the top hubs from `map`, `routes --missing-guard <Guard>` for each guard or auth
   dependency you found, plus `unused`, `dupes`, `orphans` and hot files. Write `risks.md`.
7. **Glossary, then README.** `glossary.md` maps business words to code names. Write `README.md` last, because
   it summarizes everything: what the system does, the module map, a start-here reading order, how to run it,
   and how to ask questions with this skill.
8. **Verify.** Run `verify --docs docs/project-brain`, fix every broken reference or link, and re-run until it
   is clean. Then write `.meta.json` (format in `references/handbook.md`), recording the commit you built from.
9. **Report back.** List the pages written, the 3 most important things you learned, the top risks, and what
   you could not determine.

## Ask: answer one question with evidence

1. If `docs/project-brain/` exists, read its `README.md` and the pages for the area in question. Check freshness
   with `changed-since <built_from from .meta.json>`. If files relevant to the question changed, trust the code
   over the page.
2. Follow the playbook for the question type below.
3. Run `verify` on every `path:line` and sha you are about to cite, then answer using the matching template.
4. If the answer showed that the handbook is wrong or missing something, offer to fix that page.

### Playbooks

**"What breaks if I change / delete X?"** Run `impact X` (add `--delete` for deletion). Then open the top
direct dependents to see *how* each one uses X: which exports, and whether it would fail at compile time or at
runtime. The tool knows that they depend on X; only the code tells you what breaks. Mention hidden co-change
coupling and any non-code references, because those break without a compiler error.

**"What happens when a user clicks X / calls endpoint Y?"**

1. Find the UI element by its visible text or handler name with grep.
2. Follow the handler to the HTTP call.
3. Use `routes` to find the backend route it hits. Each route lists its frontend callers, so you can also work
   backwards.
4. Read the guards and middleware, then the handler, and follow each call to its definition until you reach the
   database or an external service. `sinks <files you visited>` confirms the end points of the trail.
5. Present a numbered trace, and add a Mermaid sequence diagram when there are more than 4 hops.

**"Which APIs / tables does X touch?"** Trace as above, run `sinks` on every file you visited, and list each
DB table (read or write) and external service with its `path:line`.

**"Why does this function exist?"** Its users tell you its purpose today: `dependents` for the file, and grep
for the symbol. `why <file>:<symbol>` gives the commit that introduced it and every change since. Read those
messages, and use `--prs` for the PR descriptions when the messages are thin. Answer with: purpose (from usage),
origin (commit, date, author), rationale (quoted), and confidence.

**"Why was this particular logic introduced?"** Run `blame <file> <a-b>` on the exact lines, then
`why <file>:<a-b>`, and quote the commit or PR. If the message is just "fix" with no body, look at what else
changed in that commit and say what it suggests, clearly labeled as inference.

**"Where is <concern> implemented?"** This covers auth, tenant isolation, permissions, rate limiting, audit
logging, caching and similar.

1. Grep the concern's vocabulary (`references/frameworks.md` lists it per framework).
2. Find the mechanism: guard, middleware, decorator, dependency, ORM scope or DB policy.
3. Check coverage with `routes --missing-guard <Name>`, which lists routes that reach the same data as protected
   routes without the guard.
4. Read every flagged handler before calling it a gap. Some routes are public on purpose (login, signup, and
   webhooks verified by signature), and a guard can also be applied by middleware or at module level.

**"What's unused / duplicated?"** Run `unused` and `dupes`. Spot-check each candidate with grep, because dynamic
imports, string references and framework registration can hide real usage. Present the results as "potentially
unused", with the last author and date, so whoever deletes something knows whom to ask.

**"How does this project work?"** (an onboarding tour without a full handbook) Run `overview`, `map` and
`routes`, and read the entry files. Give a one-page tour: what the system does, the main modules and how they
connect, the 3 to 5 most important flows, where the data lives, and how to run it. Then offer the full handbook.

### Answer templates

Impact:

```markdown
**`<file>`: <HIGH|MEDIUM|LOW> impact if you <modify|delete> it**
- Imported by N files: `a.ts:3` (uses `X`, `Y`), … grouped by module
- Flows / routes that depend on it: `POST /api/invoices` (`invoice.controller.ts:10`, called from `Invoices.tsx:12`), …
- Hidden coupling: `schema.prisma` changes with it in 4 of 6 commits, with no import link
- Tests that cover it: … (or: none)
- Who knows it: <owners; flag inactive ones>
**Recommendation:** <one or two sentences>
```

Trace:

```markdown
**What happens when <trigger>**
1. <what happens> (`path:line`)
2. …
**Data touched:** <table (read/write) `path:line`> · **External calls:** … · **Guards on the way:** …
**Not determined:** <gaps, with what would settle them>
```

Why:

```markdown
**Why `<symbol>` exists**
- **Purpose today:** … (used by `a.ts:10`, `b.ts:22`)
- **Introduced:** `<sha>` <date> by <author>: "<subject>"
- **Rationale:** "<quoted commit/PR text>" (or: no recorded rationale; the evidence suggests …)
- **Later changes:** `<sha>` "<subject>", …
- **Confidence:** high / medium / low, and why
```

## Refresh: keep the handbook true

1. Read `built_from` from `docs/project-brain/.meta.json`, then run `changed-since <built_from>`. It lists the
   changed files, affected modules and routes, and the new commits.
2. Re-read the changed code, then rewrite the affected module and flow pages. If models, env vars or sinks
   changed, also update the data, config and integrations pages. Add new decisions, reverts and contributors
   to `history.md`.
3. Run `verify --docs docs/project-brain` and update `.meta.json`.

## Limits worth stating to the user

- Routes, guards and callers are found by static patterns, for NestJS, Express-style routers, Next.js, FastAPI,
  Flask and Django. Prefixes computed at runtime, routing done in config files, and custom wrappers can be
  missed. `references/frameworks.md` says where to look by hand.
- Frontend-to-backend matching uses literal URLs. A match marked "(suffix match)" assumed a base-URL prefix,
  so confirm it.
- "Active" means the author committed within 180 days of the newest commit. On a dormant repo, `people` notes
  how long the repo has been idle.
- The impact tier is a heuristic. The lists of files, routes and tests behind it are the real evidence.

## References

- `references/handbook.md`: page templates, linking rules, naming, `.meta.json`. Read it before building or
  refreshing the handbook.
- `references/frameworks.md`: where routes, DI, guards, data access, jobs and tenant scoping live in NestJS,
  Express, Next.js, React, FastAPI, Flask and Django, and the vocabulary for cross-cutting concerns. Read it when
  tracing inside one of these frameworks or hunting for a concern.
