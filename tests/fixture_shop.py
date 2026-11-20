"""Builds a small multi-module 'shop' repo with scripted git history and planted facts.

Used by tests/test_arch.py and by the skill evals (make-fixture.sh). Run directly:
    python3 tests/fixture_shop.py /path/to/empty/dir

Planted facts (what the tests assert):
- api/src/invoice/invoice.service.ts is imported by 8 files (one via the @app/invoice alias + barrel)
- Nest global prefix 'api'; Express webhook router mounted at /api/webhooks
- TenantGuard protects invoices + payments, but not ReportsController (which reads prisma.invoice)
- web Login button -> api.post('/auth/login') -> POST /api/auth/login -> prisma.user.findUnique
- commit "fix: prevent double payment on webhook retry (#42)" explains the paidAt check in markPaid
- invoice.service.ts co-changes with prisma/schema.prisma; a revert commit exists
- bob built payments and stopped committing 10 months before HEAD (knowledge at risk)
- dead code: api/src/common/legacy-format.ts, worker/app/unused_mod.py
- duplicate logic: calculateTax (invoice.service.ts) vs computeTax (reports.service.ts)
"""
import os
import subprocess
import sys

AUTHORS = {
    "alice": ("Alice Chen", "alice@shop.dev"),
    "bob": ("Bob Martin", "bob@shop.dev"),
    "carol": ("Carol Diaz", "carol@shop.dev"),
}

# ---------------------------------------------------------------- file versions

SCHEMA_HEAD = '''datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}

generator client {
  provider = "prisma-client-js"
}

model User {
  id           String @id @default(cuid())
  email        String @unique
  passwordHash String
  tenantId     String
}
'''

INVOICE_MODEL = '''
model Invoice {
  id         String    @id @default(cuid())
  tenantId   String
  customerId String
  amount     Float
  tax        Float
  createdAt  DateTime  @default(now())%s
}
'''

PAYMENT_MODEL = '''
model Payment {
  id        String   @id @default(cuid())
  tenantId  String
  invoiceId String
  amount    Float
  createdAt DateTime @default(now())
}
'''

STRIPE_EVENT_MODEL = '''
model StripeEvent {
  id         String   @id
  type       String
  receivedAt DateTime @default(now())
}
'''


def schema(stage):
    s = SCHEMA_HEAD
    if stage >= 3:
        extra = ""
        if stage >= 4:
            extra += "\n  paidAt     DateTime?\n  paymentId  String?"
        if stage >= 12:
            extra += "\n  overdue    Boolean   @default(false)"
        if stage >= 7:
            extra += "\n\n  @@index([tenantId, createdAt])"
        s += INVOICE_MODEL % extra
    if stage >= 4:
        s += PAYMENT_MODEL
    if stage >= 8:
        s += STRIPE_EVENT_MODEL
    return s


CALC_TAX = '''export function calculateTax(amount: number, region: string): number {
  const rate = region === 'EU' ? 0.2 : region === 'UK' ? 0.175 : 0.1;
  const base = Math.max(0, amount);
  const tax = Math.round(base * rate * 100) / 100;
  if (tax < 0.01) {
    return 0;
  }
  return tax;
}
'''

CALC_TAX_CACHED = '''const RATE_CACHE = new Map<string, number>();

export function calculateTax(amount: number, region: string): number {
  if (!RATE_CACHE.has(region)) {
    RATE_CACHE.set(region, region === 'EU' ? 0.2 : region === 'UK' ? 0.175 : 0.1);
  }
  const rate = RATE_CACHE.get(region);
  const base = Math.max(0, amount);
  return Math.round(base * rate * 100) / 100;
}
'''

MARK_PAID_V1 = '''
  async markPaid(invoiceId: string, paymentId: string) {
    return this.prisma.invoice.update({
      where: { id: invoiceId },
      data: { paidAt: new Date(), paymentId },
    });
  }
'''

MARK_PAID_V2 = '''
  async markPaid(invoiceId: string, paymentId: string) {
    const invoice = await this.prisma.invoice.findUnique({ where: { id: invoiceId } });
    if (invoice?.paidAt) {
      // Already paid: Stripe retried the webhook. See #42.
      return invoice;
    }
    return this.prisma.invoice.update({
      where: { id: invoiceId },
      data: { paidAt: new Date(), paymentId },
    });
  }
'''

LIST_FOR_PERIOD = '''
  async listForPeriod(tenantId: string, from: Date, to: Date) {
    return this.prisma.invoice.findMany({ where: { tenantId, createdAt: { gte: from, lt: to } } });
  }
'''


