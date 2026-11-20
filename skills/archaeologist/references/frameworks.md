# Where things live, by framework

Use this when tracing inside a framework or hunting a cross-cutting concern. Each section says what `arch.py`
already detects and what you must find by hand.

## Contents

- NestJS · Express / Koa / Fastify · Next.js · React (and other SPAs) · FastAPI · Flask · Django
- Cross-cutting concerns: auth, tenant isolation, permissions, rate limiting, caching, audit logging, feature flags
- Things static analysis misses

## NestJS

- **Routes:** `@Controller('prefix')` + `@Get/@Post/...` (detected, with `app.setGlobalPrefix`). Versioning (`enableVersioning`,
  `@Version`) and `RouterModule.register` prefixes are not, so grep for them when paths look short.
- **DI:** a constructor parameter typed `FooService` is the real call target. `@Inject('TOKEN')` and
  `useFactory` providers resolve through the module's `providers` array: read the module file.
- **Guards/middleware:** `@UseGuards` on a class or method (detected), `app.useGlobalGuards` / `APP_GUARD` (detected as
  global), `consumer.apply(X).forRoutes(...)` in `configure()` (listed under `middleware`), interceptors and pipes
  (`@UseInterceptors`, `useGlobalPipes`). Grep for these.
- **Data:** Prisma (`this.prisma.<model>.<op>`), TypeORM (`@InjectRepository(Entity)` + `repo.find/save`), Mongoose
  (`@InjectModel`).
- **Jobs/events:** `@Cron`, `@Interval`, `@Processor`/`@Process` (Bull), `@OnEvent` and `EventEmitter2`. Grep `@OnEvent`,
  because `emit('name')` calls are only connected by the event name string.

## Express / Koa / Fastify

- **Routes:** `router.get('/p', mw..., handler)` and `app.use('/prefix', router)` (detected, one level of mounting). Routers
  built in loops, `router.route('/x').get(...)`, and prefixes from variables are not.
- **Middleware:** the order of `app.use(...)` calls matters. Middleware registered before a router applies to it.
  Read the server bootstrap top to bottom.
- **Auth:** `passport.authenticate(...)`, `requireAuth`, `isAuthenticated`, `jwt(...)`, `express-jwt`, custom `req.user` setters.

## Next.js

- **Routes:** `app/**/route.ts` exports `GET/POST/...`, `pages/api/**` (both detected). Server actions (`'use server'`
  functions called from components) are endpoints too. Grep `'use server'`.
- **Auth:** `middleware.ts` (matcher config decides which paths it covers), `getServerSession`, `auth()` (NextAuth/Auth.js),
  `currentUser()` (Clerk).
- **Data:** often fetched directly in server components. Run `sinks` on `app/` files.

## React (and other SPAs)

- **UI → API:** event handlers (`onClick={handleX}`) → a hook or service → `fetch`/`axios`/a generated client. Literal URLs are
  matched to routes (`routes` shows callers). With generated clients (OpenAPI/tRPC/GraphQL), match by operation
  name instead: grep the operation name on both sides.
- **State:** Redux slices and thunks, React Query `useQuery` keys, Zustand stores. Search the key or action name.
- **Routing:** `react-router` route tables, TanStack Router file routes (`routes/`, `routeTree.gen.ts`).

## FastAPI

- **Routes:** `@router.get(...)` with `APIRouter(prefix=)` (detected). `include_router(..., prefix=settings.X)` is computed
  at runtime, so read `main.py` or `api/main.py` for the final prefix.
- **Guards:** `Depends(...)`/`Security(...)` in the signature or in `dependencies=[...]`, and `Annotated` aliases such as
  `CurrentUser` (all detected). Read the dependency function, because that is where the auth check is.
- **Data:** SQLAlchemy/SQLModel `session.exec(select(Model))`, `session.add`. Alembic migrations live in `alembic/versions`.
- **Config:** pydantic `BaseSettings` fields are env vars (detected by `env`).

## Flask

