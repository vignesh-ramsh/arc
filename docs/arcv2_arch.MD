# Arc Framework — v2 Documentation
 
**Version:** 2.0.0-alpha  
**Status:** Active Development  
**Last Updated:** June 2026  
**Python:** 3.12+  
**PostgreSQL:** 16+
 
-----
 
## Table of Contents
 
1. [What is Arc?](#1-what-is-arc)
1. [Core Principles](#2-core-principles)
1. [Architecture — Everything is a Plugin](#3-architecture--everything-is-a-plugin)
1. [Technology Stack](#4-technology-stack)
1. [Project Structure](#5-project-structure)
1. [Configuration — arc.toml](#6-configuration--arctoml)
1. [Environment Variables — .env](#7-environment-variables--env)
1. [arc.lock — Plugin Registry](#8-arclock--plugin-registry)
1. [The Plugin System](#9-the-plugin-system)
1. [Capabilities — Dependency Injection](#10-capabilities--dependency-injection)
1. [Extension Points](#11-extension-points)
1. [System Fields](#12-system-fields)
1. [Field Types](#13-field-types)
1. [Schema Files](#14-schema-files)
1. [Patch Files](#15-patch-files)
1. [Field Change Detection](#16-field-change-detection)
1. [System Tables](#17-system-tables)
1. [Migration Pipeline](#18-migration-pipeline)
1. [Backup, Restore, and Recovery](#19-backup-restore-and-recovery)
1. [CLI Reference](#20-cli-reference)
1. [Context System](#21-context-system)
1. [Lifecycle Hooks](#22-lifecycle-hooks)
1. [Health Checks](#23-health-checks)
1. [Key Rules and Invariants](#24-key-rules-and-invariants)
1. [Writing a Plugin](#25-writing-a-plugin)
1. [Phased Roadmap](#26-phased-roadmap)
-----
 
## 1. What is Arc?
 
Arc is a Python async backend framework built on a **micro-kernel** with a strict **“everything is a plugin”** model. The kernel has zero domain knowledge — it knows nothing about databases, HTTP, or any business domain. Instead it provides a thin orchestration layer that discovers plugins, resolves their dependency graph, wires them together, and manages their lifecycle.
 
The database engine (`db`), the REST layer (`api`), and the HTTP host (`http`) are all ordinary plugins listed in `arc.lock` alongside your own business plugins (`hrms`, `finance`, `inventory`). The kernel treats them all identically.
 
Arc is a **monolith** by design. All plugins run in the same process, share one PostgreSQL database, and communicate in-process through capabilities and extension points — with zero network overhead between them.
 
-----
 
## 2. Core Principles
 
### Compile ahead, do not interpret at runtime
 
Table schemas and patches are compiled to DDL at migration time (`arc db migrate`), not interpreted on every request. The database structure is fixed before the server starts.
 
### Everything is a plugin
 
The kernel imports no SQLAlchemy, no Starlette, no domain logic. Every service the application needs is a plugin. `db` and `http` are not special — they merely arrive earlier in the resolved order because other plugins declare they need them.
 
### Capabilities declare order
 
Plugin startup order is derived from a dependency graph built from `provides` and `requires` declarations. `load_order` is only a tiebreak when two plugins are otherwise independent. Nobody hard-codes a startup sequence.
 
### Inversion of control through extension points
 
Plugins never call each other by name. Instead they contribute to named extension points (`http.routes`, `db.schema_sources`, `cli.commands`). The consuming plugin reads the point — never the contributor. This eliminates all cross-plugin coupling.
 
### Storage is always UTC
 
All timestamps in the database are stored in UTC regardless of the PostgreSQL server’s timezone configuration. Arc forces `timezone = UTC` on every connection. Display timezone is configured separately in `arc.toml [locale]` and used only for formatting in responses and CLI output.
 
### Last-write-wins
 
Arc does not use optimistic locking or version fields. The last update to a row wins. Conflict handling is the responsibility of the application layer.
 
### Single tenant
 
There is no multi-tenant isolation layer. One database, one schema namespace, one set of system tables.
 
### Idempotent migrations
 
`arc db migrate` is safe to run multiple times. Every DDL statement uses `IF NOT EXISTS` or `ON CONFLICT DO NOTHING`. Running migrate on an already-migrated database is a no-op.
 
### Destructive operations require explicit confirmation
 
DROP COLUMN and DROP+ADD operations require the `--confirm-destructive` flag. Without it the migration plan is printed but nothing is applied. Dropped column values are always captured to `_trash` before the column is removed.
 
-----
 
## 3. Architecture — Everything is a Plugin
 
```
┌─────────────────────────────────────────────────────┐
│                    arc.kernel                        │
│  resolver  →  loader  →  orchestrator  →  lifecycle  │
│  capability container    extension registry          │
└────────────────────┬────────────────────────────────┘
                     │  resolves, wires, runs
         ┌───────────┼───────────┬───────────┐
         ▼           ▼           ▼           ▼
    db plugin    api plugin   http plugin  hrms plugin
    provides:    requires:    provides:    requires:
    db.engine    db.session   http.app     db.session
    db.session   ──────────►  ──────────►
```
 
**Two mechanisms replace all special-casing:**
 
**Capabilities** are named services registered by one plugin and consumed by another. `db` provides `"db.session"`. Any plugin that needs the database declares `requires = ("db.session",)`. The resolver builds a dependency graph from these declarations and topologically sorts it. `db` ends up first not because anyone hardcoded it, but because the graph demands it.
 
**Extension points** are named contribution slots. `hrms` contributes its schemas to `"db.schema_sources"` and its routes to `"http.routes"`. The `db` plugin reads `"db.schema_sources"` to discover what to migrate. The `http` plugin reads `"http.routes"` to build the Starlette app. Neither plugin knows who contributed — they only read the point.
 
The kernel’s `build()` runs two synchronous passes before the event loop:
 
1. **Setup pass** — every plugin registers its capabilities (`provides`).
1. **Contribute pass** — every plugin adds to extension points (all capabilities are ready by this point).
Then the ASGI lifespan runs the async lifecycle: `startup_check` → `startup` → `ready`, and on shutdown `shutdown` in reverse order.
 
-----
 
## 4. Technology Stack
 
|Layer          |Technology      |Version      |Notes                           |
|---------------|----------------|-------------|--------------------------------|
|Language       |Python async    |3.12+        |                                |
|Web framework  |Starlette       |0.37+        |Only imported by the http plugin|
|ASGI server    |Uvicorn         |0.30+        |                                |
|Data validation|Pydantic v2     |2.7+         |All models are frozen           |
|ORM / SQL      |SQLAlchemy async|2.0+         |                                |
|Database driver|asyncpg         |0.29+        |Only supported driver           |
|Database       |PostgreSQL      |16+          |                                |
|CLI            |Typer + Click   |0.16+ / 8.1.8|click pinned to 8.1.8           |
|Logging        |structlog       |24.1+        |                                |
|Encryption     |cryptography    |42.0+        |backup encryption only          |
 
**asyncpg is the only supported database driver.** All URLs must use `postgresql+asyncpg://`.  
**Click is pinned to 8.1.8.** Do not upgrade — Typer 0.16 is incompatible with Click 8.2+.
 
-----
 
## 5. Project Structure
 
```
myproject/
│
├── arc/                        ← Arc framework (do not modify)
│   ├── kernel/                 ← Micro-kernel: orchestrator, resolver, loader, etc.
│   ├── plugins/
│   │   ├── db/                 ← Database plugin (engine, session, migrations, backup)
│   │   ├── api/                ← REST API plugin (auto-CRUD, custom routes)
│   │   └── http/               ← HTTP host plugin (Starlette app assembly)
│   └── tools/
│       └── migrate_v1.py       ← One-shot arc.lock format migrator
│
├── hrms/                       ← Your plugin (created by `arc new-plugin hrms`)
│   ├── schemas/                ← Table definitions (JSON)
│   ├── patches/                ← Field modifications to existing tables (JSON)
│   ├── resources/              ← REST resource declarations (Python)
│   ├── tests/
│   ├── __init__.py
│   └── plugin.py               ← Plugin subclass
│
├── finance/                    ← Another plugin
│
├── backups/                    ← arc db backup output (gitignored)
│   └── .gitkeep
│
├── tests/
│   └── test_kernel.py
│
├── arc.toml                    ← App configuration (committed, no secrets)
├── arc.lock                    ← Plugin registry (committed)
├── .env                        ← Secrets and environment overrides (gitignored)
├── .env.example                ← Template for .env (committed)
├── .gitignore
└── pyproject.toml
```
 
-----
 
## 6. Configuration — arc.toml
 
`arc.toml` lives at the project root. The `arc` CLI finds it by walking up the directory tree from the current working directory, so commands work from any subdirectory.
 
**Priority order (highest wins):**
 
```
Environment variables  (ARC_<SECTION>_<KEY>=value)
  → arc.toml values
    → Built-in defaults
```
 
### [app]
 
```toml
[app]
name        = "myapp"
version     = "0.1.0"
environment = "development"    # development | staging | production
debug       = true             # false in production
secret_key  = ""               # set via ARC_APP_SECRET_KEY env var
```
 
### [server]
 
```toml
[server]
host       = "127.0.0.1"   # 0.0.0.0 for external access
port       = 8000
workers    = 1              # match CPU count in production
reload     = true           # NEVER true in production
access_log = true
```
 
### [log]
 
```toml
[log]
level             = "INFO"    # DEBUG | INFO | WARNING | ERROR | CRITICAL
renderer          = "console" # console (dev) | json (production)
include_timestamp = true
```
 
### [locale]
 
```toml
[locale]
timezone         = "Asia/Kolkata"       # IANA name — display only, storage is always UTC
date_format      = "%d-%m-%Y"
datetime_format  = "%d-%m-%Y %H:%M:%S"
currency         = "INR"                # ISO 4217 code
currency_symbol  = "₹"
decimal_places   = 2                    # default for Decimal fields
float_precision  = 6                    # significant digits for Float fields
```
 
`timezone` affects **display only**. All timestamps are stored in UTC. The locale settings are read by plugins via `rt.config.locale` and used for formatting API responses, reports, and CLI output.
 
### [plugins.db]
 
```toml
[plugins.db]
url          = ""        # always set via DATABASE_URL env var — never commit credentials
pool_size    = 5
max_overflow = 10
pool_timeout = 30.0
pool_recycle = 1800
echo         = false     # true = log every SQL statement (dev only)
 
[plugins.db.backup]
encrypt            = false
encryption_key_env = "ARC_BACKUP_KEY"   # env var name holding the passphrase
retention_days     = 30                 # arc db cleanup default cutoff
```
 
### [plugins.api]
 
```toml
[plugins.api]
auto_crud     = true
page_size     = 50
max_page_size = 200
```
 
### [plugins.http]
 
```toml
[plugins.http]
cors_origins = ["http://localhost:3000"]
cors_methods = ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]
```
 
### [plugins.<name>]
 
Your plugin reads its config table via `rt.plugin_config` inside `setup()` or `contribute()`:
 
```toml
[plugins.hrms]
leave_carry_forward = true
payroll_cycle       = "monthly"
```
 
```python
def setup(self, rt):
    cfg = rt.plugin_config          # {"leave_carry_forward": True, ...}
    self._carry = cfg.get("leave_carry_forward", False)
```
 
-----
 
## 7. Environment Variables — .env
 
Place `.env` at the project root (same directory as `arc.toml`). It is gitignored. Copy `.env.example` as the starting point.
 
```bash
cp .env.example .env
# edit .env with real values
```
 
Arc does not load `.env` automatically — use `source .env`, `direnv`, or `python-dotenv` in your run script.
 
Any `arc.toml` value can be overridden with `ARC_<SECTION>_<KEY>=value`:
 
```bash
# .env
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/mydb
ARC_APP_SECRET_KEY=long-random-string
ARC_APP_ENVIRONMENT=production
ARC_APP_DEBUG=false
ARC_LOG_LEVEL=INFO
ARC_LOG_RENDERER=json
ARC_BACKUP_KEY=backup-encryption-passphrase
```
 
**Never put credentials in `arc.toml`.** `arc.toml` is committed to git. `.env` is not.
 
-----
 
## 8. arc.lock — Plugin Registry
 
`arc.lock` is a JSON file at the project root listing every plugin the application uses. It is the single source of truth for the plugin graph. **Commit it to git.**
 
```json
{
  "arc_version": "2.0",
  "graph_hash": "<sha256 of plugin entries>",
  "plugins": [
    {
      "name": "db",
      "version": "1.0.0",
      "entrypoint": "arc.plugins.db.plugin:DatabasePlugin",
      "provides": ["db.engine", "db.session"],
      "requires": [],
      "load_order": 0,
      "critical": true,
      "config": {}
    },
    {
      "name": "hrms",
      "version": "1.0.0",
      "entrypoint": "hrms.plugin:HrmsPlugin",
      "provides": [],
      "requires": ["db.session"],
      "load_order": 100,
      "critical": false,
      "config": {}
    }
  ]
}
```
 
**Fields:**
 
|Field       |Description                                                                  |
|------------|-----------------------------------------------------------------------------|
|`name`      |Unique plugin identifier. Must match the directory name for user plugins.    |
|`version`   |Semver string. Informational.                                                |
|`entrypoint`|`module.path:ClassName` — imported by the loader at boot.                    |
|`provides`  |Capabilities this plugin registers.                                          |
|`requires`  |Capabilities this plugin needs before wiring. Drives graph order.            |
|`load_order`|Integer tiebreak (lower = earlier) when capability edges don’t decide order. |
|`critical`  |`true` — a failed `startup_check` aborts the entire application.             |
|`config`    |Plugin-specific config embedded in the lock (rarely used; prefer `arc.toml`).|
 
`arc new-plugin` writes the entry automatically. Manual edits require running `arc doctor` to verify the graph resolves.
 
-----
 
## 9. The Plugin System
 
Every plugin is a class that subclasses `arc.kernel.Plugin`. The kernel treats all plugins identically — `db`, `http`, and `hrms` are all equal.
 
```python
from arc.kernel import Plugin, CheckResult
 
class HrmsPlugin(Plugin):
    provides = ()                    # capabilities this plugin offers
    requires = ("db.session",)       # capabilities needed before wiring
    load_order = 100                 # tiebreak only
    critical = False
 
    @property
    def name(self) -> str:
        return "hrms"
 
    @property
    def version(self) -> str:
        return "1.0.0"
 
    def setup(self, rt) -> None:
        """Register capabilities (synchronous, before event loop)."""
        pass
 
    def contribute(self, rt) -> None:
        """Add to extension points (all capabilities ready, still synchronous)."""
        pass
 
    async def startup(self) -> None:
        """Open connections, warm caches (inside event loop)."""
        pass
 
    async def shutdown(self) -> None:
        """Close connections, flush buffers."""
        pass
 
    async def health_check(self) -> CheckResult:
        return CheckResult.ok()
```
 
**Two synchronous wiring passes (before the event loop):**
 
`setup(rt)` — register capabilities. Called in resolved order.  
`contribute(rt)` — add routes, schemas, CLI commands to extension points. Called after *all* `setup()` calls, so every capability is guaranteed available.
 
**Three async lifecycle hooks (inside ASGI lifespan or headless run):**
 
`startup()` — open connections, initialise state.  
`ready()` — all plugins started; cross-plugin wiring.  
`shutdown()` — teardown in reverse order.
 
-----
 
## 10. Capabilities — Dependency Injection
 
Capabilities are the DI mechanism. One plugin `provides` a named service; others `require` it.
 
```python
# db plugin — setup()
rt.capabilities.provide("db.session", instance=get_session, source="db")
 
# hrms plugin — contribute() or anywhere after setup
session_cm = rt.capabilities.require("db.session")
async with session_cm() as session:
    result = await session.execute(...)
```
 
Capabilities can be registered as a ready instance or as a lazy factory (built on first `require`). The `http.app` capability is a factory — the Starlette app is not built until all routes have been contributed.
 
Providing a capability already registered raises `CapabilityError`. Pass `replace=True` only when intentionally swapping a plugin (e.g. replacing the SQL db with a NoSQL implementation).
 
**Running multiple instances:**
 
To run two different databases side by side, give them different capability names:
 
```python
# db_hr plugin provides "db.hr"
# db_finance plugin provides "db.finance"
 
class HrmsPlugin(Plugin):
    requires = ("db.hr",)
```
 
-----
 
## 11. Extension Points
 
Extension points decouple contributors from consumers. Plugins contribute values; the kernel and other plugins read them by point name, never by plugin name.
 
**Built-in point names** (defined in `arc.kernel.registry.Points`):
 
|Point              |Type                     |Contributor                        |Consumer          |
|-------------------|-------------------------|-----------------------------------|------------------|
|`http.routes`      |Starlette `Route`        |api plugin, user plugins           |http plugin       |
|`http.middleware`  |Starlette `Middleware`   |any plugin                         |http plugin       |
|`cli.commands`     |Typer app                |db plugin, api plugin, user plugins|arc kernel CLI    |
|`db.schema_sources`|`SchemaSource`           |db plugin (auto-discovers all)     |migrator          |
|`resource.sources` |`Resource`               |user plugins                       |api plugin        |
|`health.checks`    |`async () -> CheckResult`|any plugin                         |`/health` endpoint|
 
**Contributing:**
 
```python
from arc.kernel.registry import Points
from arc.plugins.db.migrations.migrator import SchemaSource
from pathlib import Path
 
def contribute(self, rt):
    rt.extensions.contribute(
        Points.DB_SCHEMA_SOURCES,
        SchemaSource(plugin="hrms", plugin_dir=Path(__file__).parent),
        source=self.name,
    )
```
 
**User plugins do not need to manually contribute `db.schema_sources` or `resource.sources`.** The `db` plugin auto-discovers every plugin’s `schemas/` directory, and the `api` plugin auto-discovers every plugin’s `resources/` directory. Manual contribution is only needed when overriding auto-discovery or contributing from non-standard paths.
 
-----
 
## 12. System Fields
 
Every Arc-managed table receives these columns automatically. They must never appear in schema JSON files.
 
|Field       |Type        |Managed by               |Notes                                                                                                        |
|------------|------------|-------------------------|-------------------------------------------------------------------------------------------------------------|
|`id`        |UUID        |DB — `uuid_generate_v7()`|Time-ordered UUID v7 (RFC 9562). Better index locality than v4. Embedded timestamp enables cursor pagination.|
|`created_at`|TIMESTAMPTZ |DB — `DEFAULT now()`     |Always UTC.                                                                                                  |
|`updated_at`|TIMESTAMPTZ |DB — `DEFAULT now()`     |Always UTC. Update via application logic — no trigger. Last-write-wins.                                      |
|`created_by`|VARCHAR(255)|Application (UserContext)|Set by the API layer using `UserContext.id`.                                                                 |
|`updated_by`|VARCHAR(255)|Application (UserContext)|Set by the API layer using `UserContext.id`.                                                                 |
|`_state`    |INTEGER     |Application / workflow   |Default `0`. Used by the workflow engine (Phase 11). Meaning is plugin-defined.                              |
 
**UUID v7** is generated by the `uuid_generate_v7()` plpgsql function that Arc installs on every `arc db migrate`. It does not require any PostgreSQL extension and works on PG16.
 
-----
 
## 13. Field Types
 
|Arc type  |PostgreSQL type   |Notes                                              |
|----------|------------------|---------------------------------------------------|
|`Data`    |`VARCHAR(n)`      |Default length 140. Override with `max_length`.    |
|`Text`    |`TEXT`            |Unbounded.                                         |
|`Int`     |`INTEGER`         |                                                   |
|`Float`   |`DOUBLE PRECISION`|Display precision from `[locale] float_precision`. |
|`Decimal` |`NUMERIC`         |Display scale from `[locale] decimal_places`.      |
|`Bool`    |`BOOLEAN`         |                                                   |
|`Date`    |`DATE`            |                                                   |
|`Datetime`|`TIMESTAMPTZ`     |Always stored UTC.                                 |
|`JSON`    |`JSONB`           |                                                   |
|`Link`    |`UUID`            |FK to another table’s `id`. Pair with `link_table`.|
 
-----
 
## 14. Schema Files
 
Schema files live in `{plugin}/schemas/` and are named `{TableName}.json`. Each file defines one table.
 
```json
{
  "table": "Employee",
  "plugin": "hrms",
  "fields": [
    {
      "fld_id": "AA01",
      "field_name": "employee_code",
      "type": "Data",
      "reqd": true,
      "max_length": 32,
      "unique": true
    },
    {
      "fld_id": "AA02",
      "field_name": "full_name",
      "type": "Data",
      "reqd": true,
      "max_length": 140
    },
    {
      "fld_id": "AA03",
      "field_name": "department",
      "type": "Link",
      "link_table": "Department"
    },
    {
      "fld_id": "AA04",
      "field_name": "date_of_joining",
      "type": "Date",
      "reqd": true
    }
  ]
}
```
 
**Rules:**
 
- `fld_id` must match `[A-Z]{2}[0-9]{2}`. Permanent — never reassign, never reuse, even after the column is dropped.
- `fld_id` must be unique within the table. Cross-table reuse is tracked separately.
- Every schema must declare at least one field with `"unique": true`. This is the business key (e.g. `employee_code`, `email`, `sku`). The `id` field does not qualify.
- System field names (`id`, `created_at`, `updated_at`, `created_by`, `updated_by`, `_state`) are forbidden in schema JSON.
- Table names must be unique across all plugins. The DDL linter blocks duplicates before any DDL runs.
- `arc db migrate` issues `CREATE TABLE IF NOT EXISTS` — idempotent, safe to re-run.
- Schemas only **create** tables. Modifying existing tables is done through patches.
-----
 
## 15. Patch Files
 
Patch files live in `{plugin}/patches/` and modify columns on **existing tables**. Any plugin can patch any table — `payroll` can add fields to `hrms`’s `Employee`.
 
```json
{
  "table": "Employee",
  "patch_id": "PAY001",
  "plugin": "payroll",
  "description": "Add payroll fields to Employee",
  "fields": [
    {
      "fld_id": "BA01",
      "field_name": "bank_account",
      "type": "Data",
      "max_length": 64
    },
    {
      "fld_id": "BA02",
      "field_name": "pay_grade",
      "type": "Data",
      "max_length": 16
    }
  ]
}
```
 
**Rules:**
 
- Patches **never** issue `CREATE TABLE` or `DROP TABLE`. Violation is an ERROR.
- `patch_id` is recorded in `_patch_history` on successful application and will never be re-run.
- If the target table does not exist, the patch is skipped with a warning.
- To drop a column, remove its `fld_id` from the patch fields and apply with `--confirm-destructive`. The column’s values are captured to `_trash` before the DROP.
- To rename a column, change `field_name` while keeping `fld_id` the same.
-----
 
## 16. Field Change Detection
 
When a patch is applied, the migrator diffs the patch’s field list against the live `_field_registry` snapshot for that table. The diff drives exactly what DDL to emit.
 
|Condition                                                          |Operation      |Destructive|
|-------------------------------------------------------------------|---------------|-----------|
|`fld_id` not in registry                                           |`ADD COLUMN`   |No         |
|`fld_id` exists, `field_name` changed, column present in DB        |`RENAME COLUMN`|No         |
|`fld_id` exists, `type`/`reqd`/`max_length` changed, name unchanged|`ALTER COLUMN` |No         |
|`fld_id` exists, name changed, column not found in DB              |`DROP + ADD`   |**Yes**    |
|`fld_id` in registry, absent from patch fields                     |`DROP COLUMN`  |**Yes**    |
 
For `RENAME` followed by a type change in the same patch, Arc emits `RENAME` first then `ALTER` — two separate statements.
 
Destructive operations (DROP COLUMN, DROP+ADD) require `--confirm-destructive`. Without it, the migration plan is printed but nothing is applied. All dropped column values are written to `_trash` with `drop_type = 'column'` before the column is removed.
 
-----
 
## 17. System Tables
 
Arc creates and manages three system tables before any user tables. They use underscore prefixes to distinguish them from user-defined tables.
 
### `_field_registry`
 
Tracks every column Arc has ever created or modified. This is the source of truth for field change detection.
 
|Column      |Type        |Description               |
|------------|------------|--------------------------|
|`fld_id`    |VARCHAR(4)  |Permanent field identifier|
|`table_name`|VARCHAR(255)|                          |
|`field_name`|VARCHAR(255)|Current column name       |
|`type`      |VARCHAR(32) |Arc field type            |
|`reqd`      |BOOLEAN     |NOT NULL constraint       |
|`max_length`|INTEGER     |For Data fields           |
|`plugin`    |VARCHAR(128)|Owner plugin              |
 
Rows are **inserted** on ADD, **updated** on RENAME or ALTER, and **deleted** on DROP. `fld_id` is immutable and may never be changed.
 
### `_patch_history`
 
Records every successfully applied `patch_id`. A patch is never run twice.
 
|Column       |Type                    |Description|
|-------------|------------------------|-----------|
|`patch_id`   |VARCHAR(128) PRIMARY KEY|           |
|`plugin`     |VARCHAR(128)            |           |
|`table_name` |VARCHAR(255)            |           |
|`applied_at` |TIMESTAMPTZ             |           |
|`description`|TEXT                    |           |
 
### `_trash`
 
Destination for all deletions — both row deletions (from API DELETE) and column drops. Never issue `DELETE` on user tables directly.
 
|Column       |Type                 |Description                                                                                             |
|-------------|---------------------|--------------------------------------------------------------------------------------------------------|
|`id`         |BIGSERIAL PRIMARY KEY|                                                                                                        |
|`table_name` |VARCHAR(255)         |                                                                                                        |
|`record_id`  |UUID                 |NULL for column drops                                                                                   |
|`drop_type`  |VARCHAR(16)          |`'row'` or `'column'`                                                                                   |
|`data`       |JSONB                |Full row for row drops; `{table, column, fld_id, type, reqd, max_length, values:[...]}` for column drops|
|`deleted_by` |VARCHAR(255)         |                                                                                                        |
|`deleted_at` |TIMESTAMPTZ          |                                                                                                        |
|`restored_at`|TIMESTAMPTZ          |Set by `arc db recover`                                                                                 |
 
-----
 
## 18. Migration Pipeline
 
```
arc db migrate
    │
    ├── 1. Read DB state (AUTOCOMMIT read connection)
    │       existing tables    ← information_schema.tables
    │       existing columns   ← information_schema.columns
    │       _field_registry    ← fld_id snapshots (column-aware read for upgrades)
    │       _patch_history     ← applied patch_ids
    │
    ├── 2. Build MigrationPlan (pure — zero DB writes)
    │       a. System objects   uuid_generate_v7(), _field_registry, _patch_history, _trash
    │       b. For each plugin in resolved order:
    │          → schemas: CREATE TABLE IF NOT EXISTS (new tables only)
    │          → patches: change detection → DDL + registry sync
    │          → record patch_id in _patch_history
    │       c. Run DDL linter → block on any ERROR, warn on warnings
    │
    ├── 3. Print lint issues (if any)
    │       ERROR = blocked, nothing applied
    │       WARNING = printed, migration continues
    │
    ├── 4. Execute plan (AUTOCOMMIT DDL connection)
    │       One text() call per statement
    │       (asyncpg rejects multi-statement strings)
    │
    └── 5. Print summary
```
 
**Two separate AUTOCOMMIT connections** — one for reading state, one for executing DDL. They are never mixed.
 
**`arc db plan`** runs steps 1–3 only and prints the DDL. Zero database writes.
 
-----
 
## 19. Backup, Restore, and Recovery
 
### Backup formats
 
**JSON (default)** — structured document with per-table column metadata and rows. Restore is **partial**: only columns present in the current live table are restored; missing tables are skipped. Survives schema drift between backup and restore time.
 
**SQL** — INSERT statements with a machine-readable `-- ARC-BACKUP-META:` header. Restore is **strict**: every table’s column set must exactly match the live schema. If any mismatch exists, restore is blocked with “schema mismatch — Restore Failed”. Use `arc db backup-convert` to turn a SQL backup into JSON for partial restore.
 
### Encryption
 
Backups can be encrypted with a passphrase. Arc uses Fernet (AES-128-CBC, PKCS7 padding) with a key derived via PBKDF2-HMAC-SHA256 (200,000 iterations, random 16-byte salt stored in the file header). The encryption passphrase comes from the `--key` flag or the env var named in `[plugins.db.backup] encryption_key_env`.
 
Never store the passphrase in `arc.toml`. Use the env var.
 
### Recovery
 
`arc db recover -t row --id <trash_id>` re-inserts a deleted row back into its original table.
 
`arc db recover -t column --id <trash_id>` re-adds a dropped column via ALTER TABLE, backfills every row’s value from the stored JSONB payload, optionally re-enforces NOT NULL, and re-registers the column in `_field_registry`.
 
### Cleanup
 
`arc db cleanup [--before ISO_DATE]` permanently purges `_trash` rows older than the cutoff. Default cutoff is `retention_days` from `arc.toml` (default 30 days). Use `--dry-run` to preview what would be purged.
 
-----
 
## 20. CLI Reference
 
### Core commands
 
|Command                       |Description                                                    |
|------------------------------|---------------------------------------------------------------|
|`arc init [DIR] [--name NAME]`|Scaffold `arc.toml` + `arc.lock` with db, api, http plugins    |
|`arc new-plugin NAME`         |Scaffold `{name}/` directory and register in `arc.lock`        |
|`arc run [--host] [--port]`   |Build and serve with Uvicorn                                   |
|`arc doctor`                  |Resolve the plugin graph and print startup order + capabilities|
|`arc clear-cache`             |Reset import cache, structlog cache, importlib path cache      |
 
### arc db
 
|Command                                                     |Description                           |
|------------------------------------------------------------|--------------------------------------|
|`arc db migrate [--confirm-destructive]`                    |Apply schemas and patches             |
|`arc db plan [--confirm-destructive]`                       |Preview DDL without applying          |
|`arc db status`                                             |List managed tables and field registry|
|`arc db connect`                                            |Open interactive psql shell           |
|`arc db backup [-p PLUGIN] [--format json|sql] [--key K]`   |Back up to `backups/`                 |
|`arc db restore [PATH] [-p PLUGIN] [--format] [--key K]`    |Restore from backup                   |
|`arc db recover -t row|column --id TRASH_ID`                |Recover from `_trash`                 |
|`arc db cleanup [-p PLUGIN] [--before ISO_DATE] [--dry-run]`|Purge old trash                       |
|`arc db backup-convert PATH [--key K] [--out PATH]`         |Convert SQL backup to JSON            |
 
### arc api
 
|Command         |Description                                     |
|----------------|------------------------------------------------|
|`arc api routes`|List every HTTP route the API plugin would serve|
 
-----
 
## 21. Context System
 
Arc uses `contextvars` to carry per-request state without threading it through function signatures. Any plugin can read the current request and user from anywhere in the call stack.
 
```python
from arc.kernel.context import get_request, get_user
 
request = get_request()   # RequestContext | None
user    = get_user()      # UserContext | None
```
 
**RequestContext:** `id`, `method`, `path`, `started_at`
 
**UserContext:** `id`, `email`, `roles`, `permissions`, `is_superuser`
 
The context is set by request middleware (to be built in Phase 4/5) and cleared at the end of each request. System fields `created_by` and `updated_by` are populated from `UserContext.id` by the API layer.
 
There is no `TenantContext`. Arc is single-tenant.
 
-----
 
## 22. Lifecycle Hooks
 
```
STARTUP (forward order)
  for each plugin in resolved order:
    startup_check()    → fail on critical plugin aborts boot
    startup()          → open connections
    ready()            → cross-plugin wiring (all plugins started)
 
SHUTDOWN (reverse order)
  for each plugin in reverse:
    shutdown()         → errors collected, never re-raised
```
 
A critical plugin’s failed `startup_check` raises `StartupError` and the application does not start. A non-critical plugin’s failed check is logged as a warning and the app continues.
 
-----
 
## 23. Health Checks
 
`GET /health` aggregates `health_check()` from every plugin. If all pass, the response is HTTP 200 `{"status": "ok"}`. If any fail, HTTP 503 `{"status": "degraded"}`.
 
```python
async def health_check(self) -> CheckResult:
    try:
        # Check this plugin's runtime dependency
        async with self._session() as s:
            await s.execute(text("SELECT 1"))
        return CheckResult.ok("db reachable")
    except Exception as exc:
        return CheckResult.fail(str(exc))
```
 
`CheckResult` has three states: `.ok(msg)`, `.warn(msg)`, `.fail(msg)`. A `warn` does not degrade the health endpoint; a `fail` does.
 
-----
 
## 24. Key Rules and Invariants
 
These rules must never be violated, by framework code or user plugins.
 
|# |Rule                                                                                                                                  |
|--|--------------------------------------------------------------------------------------------------------------------------------------|
|1 |Never name a file the same as a Python stdlib module. Use `logger.py`, not `logging.py`.                                              |
|2 |`fld_id` is permanent. Never reassign, never reuse — even after the column is dropped.                                                |
|3 |System field names (`id`, `created_at`, `updated_at`, `created_by`, `updated_by`, `_state`) must never appear in schema or patch JSON.|
|4 |Every schema must declare at least one field with `"unique": true`. The `id` column does not qualify.                                 |
|5 |Table names must be unique across all plugins. Duplicates are a DDL linter ERROR.                                                     |
|6 |Never issue `DELETE` on user tables directly. Use the API DELETE path which moves rows to `_trash`.                                   |
|7 |Destructive operations (DROP COLUMN, DROP+ADD) require `--confirm-destructive`.                                                       |
|8 |DDL must always execute with `AUTOCOMMIT` isolation level. Never mix DDL and DML on the same connection.                              |
|9 |`arc db migrate` is idempotent. Safe to run multiple times.                                                                           |
|10|Timestamps are always stored in UTC. Never set `created_at`/`updated_at` from Python — let the DB default handle it.                  |
|11|Click must stay pinned at `8.1.8`. Typer 0.16 is incompatible with Click 8.2+.                                                        |
|12|`plan()` in the migrator is pure — it never touches the database. Only `execute()` writes.                                            |
|13|One `text()` call per SQL statement. asyncpg rejects multi-statement strings in prepared statements.                                  |
|14|Never import domain libraries (SQLAlchemy, Starlette) in the kernel. The kernel is domain-free.                                       |
|15|`arc.lock` entrypoint is `"module.path:ClassName"`. The colon is mandatory.                                                           |
 
-----
 
## 25. Writing a Plugin
 
### 1. Scaffold
 
```bash
arc new-plugin finance
```
 
This creates:
 
```
finance/
├── schemas/
├── patches/
├── resources/
│   └── __init__.py
├── tests/
├── __init__.py
└── plugin.py
```
 
And adds the entry to `arc.lock`.
 
### 2. Define tables
 
`finance/schemas/Invoice.json`:
 
```json
{
  "table": "Invoice",
  "plugin": "finance",
  "fields": [
    {"fld_id": "FA01", "field_name": "invoice_no", "type": "Data",
     "reqd": true, "max_length": 32, "unique": true},
    {"fld_id": "FA02", "field_name": "customer", "type": "Link",
     "link_table": "Customer", "reqd": true},
    {"fld_id": "FA03", "field_name": "total_amount", "type": "Decimal",
     "reqd": true},
    {"fld_id": "FA04", "field_name": "due_date", "type": "Date"}
  ]
}
```
 
### 3. Expose via REST
 
`finance/resources/invoice.py`:
 
```python
from arc.plugins.api import Resource
 
invoice = Resource(
    plugin="finance",
    table="Invoice",
    fields=["invoice_no", "customer", "total_amount", "due_date"],
    create_fields=["invoice_no", "customer", "total_amount", "due_date"],
    page_size=50,
)
```
 
### 4. Plugin class (for most plugins, contribute() stays empty)
 
`finance/plugin.py`:
 
```python
from arc.kernel import Plugin, CheckResult
 
class FinancePlugin(Plugin):
    requires = ("db.session",)
    load_order = 100
 
    @property
    def name(self):
        return "finance"
 
    @property
    def version(self):
        return "1.0.0"
 
    def contribute(self, rt) -> None:
        # schemas/ and resources/ are auto-discovered — no registration needed.
        # Add custom CLI commands or capabilities here if required.
        pass
 
    async def health_check(self) -> CheckResult:
        return CheckResult.ok()
```
 
### 5. Migrate and run
 
```bash
arc db plan            # preview
arc db migrate         # apply
arc api routes         # verify endpoints
arc run                # serve
```
 
### 6. Providing capabilities to other plugins
 
If your plugin offers a service others need:
 
```python
def setup(self, rt) -> None:
    from finance.ledger import LedgerService
    rt.capabilities.provide("finance.ledger", instance=LedgerService(), source=self.name)
```
 
Another plugin then declares `requires = ("finance.ledger",)` and calls `rt.capabilities.require("finance.ledger")`.
 
-----
 
## 26. Phased Roadmap
 
### Completed
 
|Phase |Name                       |Deliverables                                                                                                                                                                                                                                         |
|------|---------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
|**1** |Core Framework             |Micro-kernel, Plugin ABC, capability container, extension registry, resolver, loader, lifecycle, orchestrator, `arc.toml` config, context system, CLI, `arc.lock` v2 format                                                                          |
|**2a**|Database Layer — Core      |UUID v7 `id`, system tables (`_field_registry`, `_patch_history`, `_trash`), schema compiler with unique-key rule, patch compiler with 5-op change detection, DDL linter, full migration pipeline, duplicate-table fast-fail, column-drop to `_trash`|
|**2b**|Database Layer — Operations|Backup/restore (JSON + SQL, encryption), partial JSON restore, strict SQL restore with schema validation, SQL→JSON converter, row/column recovery from `_trash`, trash cleanup, `arc db connect`                                                     |
 
### Upcoming
 
-----
 
#### Phase 3 — API Engine
 
The current `Resource` is a skeleton with list, get, and create only. Phase 3 completes the REST surface.
 
**Planned features:**
 
- `permissions` — role-to-method mapping. `{"HR Manager": ["get","post","patch"], "Guest": ["get"]}`.
- `addon_fields` — extra fields visible only to specific roles.
- `filters.mandatory` — always applied regardless of query params.
- `filters.static` — fixed conditions (`{"_state": {"neq": 99}}`).
- `filters.optional` — querystring-driven filters with 14 operators (`eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `like`, `ilike`, `in`, `nin`, `is_null`, `not_null`, `between`, `range`).
- `query` — full-text search across declared fields (`?q=search+term`).
- `resource_name` — custom URL slug instead of the table name.
- `PATCH` endpoint — partial update of declared writable fields.
- `DELETE` endpoint — moves row to `_trash` (never issues raw DELETE).
- `ETag` / `304 Not Modified` — SHA-256 response hash caching.
- Cursor-based pagination — stateless, stable across concurrent updates.
- OpenAPI 3.1 spec auto-generated from the resource registry.
- `arc api routes` showing method, path, permissions, and filters.
-----
 
#### Phase 4 — Authentication
 
- JWT access + refresh token pair.
- API key authentication (header and query param).
- OAuth2 / OIDC provider integration.
- MFA (TOTP).
- Token revocation list.
- `UserContext` populated by auth middleware before any handler runs.
- `arc auth` CLI group.
-----
 
#### Phase 5 — Permissions
 
- RBAC — roles assigned to users, permissions assigned to roles.
- Field-level permissions — hide or make read-only individual fields per role.
- Row-level permissions — compile role policy into a SQL WHERE clause appended to every query. Zero application-layer filtering; enforcement happens in PostgreSQL.
- Policy compiler — declarative policy JSON → SQL fragment, compiled at startup not per-request.
- Superuser bypass — `UserContext.is_superuser = True` skips all checks.
-----
 
#### Phase 6 — Event System
 
- In-process event bus — `emit(event_name, payload)` / `on(event_name, handler)`.
- Transactional outbox — events written to DB in the same transaction as the triggering change; a background worker delivers them. Guarantees at-least-once delivery.
- Broker integration — plug in Redis Streams, RabbitMQ, or Kafka as a backend for cross-process delivery.
- Built as a plugin; the event bus capability is `"event.bus"`.
-----
 
#### Phase 7 — Queue
 
- Background task execution with retry logic and exponential backoff.
- Dead-letter queue for tasks that exhaust retries.
- Priority queues.
- Task result storage.
- `arc queue` CLI — inspect pending, running, and failed tasks.
- Backends: PostgreSQL (default, no extra infra), Redis, SQS.
-----
 
#### Phase 8 — Scheduler
 
- Cron expression and fixed-interval job scheduling.
- Leader election — in a multi-worker deployment, only one worker runs each scheduled job.
- Job history and next-run preview.
- `arc scheduler` CLI.
-----
 
#### Phase 9 — Email
 
- Transactional email with provider abstraction.
- Supported providers: SMTP, Resend, AWS SES.
- Queue-backed sending — emails enqueue in Phase 7’s queue, not sent inline.
- Template system (Jinja2).
- Bounce / complaint handling.
- `arc email` CLI — test send, queue status.
-----
 
#### Phase 10 — Notification
 
- Multi-channel alerts: in-app, email (Phase 9), SMS, push.
- User notification preferences (opt-in/out per channel per event type).
- Webhook delivery with HMAC signing and retry.
- Notification inbox API.
-----
 
#### Phase 11 — Workflow
 
- State machine engine — tables with `_state` field transition through declared states.
- Transition guards — `_state` value checked before allowing API write.
- Approval chains — multi-step approval workflows with assignees.
- Transition hooks — `on_enter`, `on_exit`, `on_reject` async callbacks.
- Workflow declarations in schema JSON or a dedicated `workflow.json` per plugin.
-----
 
#### Phase 12 — Storage
 
- File and object storage with provider abstraction.
- Providers: local disk (`storage.local`), AWS S3 (`storage.cloud`), GCS, Azure Blob, Cloudflare R2.
- Signed URLs for direct client upload and download.
- Image processing: resize, format conversion, thumbnail generation.
- Storage plugins provide the `storage.local` and `storage.cloud` capabilities; user plugins consume them via `requires`.
-----
 
#### Phase 13 — Search
 
- Full-text search using PostgreSQL `tsvector` / `tsquery` (no external service required for basic use).
- Faceted filtering — count results per filter value.
- Per-plugin search indices — each plugin declares which fields to index.
- Optional Elasticsearch / OpenSearch backend for large datasets.
- `?q=` search integrated into Phase 3 Resource filtering.
-----
 
#### Phase 14 — Monitoring
 
- OpenTelemetry tracing — spans for every request, DB query, and background task.
- Prometheus metrics endpoint (`/metrics`).
- Structured audit log — every write operation recorded with user, timestamp, before/after values.
- `arc monitor` CLI — tail live logs, query audit log.
-----
 
#### Phase 15 — Realtime
 
- WebSocket support via Starlette.
- Server-Sent Events (SSE) for one-way streaming.
- Presence tracking — which users are online, last seen.
- Channel-based pub/sub — plugins publish to named channels, clients subscribe.
-----
 
#### Phase 16 — Import / Export
 
- CSV, Excel (xlsx), and JSON bulk import with validation against the schema.
- Conflict handling on import: skip, overwrite, or error.
- Bulk export — full table or filtered subset.
- Background processing for large files (Phase 7 queue).
- `arc import` and `arc export` CLI.
-----
 
#### Phase 17 — Production Tooling
 
- Dockerfile and `docker-compose.yml` templates.
- Helm chart for Kubernetes deployment.
- `arc pull-plugin NAME` — clone a plugin from a git URL and register in `arc.lock`.
- `arc rm-plugin NAME` — deregister and optionally remove a plugin.
- `arc upgrade` — check for framework and plugin updates.
- Automated pre-upgrade backup before destructive migrations.
- Backup retention management.
-----
 
#### Phase 18 — Enterprise
 
- SAML 2.0 SSO integration.
- SCIM 2.0 user provisioning.
- Secret rotation — roll API keys and JWT signing keys without downtime.
- Software Bill of Materials (SBOM) generation.
- Compliance audit log export (GDPR / SOC 2 format).
-----
 
## Future Development Guidelines
 
When building any new phase or feature, these principles must be respected:
 
**Never add domain knowledge to the kernel.** The kernel (`arc/kernel/`) must remain domain-free. It imports nothing from `arc/plugins/`. New domains belong in new plugins.
 
**New services are capabilities.** Any cross-plugin service (event bus, cache client, storage client) must be provided as a named capability. Consumer plugins declare `requires`. Nobody imports the provider’s module directly.
 
**New shared surfaces are extension points.** If multiple plugins need to contribute to a shared collection (event handlers, background tasks, search indices), add a new named point to `arc.kernel.registry.Points` and document it.
 
**One statement per SQL execute.** asyncpg does not accept multi-statement strings. Every SQL string passed to `conn.execute(text(...))` must be a single statement.
 
**Migrations are compile-ahead.** New system tables, new schema features, and new system functions go through `arc db migrate`. Nothing about the schema is interpreted at request time.
 
**Backups before destructive changes.** Any operation that drops data (DROP COLUMN, DROP TABLE, bulk delete) should optionally trigger an automatic backup of the affected tables. This was established in Phase 2b and must be respected going forward.
 
**Tests for each phase.** Every new phase must ship with a test suite that covers the pure logic (no live database) and documents the expected behaviour. Integration tests requiring Postgres are clearly separated.
 
-----
 
*Arc Framework v2 — Single-tenant · Python 3.12+ · PostgreSQL 16+ · asyncpg only*