def invoice_service(stage):
    tax = CALC_TAX_CACHED if stage == 10 else CALC_TAX
    body = '''import { Injectable } from '@nestjs/common';
import Stripe from 'stripe';
import { PrismaService } from '../prisma/prisma.service';

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY);

%s
@Injectable()
export class InvoiceService {
  constructor(private readonly prisma: PrismaService) {}

  async generate(tenantId: string, customerId: string, amount: number, region: string) {
    const tax = calculateTax(amount, region);
    const invoice = await this.prisma.invoice.create({
      data: { tenantId, customerId, amount, tax },
    });
    await stripe.invoiceItems.create({ customer: customerId, amount: Math.round((amount + tax) * 100), currency: 'usd' });
    return invoice;
  }

  async findOne(tenantId: string, id: string) {
    return this.prisma.invoice.findFirst({ where: { id, tenantId } });
  }
''' % tax
    if stage >= 4:
        body += MARK_PAID_V2 if stage >= 8 else MARK_PAID_V1
    if stage >= 7:
        body += LIST_FOR_PERIOD
    return body + "}\n"


def invoice_controller(stage):
    guard_import = "import { TenantGuard } from '../common/tenant.guard';\n" if stage >= 6 else ""
    guard = "@UseGuards(TenantGuard)\n" if stage >= 6 else ""
    return '''import { Body, Controller, Get, Param, Post, Req, UseGuards } from '@nestjs/common';
import { InvoiceService } from './invoice.service';
%s
@Controller('invoices')
%sexport class InvoiceController {
  constructor(private readonly invoices: InvoiceService) {}

  @Post()
  create(@Req() req, @Body() body) {
    return this.invoices.generate(req.user.tenantId, body.customerId, body.amount, body.region);
  }

  @Get(':id')
  findOne(@Req() req, @Param('id') id: string) {
    return this.invoices.findOne(req.user.tenantId, id);
  }
}
''' % (guard_import, guard)


def payments_controller(stage):
    guard_import = "import { TenantGuard } from '../common/tenant.guard';\n" if stage >= 6 else ""
    guard = "@UseGuards(TenantGuard)\n" if stage >= 6 else ""
    return '''import { Body, Controller, Post, Req, UseGuards } from '@nestjs/common';
import { ReconcileService } from './reconcile.service';
%s
@Controller('payments')
%sexport class PaymentsController {
  constructor(private readonly reconcile: ReconcileService) {}

  @Post('reconcile')
  run(@Req() req, @Body() body) {
    return this.reconcile.run(req.user.tenantId, body.since);
  }
}
''' % (guard_import, guard)


RECONCILE_SERVICE = '''import { Injectable } from '@nestjs/common';
import { PrismaService } from '../prisma/prisma.service';
import { InvoiceService } from '../invoice/invoice.service';

@Injectable()
export class ReconcileService {
  constructor(private readonly prisma: PrismaService, private readonly invoices: InvoiceService) {}

  async run(tenantId: string, since: string) {
    const payments = await this.prisma.payment.findMany({ where: { tenantId, createdAt: { gte: new Date(since) } } });
    for (const payment of payments) {
      await this.invoices.markPaid(payment.invoiceId, payment.id);
    }
    return { reconciled: payments.length };
  }
}
'''


def nest_module(controller, ctrl_file, providers):
    lines = ["import { Module } from '@nestjs/common';",
             "import { %s } from './%s';" % (controller, ctrl_file)]
    for name, path in providers:
        lines.append("import { %s } from '%s';" % (name, path))
    return "\n".join(lines) + '''

@Module({
  controllers: [%s],
  providers: [%s],
})
export class %s {}
''' % (controller, ", ".join(n for n, _ in providers), controller.replace("Controller", "Module"))


INVOICE_MODULE = nest_module("InvoiceController", "invoice.controller",
                             [("InvoiceService", "./invoice.service"), ("PrismaService", "../prisma/prisma.service")])
PAYMENTS_MODULE = nest_module("PaymentsController", "payments.controller",
                              [("ReconcileService", "./reconcile.service"),
                               ("InvoiceService", "../invoice/invoice.service"),
                               ("PrismaService", "../prisma/prisma.service")])