- **Routes:** `@app.route` / `@bp.route` (detected; methods come from `methods=[...]`). Blueprint `url_prefix` is detected when
  it is set where the Blueprint is created, but not when it is set at `register_blueprint`.
- **Guards:** `@login_required`, `@roles_required`, and custom decorators placed between `@x.route(...)` and the function
  (the usual order; detected as guards). Decorators above the route decorator and `before_request` hooks are not
  detected, so grep for them.

## Django

- **Routes:** `urls.py` `path()`/`re_path()` (detected; `include()` prefixes are not composed, so follow `include` by hand).
  DRF `router.register('x', ViewSet)` generates the CRUD routes.
- **Guards:** `@login_required`, `LoginRequiredMixin`, `permission_classes`, `MIDDLEWARE` in settings.
- **Data:** `Model.objects...` (detected), custom managers (`objects = TenantManager()`) and `get_queryset` overrides.
  This is where tenant scoping often hides.
- **Jobs:** Celery `@shared_task`, beat schedule in settings (`CELERY_BEAT_SCHEDULE`), management commands
  (`management/commands/*.py`).

## Cross-cutting concerns: what to grep

| Concern | Vocabulary to search | Where the mechanism usually is |
| --- | --- | --- |
| Auth | `auth`, `jwt`, `session`, `passport`, `login_required`, `UseGuards`, `Depends(get_current`, `getServerSession`, `middleware.ts` | guards, middleware, dependencies, session/JWT parsing |
| Tenant isolation | `tenant`, `tenantId`, `tenant_id`, `org`, `orgId`, `organization`, `workspace`, `account_id`, `company`, `scope`, `CREATE POLICY`, `row level security`, `current_setting(`, `$extends`, `$use`, `get_queryset`, `TenantManager` | a guard that requires the tenant, ORM middleware or extension that adds `where tenantId`, Postgres RLS policies in migrations, base repositories |
| Permissions / RBAC | `role`, `roles`, `permission`, `can(`, `ability`, `casl`, `@Roles`, `is_admin`, `superuser`, `policy` | guards or decorators, policy classes, admin checks inside handlers |
| Rate limiting | `rate`, `throttle`, `@Throttle`, `express-rate-limit`, `slowapi`, `limiter` | middleware, gateway config, nginx/ingress config |
| Caching | `cache`, `redis`, `@CacheKey`, `lru_cache`, `memoize`, `revalidate`, `stale` | decorators, wrapper services, CDN headers |
| Audit logging | `audit`, `activity`, `history`, `event_log`, `createdBy`, `updatedBy` | interceptors, ORM hooks/subscribers, explicit calls in services |
| Feature flags | `flag`, `feature`, `launchdarkly`, `unleash`, `growthbook`, `isEnabled(` | flag client wrapper, config tables |
| Idempotency / retries | `idempot`, `retry`, `dedupe`, `already processed`, `paidAt`, unique constraints | webhook handlers, job handlers, DB constraints |

**How to judge coverage:** find the mechanism, then list every entry point that should pass through it (`routes`, `jobs`,
webhooks, admin scripts) and check each one. `routes --missing-guard <Name>` does the first pass for route-level
guards. Then read each flagged handler, because the check might happen inside the handler, in middleware, or in
the query itself (a `where: { tenantId }`). For tenant isolation, also check queries that **lack** the tenant
filter even behind the guard (for example an `aggregate` with no tenant `where`). The guard proves the caller has
a tenant; only the query proves the data is scoped to it.

## Things static analysis misses (say so when they matter)

- Dynamic imports with computed paths, `require(variable)`, plugin loaders, and DI by string token
- Routes registered in loops, from config files, or through custom wrapper functions
- Event-driven hops (`emit` → `on`), message queues, pub/sub: connect them by event or topic name with grep
- Reflection, metaprogramming, monkey-patching, Django signals
- Code generated at build time (GraphQL/OpenAPI clients, Prisma client): read the source schema instead
- Behavior controlled by env vars or feature flags at runtime