REPORTS_MODULE = '''import { Module } from '@nestjs/common';
import { ReportsController } from './reports.controller';
import { ReportsService } from './reports.service';
import { InvoiceModule } from '../invoice/invoice.module';
import { PrismaService } from '../prisma/prisma.service';

@Module({
  imports: [InvoiceModule],
  controllers: [ReportsController],
  providers: [ReportsService, PrismaService],
})
export class ReportsModule {}
'''
AUTH_MODULE = nest_module("AuthController", "auth.controller",
                          [("AuthService", "./auth.service"), ("PrismaService", "../prisma/prisma.service")])

INVOICE_BARREL = "export { InvoiceService, calculateTax } from './invoice.service';\n"

REPORTS_SERVICE = '''import { Injectable } from '@nestjs/common';
import { InvoiceService } from '@app/invoice';
import { PrismaService } from '../prisma/prisma.service';

export function computeTax(total: number, country: string): number {
  const pct = country === 'EU' ? 0.2 : country === 'UK' ? 0.175 : 0.1;
  const net = Math.max(0, total);
  const result = Math.round(net * pct * 100) / 100;
  if (result < 0.01) {
    return 0;
  }
  return result;
}

@Injectable()
export class ReportsService {
  constructor(private readonly prisma: PrismaService, private readonly invoices: InvoiceService) {}

  async monthlyRevenue(tenantId: string, month: string) {
    const from = new Date(`${month}-01`);
    const to = new Date(from.getFullYear(), from.getMonth() + 1, 1);
    const invoices = await this.invoices.listForPeriod(tenantId, from, to);
    const totals = await this.prisma.invoice.aggregate({ _sum: { amount: true }, where: { createdAt: { gte: from, lt: to } } });
    return { count: invoices.length, revenue: totals._sum.amount, tax: computeTax(totals._sum.amount ?? 0, 'EU') };
  }
}
'''

REPORTS_CONTROLLER = '''import { Controller, Get, Query, Req } from '@nestjs/common';
import { ReportsService } from './reports.service';

@Controller('reports')
export class ReportsController {
  constructor(private readonly reports: ReportsService) {}

  @Get('revenue')
  revenue(@Req() req, @Query('month') month: string) {
    return this.reports.monthlyRevenue(req.user?.tenantId, month);
  }
}
'''

AUTH_CONTROLLER = '''import { Body, Controller, Post } from '@nestjs/common';
import { AuthService } from './auth.service';

@Controller('auth')
export class AuthController {
  constructor(private readonly auth: AuthService) {}

  @Post('login')
  login(@Body() body: { email: string; password: string }) {
    return this.auth.login(body.email, body.password);
  }
}
'''

AUTH_SERVICE = '''import { Injectable, UnauthorizedException } from '@nestjs/common';
import * as jwt from 'jsonwebtoken';
import * as bcrypt from 'bcryptjs';
import { PrismaService } from '../prisma/prisma.service';

@Injectable()
export class AuthService {
  constructor(private readonly prisma: PrismaService) {}

  async login(email: string, password: string) {
    const user = await this.prisma.user.findUnique({ where: { email } });
    if (!user || !(await bcrypt.compare(password, user.passwordHash))) {
      throw new UnauthorizedException();
    }
    const token = jwt.sign({ sub: user.id, tenantId: user.tenantId }, process.env.JWT_SECRET, { expiresIn: '8h' });
    return { token };
  }
}
'''

TENANT_GUARD = '''import { CanActivate, ExecutionContext, ForbiddenException, Injectable } from '@nestjs/common';

// Every invoice and payment request must carry the caller's tenant (set from the JWT).
@Injectable()
export class TenantGuard implements CanActivate {
  canActivate(context: ExecutionContext): boolean {
    const request = context.switchToHttp().getRequest();
    if (!request.user?.tenantId) {
      throw new ForbiddenException('Missing tenant');
    }
    return true;
  }
}
'''

PRISMA_SERVICE = '''import { Injectable, OnModuleInit } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';

@Injectable()
export class PrismaService extends PrismaClient implements OnModuleInit {
  async onModuleInit() {
    await this.$connect();
  }
}
'''

PRISMA_CLIENT = '''import { PrismaClient } from '@prisma/client';

export const prisma = new PrismaClient({ datasources: { db: { url: process.env.DATABASE_URL } } });
'''

STRIPE_ROUTES = '''import { Router } from 'express';
import { prisma } from '../prisma/client';

export const stripeRouter = Router();

function verifySignature(req, res, next) {
  if (!req.headers['stripe-signature']) {
    return res.status(400).send('missing signature');
  }
  next();
}

stripeRouter.post('/stripe', verifySignature, async (req, res) => {
  await prisma.stripeEvent.create({ data: { id: req.body.id, type: req.body.type } });
  res.sendStatus(200);
});
'''

OVERDUE_JOB = '''import { Injectable } from '@nestjs/common';
import { Cron } from '@nestjs/schedule';
import { InvoiceService } from '../invoice/invoice.service';
import { PrismaService } from '../prisma/prisma.service';

@Injectable()
export class OverdueJob {
  constructor(private readonly prisma: PrismaService, private readonly invoices: InvoiceService) {}

  @Cron('0 2 * * *')
  async flagOverdue() {
    const cutoff = new Date(Date.now() - 30 * 24 * 3600 * 1000);
    await this.prisma.invoice.updateMany({ where: { paidAt: null, createdAt: { lt: cutoff } }, data: { overdue: true } });
  }
}
'''

INVOICE_SPEC = '''import { InvoiceService, calculateTax } from './invoice.service';

describe('InvoiceService', () => {
  it('calculates EU tax', () => {
    expect(calculateTax(100, 'EU')).toBe(20);
  });

  it('is defined', () => {
    expect(InvoiceService).toBeDefined();
  });
});
'''

LEGACY_FORMAT = '''export function formatLegacyAmount(cents: number): string {
  return (cents / 100).toFixed(2).replace('.', ',');
}
'''


def main_ts(stage):
    webhook_import = "import { stripeRouter } from './webhooks/stripe.routes';\n" if stage >= 8 else ""
    webhook_mount = "  app.use('/api/webhooks', stripeRouter);\n" if stage >= 8 else ""
    return '''import { NestFactory } from '@nestjs/core';
import { AppModule } from './app.module';
%s
async function bootstrap() {
  const app = await NestFactory.create(AppModule);
  app.setGlobalPrefix('api');
%s  await app.listen(3000);
}
bootstrap();
''' % (webhook_import, webhook_mount)


def app_module(stage):
    mods = []
    if stage >= 2:
        mods.append(("AuthModule", "./auth/auth.module"))
    if stage >= 3:
        mods.append(("InvoiceModule", "./invoice/invoice.module"))
    if stage >= 4:
        mods.append(("PaymentsModule", "./payments/payments.module"))
    if stage >= 7:
        mods.append(("ReportsModule", "./reports/reports.module"))
    lines = ["import { Module } from '@nestjs/common';"]
    if stage >= 12:
        lines.append("import { ScheduleModule } from '@nestjs/schedule';")
    lines += ["import { %s } from '%s';" % m for m in mods]
    imports = [m[0] for m in mods]
    providers = ""
    if stage >= 12:
        lines.append("import { OverdueJob } from './jobs/overdue.job';")
        imports = ["ScheduleModule.forRoot()"] + imports
        providers = "\n  providers: [OverdueJob],"
    return "\n".join(lines) + '''

@Module({
  imports: [%s],%s
})
export class AppModule {}
''' % (", ".join(imports), providers)


WEB_API = '''import axios from 'axios';

export const api = axios.create({ baseURL: '/api' });
'''

LOGIN_TSX = '''import { useState } from 'react';
import { api } from '../api';

export function Login() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');

  const handleLogin = async () => {
    const { data } = await api.post('/auth/login', { email, password });
    localStorage.setItem('token', data.token);
  };

  return (
    <form>
      <input value={email} onChange={(e) => setEmail(e.target.value)} />
      <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
      <button type="button" onClick={handleLogin}>Log in</button>
    </form>
  );
}
'''

INVOICES_TSX = '''import { useEffect, useState } from 'react';
import { api } from '../api';

export function Invoices({ id }: { id: string }) {
  const [invoice, setInvoice] = useState(null);

  useEffect(() => {
    api.get(`/invoices/${id}`).then((res) => setInvoice(res.data));
  }, [id]);

  const createInvoice = async () => {
    await api.post('/invoices', { customerId: 'c1', amount: 100, region: 'EU' });
  };

  return <button onClick={createInvoice}>New invoice</button>;
}
'''

WEB_MAIN = '''import { createRoot } from 'react-dom/client';
import { Login } from './pages/Login';
import { Invoices } from './pages/Invoices';

createRoot(document.getElementById('root')).render(<><Login /><Invoices id="1" /></>);
'''

WORKER_MAIN = '''from fastapi import APIRouter, Depends, FastAPI

from . import tax
from .deps import get_tenant
from .report import build_report

app = FastAPI()
router = APIRouter(prefix="/worker")


@router.get("/tax/{amount}")
def tax_for(amount: float, tenant=Depends(get_tenant)):
    return {"tax": tax.compute_tax(amount)}


@router.post("/reports/run")
def run_report(tenant=Depends(get_tenant)):
    return build_report(tenant)


app.include_router(router)
'''

WORKER_DEPS = '''import os

from fastapi import Header, HTTPException


def get_tenant(x_tenant_id: str = Header(...), authorization: str = Header(...)):
    if authorization != f"Bearer {os.environ.get('WORKER_TOKEN')}":
        raise HTTPException(status_code=401)
    return x_tenant_id
'''

WORKER_TAX = '''RATES = {"EU": 0.2, "UK": 0.175}


def compute_tax(amount: float, region: str = "EU") -> float:
    return round(max(0.0, amount) * RATES.get(region, 0.1), 2)
'''

WORKER_REPORT = '''from .tax import compute_tax


def build_report(tenant: str) -> dict:
    return {"tenant": tenant, "sample_tax": compute_tax(100.0)}
'''

WORKER_UNUSED = '''def old_export_csv(rows):
    return "\\n".join(",".join(map(str, r)) for r in rows)
'''

API_TSCONFIG = '''{
  // path aliases used across the API
  "compilerOptions": {
    "baseUrl": ".",
    "paths": { "@app/*": ["src/*"] },
    "experimentalDecorators": true,
  }
}
'''

ROOT_PKG = '''{
  "name": "shop",
  "private": true,
  "workspaces": ["api", "web"],
  "scripts": { "dev": "npm run start --workspace api" }
}
'''

API_PKG = '''{
  "name": "@shop/api",
  "scripts": { "start": "nest start", "test": "jest" },
  "dependencies": {
    "@nestjs/common": "^10.0.0", "@nestjs/core": "^10.0.0", "@nestjs/schedule": "^4.0.0",
    "@prisma/client": "^5.0.0", "stripe": "^14.0.0", "express": "^4.18.0",
    "jsonwebtoken": "^9.0.0", "bcryptjs": "^2.4.3"
  },
  "devDependencies": { "jest": "^29.0.0", "prisma": "^5.0.0" }
}
'''

WEB_PKG = '''{
  "name": "@shop/web",
  "scripts": { "dev": "vite" },
  "dependencies": { "react": "^18.2.0", "react-dom": "^18.2.0", "axios": "^1.6.0" }
}
'''

ENV_EXAMPLE = '''DATABASE_URL=postgres://localhost/shop
STRIPE_SECRET_KEY=sk_test_xxx
JWT_SECRET=change-me
REPORTS_CACHE_TTL=300
'''

# ---------------------------------------------------------------- history

A = "api/src/"


def commits():
    """(date, author, message, {path: content-or-None})"""
    return [
        ("2023-01-10T10:00:00", "alice", "chore: bootstrap nest api and prisma", {
            "package.json": ROOT_PKG, "api/package.json": API_PKG, "api/tsconfig.json": API_TSCONFIG,
            "api/prisma/schema.prisma": schema(1), A + "main.ts": main_ts(1), A + "app.module.ts": app_module(1),
            A + "prisma/prisma.service.ts": PRISMA_SERVICE, A + "prisma/client.ts": PRISMA_CLIENT,
            ".env.example": ENV_EXAMPLE, "README.md": "# Shop\n\nInternal invoicing app.\n"}),
        ("2023-02-15T10:00:00", "alice", "feat(auth): email/password login (#3)", {
            A + "auth/auth.controller.ts": AUTH_CONTROLLER, A + "auth/auth.service.ts": AUTH_SERVICE,
            A + "auth/auth.module.ts": AUTH_MODULE, A + "app.module.ts": app_module(2)}),
        ("2023-04-02T10:00:00", "alice", "feat(invoice): create and fetch invoices (#12)", {
            A + "invoice/invoice.service.ts": invoice_service(3), A + "invoice/invoice.controller.ts": invoice_controller(3),
            A + "invoice/invoice.module.ts": INVOICE_MODULE, A + "app.module.ts": app_module(3),
            "api/prisma/schema.prisma": schema(3)}),
        ("2023-06-20T10:00:00", "bob", "feat(payments): reconcile payments against invoices (#20)", {
            A + "payments/reconcile.service.ts": RECONCILE_SERVICE, A + "payments/payments.controller.ts": payments_controller(4),
            A + "payments/payments.module.ts": PAYMENTS_MODULE, A + "invoice/invoice.service.ts": invoice_service(4),
            A + "app.module.ts": app_module(4), "api/prisma/schema.prisma": schema(4)}),
        ("2023-07-05T10:00:00", "carol", "feat(web): login and invoices pages (#25)", {
            "web/package.json": WEB_PKG, "web/src/api.ts": WEB_API, "web/src/pages/Login.tsx": LOGIN_TSX,
            "web/src/pages/Invoices.tsx": INVOICES_TSX, "web/src/main.tsx": WEB_MAIN}),
        ("2023-09-12T10:00:00", "bob", "feat: tenant guard for invoice and payment endpoints (#30)\n\n"
         "Every invoice and payment query must be scoped to the caller's tenant; the guard reads tenantId from the JWT.", {
            A + "common/tenant.guard.ts": TENANT_GUARD, A + "invoice/invoice.controller.ts": invoice_controller(6),
            A + "payments/payments.controller.ts": payments_controller(6)}),
        ("2023-11-03T10:00:00", "carol", "feat(reports): monthly revenue report (#33)", {
            A + "reports/reports.service.ts": REPORTS_SERVICE, A + "reports/reports.controller.ts": REPORTS_CONTROLLER,
            A + "reports/reports.module.ts": REPORTS_MODULE, A + "invoice/index.ts": INVOICE_BARREL,
            A + "invoice/invoice.service.ts": invoice_service(7), A + "app.module.ts": app_module(7),
            "api/prisma/schema.prisma": schema(7)}),
        ("2024-01-18T10:00:00", "bob", "fix: prevent double payment on webhook retry (#42)\n\n"
         "Stripe retries webhook deliveries when we respond slowly, so reconcile ran twice for the same payment "
         "and created duplicate ledger entries. markPaid now returns early when the invoice already has paidAt set, "
         "and webhook events are recorded in StripeEvent.", {
            A + "invoice/invoice.service.ts": invoice_service(8), A + "webhooks/stripe.routes.ts": STRIPE_ROUTES,
            A + "main.ts": main_ts(8), "api/prisma/schema.prisma": schema(8)}),
        ("2024-03-07T10:00:00", "alice", "chore: add legacy amount formatter for CSV export", {
            A + "common/legacy-format.ts": LEGACY_FORMAT}),
        ("2024-05-21T10:00:00", "bob", "perf: cache tax rates in invoice service", {
            A + "invoice/invoice.service.ts": invoice_service(10)}),
        ("2024-05-22T10:00:00", "bob", 'Revert "perf: cache tax rates in invoice service"\n\n'
         "Cached rates went stale after the EU rate change, so invoices were taxed at the old rate.", {
            A + "invoice/invoice.service.ts": invoice_service(11)}),
        ("2024-08-01T10:00:00", "bob", "feat(jobs): flag overdue invoices nightly (#47)", {
            A + "jobs/overdue.job.ts": OVERDUE_JOB, A + "app.module.ts": app_module(12),
            "api/prisma/schema.prisma": schema(12)}),
        ("2024-10-15T10:00:00", "carol", "feat(worker): python tax worker (#51)", {
            "worker/requirements.txt": "fastapi\nuvicorn\n", "worker/app/__init__.py": "",
            "worker/app/main.py": WORKER_MAIN, "worker/app/deps.py": WORKER_DEPS, "worker/app/tax.py": WORKER_TAX,
            "worker/app/report.py": WORKER_REPORT, "worker/app/unused_mod.py": WORKER_UNUSED}),
        ("2025-06-01T10:00:00", "alice", "test: invoice service spec", {
            A + "invoice/invoice.service.spec.ts": INVOICE_SPEC}),
    ]


def git(dest, *args, env=None):
    subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                   cwd=dest, check=True, env=env, stdout=subprocess.DEVNULL)


def build(dest):
    os.makedirs(dest, exist_ok=True)
    git(dest, "init", "-q", "-b", "main")
    for date, who, message, files in commits():
        for path, content in files.items():
            full = os.path.join(dest, path)
            if content is None:
                os.remove(full)
                continue
            os.makedirs(os.path.dirname(full) or dest, exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
        name, email = AUTHORS[who]
        env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_AUTHOR_DATE=date + "+0000",
                   GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email, GIT_COMMITTER_DATE=date + "+0000")
        git(dest, "add", "-A")
        subject, _, body = message.partition("\n\n")
        args = ["commit", "-q", "-m", subject] + (["-m", body] if body else [])
        git(dest, *args, env=env)
    return dest


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else ".")
