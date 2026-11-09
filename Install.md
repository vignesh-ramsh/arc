# Installing ARC

ARC is a plugin platform: a small Python **kernel** (the `arc` command) plus **plugins** that each add one capability — database access, an HTTP server, user accounts, background jobs, and so on. This guide takes a blank Linux machine to a running ARC with a working **Admin Console** in your browser.

```
Browser ──HTTP──► gateway (:8001) ──► relay ──► pgdb ──► PostgreSQL
                      │                 │
                      │                 └─► redix ──► Redis / Valkey   (cache, locks, rate limits)
                      └─ serves /admin
                                       lineup workers ◄── Redis / Valkey   (background jobs)
```

Most of the time is spent waiting on package downloads; the ARC steps themselves take seconds.

## Steps

1. [Install the system packages](#1-install-the-system-packages)
2. [Install uv and Python 3.12](#2-install-uv-and-python-312)
3. [Install the `arc` command](#3-install-the-arc-command)
4. [Create the PostgreSQL role and database](#4-create-the-postgresql-role-and-database)
5. [Create your project](#5-create-your-project)
6. [Install the plugins](#6-install-the-plugins)
7. [Create the tables](#7-create-the-tables)
8. [Create the first admin user](#8-create-the-first-admin-user)
9. [Build the Admin UI](#9-build-the-admin-ui)
10. [Start ARC](#10-start-arc)

Then: [verify](#verify-everything), [keep it running and put it on the internet](#keep-it-running-and-put-it-on-the-internet), [troubleshooting](#troubleshooting), [updating](#updating).

---

## What you need

| Component | Needed for | Version |
|---|---|---|
| Linux | everything | Ubuntu 22.04 tested. Windows: use WSL2 (native Windows is not tested). |
| Python | the kernel and every plugin | **3.12 or newer** (`uv` can fetch it for you) |
| [uv](https://docs.astral.sh/uv/) | builds the project environment (`arc init` / `arc install` call `uv sync`); supplies Python 3.12 if needed | any recent version |
| git | `arc init` and `arc install` clone repositories | any |
| PostgreSQL | all data (the `pgdb` plugin) | **14 tested.** 13+ should work, see the note in step 4. |
| Redis or Valkey | caching, locks, rate limiting, background jobs (the `redix` and `lineup` plugins) | Redis 6.0 tested. Valkey is supported but was not tested. |
| Node.js + Yarn 1 | **only** to build the Admin UI | Node 20 (the UI pins `20.20.2` in `.nvmrc`), Yarn 1.x ("classic") |

You do not need a compiler or any `-dev` libraries: every Python dependency installs from a prebuilt wheel.

**How this guide was checked.** The steps below were run from an empty directory against a freshly initialised, private PostgreSQL 14.24 and Redis 6.0.16, on Ubuntu 22.04 with Python 3.12, Node 20.20.2 and Yarn 1.22.22 — ending in a real login and successful calls to the Admin API. In that run the repositories were cloned from local copies confirmed to be at the same commits as GitHub's `main`. Step 3 was then checked separately against the published PyPI package, version 0.1.1: `pip install arc-kernel` inside a Python 3.12 virtualenv, `uv tool install arc-kernel`, and the failure quoted there on Python 3.10; then `arc init` with the kernel cloned straight from GitHub (it wrote `.vscode/settings.json` and `.gitignore` excluded the settings store), `arc install` of `pgdb` from GitHub, and `arc doctor` (the later steps were not repeated on that second project). Things that were **not** run are marked *(not run)*: `arc deploy prod` (system-wide units, nginx and certbot — its units were checked with `systemd-analyze verify` and unit tests only), the server-wide `setup.sh`, Valkey, and every OS other than Ubuntu. `arc deploy dev` was run against a scratch project and driven through crash tests under a real `systemd --user` manager, and `arc deploy dev --enable` was used to switch a running server from the old single `arc run` unit (about 9 seconds of downtime for the switch).

---

## 1. Install the system packages

On Ubuntu or Debian:

```bash
sudo apt update
sudo apt install -y git curl postgresql redis-server
```

Make sure both servers are running:

```bash
sudo systemctl enable --now postgresql redis-server
pg_isready
redis-cli ping
```

`pg_isready` should say `accepting connections` and `redis-cli ping` should answer `PONG`.

> **Other systems** *(not run)*. ARC needs PostgreSQL's **`pgcrypto`** extension. On Debian and Ubuntu it ships inside the main `postgresql` package. On Fedora/RHEL install `postgresql-contrib` as well. On macOS, `brew install postgresql@14 redis` works the same way. For Valkey, install it in place of Redis — ARC talks to either, and `arc redix connect` accepts `redis-cli` or `valkey-cli`.

**Node.js and Yarn** — only needed for step 9. Install Node 20 from your package manager, [nodesource](https://github.com/nodesource/distributions) or [nvm](https://github.com/nvm-sh/nvm), then:

```bash
npm install -g yarn
```

The Admin UI's lockfile is Yarn 1 (`yarn.lock` begins `# yarn lockfile v1`), so install Yarn **1.x**, not Yarn Berry.

## 2. Install uv and Python 3.12

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new shell (or follow the installer's PATH hint), then:

```bash
uv --version
uv python install 3.12
```

`uv` uses a system Python 3.12 if there is one and downloads its own otherwise, so this step is safe either way.

You need `uv` even though step 3 uses `pip`: `arc init` and `arc install` run `uv sync` to build the project's environment. It is also what supplies Python 3.12 on a machine whose own Python is older.

## 3. Install the `arc` command

The `arc` command is published on PyPI as **`arc-kernel`**:

```bash
pip install arc-kernel
arc --help
```

**The `pip` has to belong to Python 3.12 or newer** — the package declares `Requires-Python >=3.12`. Check with `pip --version`: it should end in `(python 3.12)` or later. On Ubuntu 22.04, whose default Python is 3.10, pip refuses with:

```
ERROR: Ignored the following versions that require a different python version: 0.1.0 Requires-Python >=3.12; 0.1.1 Requires-Python >=3.12
ERROR: Could not find a version that satisfies the requirement arc-kernel (from versions: none)
ERROR: No matching distribution found for arc-kernel
```

That is not a broken package, just a pip running an older Python. Use the Python 3.12 that `uv` gave you in step 2, in either of these ways:

```bash
# a) a virtualenv, then plain pip inside it
uv venv --seed --python 3.12 ~/.arc-cli
source ~/.arc-cli/bin/activate
pip install arc-kernel

# b) or let uv do both in one step and put `arc` on your PATH permanently
uv tool install arc-kernel
```

If your shell says `arc: command not found` after (b), run `uv tool update-shell` and open a new shell. Avoid `sudo pip install arc-kernel` into the system Python: recent distributions (Ubuntu 24.04, Debian 12) mark it "externally managed" and refuse global `pip install`s *(not run)*, which is what the virtualenv in (a) sidesteps.

Use version **0.1.1 or newer** (`pip show arc-kernel`; upgrade with `pip install -U arc-kernel`). 0.1.0 is older than the kernel `arc init` clones, and its `arc init` writes a `.gitignore` that does not exclude `.arc/arc.store.db`, the encrypted settings store.

This global `arc` only bootstraps projects. `arc init` (step 5) clones the kernel into your project, and from then on the project's own copy is the `arc` you use.

> Setting up a shared server where several people will each create projects? `setup.sh` in this repository installs `uv`, Python 3.12 and `arc` system-wide for every user *(not run)*:
> `sudo ARC_KERNEL_REPO=https://github.com/vignesh-ramsh/arc.git ./setup.sh`

## 4. Create the PostgreSQL role and database

Create a dedicated role and a database **owned by that role**:

```bash
sudo -u postgres psql <<'SQL'
CREATE ROLE arc WITH LOGIN PASSWORD 'replace-with-a-strong-password';
CREATE DATABASE arc OWNER arc ENCODING 'UTF8';
SQL
```

Generate the password with `openssl rand -hex 16` so it contains nothing that needs escaping inside the SQL string. (ARC itself handles special characters fine — this only protects the line above.)

**The `OWNER arc` part matters.** On first migration ARC runs `CREATE EXTENSION IF NOT EXISTS pgcrypto`. On PostgreSQL 14 `pgcrypto` is marked `trusted = true` (you can see it in `pgcrypto.control`), so the database's owner can install it with no superuser rights. This was verified in both directions:

- role owns the database → works, no superuser needed
- database owned by someone else → fails with `permission denied to create extension "pgcrypto"`

ARC does not need a superuser, `CREATEDB`, or `CREATEROLE` — and you should not grant them. Only PostgreSQL 14 was tested. Trusted extensions were introduced in PostgreSQL 13, so 13 should behave the same, but on 12 and older you would need a superuser to create the extension first.

Check that the role can log in over TCP:

```bash
PGPASSWORD='replace-with-a-strong-password' psql -h 127.0.0.1 -U arc -d arc -c 'select 1'
```

If that fails, see [Troubleshooting](#troubleshooting).

## 5. Create your project

```bash
arc init myproject --kernel-repo https://github.com/vignesh-ramsh/arc.git
cd myproject
source .venv/bin/activate
```

Run this as your normal user, not with `sudo`. To avoid typing the URL every time, `export ARC_KERNEL_REPO=https://github.com/vignesh-ramsh/arc.git` in your shell profile.

`arc init` creates:

| Path | What it is |
|---|---|
| `arc/` | the kernel source, cloned from `--kernel-repo` |
| `plugins/` | where plugins are installed |
| `.arc/` | project config: `arc.toml`, `plugins.lock`, the encrypted settings store `arc.store.db`, and the master key `arc.mkey` |
| `config/`, `logs/`, `backups/` | per-environment overrides, log files, backup targets |
| `pyproject.toml`, `.venv/` | a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) tying the kernel and every plugin into one virtual environment |
| `.gitignore` | already excludes the master key, the settings store, `.venv/`, `arc/` and `plugins/*/` |

> **Back up `.arc/arc.mkey`.** It encrypts every secret ARC stores, including your database password. Lose it and those secrets cannot be recovered — ARC will report `Could not decrypt secret … wrong master key, or the store is corrupt`. `arc init` keeps it out of git on purpose; keep a copy somewhere safe.

## 6. Install the plugins

Every plugin is its own public GitHub repository. Install them in this order, **configuring each service right after the plugin that talks to it** — that keeps the installer quiet (see the note below the commands).

```bash
# PostgreSQL access
arc install https://github.com/vignesh-ramsh/psqldb.git --name pgdb
arc pgdb setup

# Redis / Valkey access
arc install https://github.com/vignesh-ramsh/redix.git
arc redix setup

# everything else
arc install https://github.com/vignesh-ramsh/relay.git
arc install https://github.com/vignesh-ramsh/gateway.git
arc install https://github.com/vignesh-ramsh/lineup.git
arc install https://github.com/vignesh-ramsh/filer.git
arc install https://github.com/vignesh-ramsh/authn.git
arc install https://github.com/vignesh-ramsh/mail.git
arc install https://github.com/vignesh-ramsh/admin.git
```

> **The pgdb repository is called `psqldb`** — a leftover from before the plugin was renamed. Always pass `--name pgdb`. Without it the folder becomes `plugins/psqldb`, and `arc update` then silently skips the plugin (it looks for `plugins/pgdb`, finds nothing, and reports `skipped_no_git`) so pgdb would never receive updates.

`arc pgdb setup` asks for the details of the role you created in step 4, tests the connection, and only then saves it:

```
Host [localhost]: 127.0.0.1
Port [5432]:
Database name: arc
User: arc
Password (blank for none):
Connecting to 127.0.0.1:5432/arc...
Connected — PostgreSQL 14.24 ...
```

(The password is typed blind — nothing is echoed.)

`arc redix setup` is the same for Redis: host `127.0.0.1`, port `6379`, DB index `0`, and leave user and password blank unless you configured them.

Both commands store the connection string **encrypted** in `.arc/arc.store.db`.

**What each plugin does**

| Plugin | Purpose | Needs |
|---|---|---|
| `pgdb` | PostgreSQL access, schema migrations, soft-delete and audit trail | — |
| `redix` | Redis/Valkey client: cache, distributed locks, rate limiting | — |
| `relay` | CRUD, hooks and the whitelisted-API layer everything else is built on | `pgdb` |
| `gateway` | the HTTP and WebSocket server | — |
| `lineup` | background-job workers and the cron scheduler | `redix`, `pgdb` |
| `filer` | file uploads and storage (local disk or S3) | `pgdb`, `relay` |
| `authn` | users, roles, sessions, login, access keys | `pgdb`, `relay` |
| `mail` | templated email over SMTP | `pgdb`, `relay` |
| `admin` | the Admin Console: web UI and API | `relay`, `gateway`, `authn`, `filer`, `pgdb` |

Install all nine; that is the configuration this guide was tested with. Redis is technically optional for the Admin Console — without `redix`, ARC falls back to in-process caching and locking, and login rate limiting becomes per-worker rather than shared — but you should run it. Background jobs (`lineup`) require it.

**Why the order matters.** After every install ARC regenerates IDE type stubs, which needs a successful boot. A boot fails until `pgdb_dsn` (and `redix_url`) are set, so installing `pgdb` and *then* configuring it avoids a `Warning: arc stubs failed to regenerate IDE stubs` on every later install. The warning is harmless — just noise — if you do it in another order.

## 7. Create the tables

```bash
arc pgdb plan
arc pgdb migrate
```

`plan` previews every change without writing to the database. On a fresh database it lists only additions — `CREATE TABLE` lines, plus a few `ADD COLUMN` lines that plugins like `filer` add to their own tables — each marked `[safe]`. `migrate` shows the same plan, asks for confirmation, then applies it (add `--yes` for scripts). Expect `Migration complete.` and about 20 tables.

Migrate also writes the SQL it ran to `migrations/*.sql` inside each plugin's folder. Those files are git-ignored.

## 8. Create the first admin user

```bash
arc authn create-user --email you@example.com --superuser
```

It prompts for a password (hidden, with confirmation). Passwords are scored with [zxcvbn](https://github.com/dropbox/zxcvbn) and must reach a minimum score (`authn_min_password_score`, default 3 out of 4), so something like `Password1!` will be refused. To have ARC generate a strong one instead and print it **once**:

```bash
arc authn create-user --email you@example.com --superuser --random-password
```

`--superuser` creates the `Superuser` role if it does not exist and grants it. It bypasses every role check, so give it only to administrators — ARC prints a warning to that effect.

## 9. Build the Admin UI

The Admin UI is a React app that has to be built once. It lives in `plugins/admin/admin/ui`:

```bash
cd plugins/admin/admin/ui
yarn install --frozen-lockfile
yarn build
cd ../../../..
```

> **Do this instead of `arc build` on a fresh machine.** `arc build` runs `npm run build` and never installs dependencies first, so on a new checkout it stops with `sh: 1: tsc: not found`. Installing with Yarn first, as above, is what the lockfile is written for.

If you skip this step ARC still runs and the Admin **API** works, but `/admin` returns 404. ARC records a boot advisory saying `admin: UI not built` (read from the plugin's source; this guide's test run always built the UI first).

## 10. Start ARC

```bash
arc run
```

```
gateway: 8 worker(s) → http://127.0.0.1:8001
A queue used only through an ad hoc arc.relay.enqueue(fn, queue="...") call and never declared is not consumed — declare it with arc.relay.declare_queue(), or list every queue with `arc settings set lineup_queues a,b`.
lineup worker: 8 process(es) (auto-discovered queues: default, mail)
lineup scheduler: 1 (fixed — never scaled, would double-fire cron jobs)
```

`arc run` supervises the HTTP server, the job workers and the scheduler as one unit. Open **http://127.0.0.1:8001/admin** and sign in with the email and password from step 8. `Ctrl-C` stops everything.

Nothing needs configuring for background jobs: the workers consume every queue the installed plugins declare, which is the `auto-discovered queues` list above — `default`, plus `mail` for outgoing email. You only touch this if **your own** code enqueues onto a queue of its own name. Either declare it in a plugin's `register()` with `arc.relay.declare_queue("name")`, or list every queue by hand with `arc settings set lineup_queues default,mail,name`. A `lineup_queues` value *replaces* discovery, so always include `default` and `mail` in it.

The worker counts are sized to your machine's CPU and memory (the automatic choice never exceeds 8; a number you set yourself is used as given, so a 32-core server can run 32 web workers: `arc settings set gateway_workers 32`). On a small VM you can lower them: `arc settings set gateway_workers 2` and `arc settings set lineup_workers 2`. To use another port, `arc run --port 9000`.

---

## Verify everything

```bash
arc doctor
arc pgdb status
arc redix status
arc health
```

- `arc doctor` prints the plugin load order and ends with **`Boot resolution OK.`** Two lines starting `warning: optional ordering skipped` are normal and expected — they describe a harmless dependency cycle, not a problem.
- `arc pgdb status` and `arc redix status` should each print `OK` with a latency.
- `arc status` (once you have deployed with `arc deploy`) shows every process that should be running and whether it is; it exits 1 if anything is off.
- `arc health` lists each capability with `ok: yes`. For a deeper check that really opens connections and reports pool sizes and settings drift, use `arc doctor --deep`.

From another terminal, a quick end-to-end check:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/openapi.json   # 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/whoami          # 403 until you log in
```

A `403` on `/whoami` before logging in is correct, not a fault.

**Health endpoints, for a load balancer or watchdog.** Both are unauthenticated and are answered before anything else runs, so they leave no access-log line and no request metrics, are never rate-limited, and keep answering during maintenance mode:

```bash
curl -s http://127.0.0.1:8001/healthz   # {"status":"ok"}  - this process is up; no I/O at all
curl -s http://127.0.0.1:8001/readyz    # {"status":"ok","checks":{"pgdb":"ok","redix":"ok"}}
```

`/readyz` answers 503 (`{"status":"unavailable","checks":{"redix":"failed"}}`) when PostgreSQL or Redis does not answer within 2 seconds. It says only `ok`, `failed` or `timeout` per check, never an error message or a server version; the reason goes to the log, once per change. Use `/healthz` to decide whether to *restart* a process and `/readyz` to decide whether to *send it traffic*: a dead database is a reason to stop routing to a web worker, not to kill it. `HEAD` works too. The answer is shared for one second, so polling it hard costs the database one check per second per worker, not one per request.

---

## Keep it running and put it on the internet

`arc run` stops when you close the terminal, and if one of its processes keeps crashing it stops everything. For a server people use, let systemd supervise **each process on its own**. Both commands below install one unit per process — the log writer, the gateway, the background-job workers (one set per queue), the scheduler — grouped under `arc-<project>.target`. systemd restarts exactly the process that died; a process that keeps crashing ends as one `failed` unit (5 starts in 120 s) while the rest keep running. In testing against a real `systemd --user` manager, killing a job worker, a web worker, the gateway or the log writer never took the site down for longer than the gateway's own restart (about 4.5 s when the gateway master itself dies), and the other units' restart counters stayed at 0. The user-level commands were run; the system-wide ones need root, so they were checked with systemd's own `systemd-analyze verify` and unit tests, but not run *(not run)*.

**Development or a small server: a per-user service** — no root needed:

```bash
arc deploy dev --enable
```

This runs **2 web workers, 1 worker per queue and 1 scheduler**, keeps them running across logouts and reboots, and sets `restart_command` so `arc restart` works. Without `--enable` it only writes the units and never changes what is running.

**Production: system-wide units plus nginx and HTTPS** — needs root:

```bash
sudo "$(command -v arc)" deploy prod --domain app.example.com
```

This writes an nginx site for that domain that proxies to the app (closing `/metrics` to everyone but this machine - see *Before going public* - and allowing each visitor address 10 logins per second, with a burst of 50, before nginx answers `429` (password hashing is memory-hungry, so one source must not be able to use it all; see *An office behind one address* below to change or exempt it). Both assume nginx sees each visitor's real address, so behind a CDN or another proxy configure nginx's `real_ip` first. A site written by an earlier version has neither rule, so run the command again) and obtains a Let's Encrypt certificate with certbot (`--no-ssl` for plain HTTP; `--port` to change the app's internal port, default 8001). It then installs the same units under `/etc/systemd/system`, run as the project's owner (never root), started on boot, **sized from the machine**: as many web workers as the CPU and memory allow (never more than 8 automatically), and the same number of job workers in total, shared out over the queues with at least one each. The app starts last, so it comes up with the final settings. `arc restart` runs `sudo systemctl restart arc-<project>.target`, so the project's owner needs passwordless sudo for exactly that command. It only touches this project's own units and nginx site. Run it from inside your project with the virtualenv active: `sudo` does not inherit your `PATH`, and `"$(command -v arc)"` hands it the full path to the project's copy.

**Body size and timeouts are set per route.** `arc deploy prod` does not give the whole site one figure. It boots the project once (as the project's owner, not root) and asks the gateway what its routes declare (`arc gateway routes --json`). Every path then gets the gateway's own body ceiling (`gateway_max_body_bytes`, 10 MB) and an ordinary timeout of 60 seconds. Only the routes that declare a larger `max_body_bytes` (an upload route: `/files/upload` takes 60 MB) get a location of their own with that ceiling, and only WebSocket routes get the `Upgrade` headers and a one-hour idle timeout. It prints what it scoped (*"bodies up to 10 MB everywhere, up to 60 MB on 3 upload path(s); 1 WebSocket path(s) …"*). The site written by earlier versions allowed 100 MB everywhere (ten times what the gateway accepts, buffered by nginx before it is refused) and kept every connection open for an hour, so one hung request held a connection for an hour. The gateway still enforces each route's exact limit; nginx only has to not refuse first.

The limits are written into the site file when the command runs, so **run `arc deploy prod` again after you add an upload route or a WebSocket, or change `gateway_max_body_bytes`**; until then nginx answers `413` for an upload bigger than the old ceiling. The command stops before installing anything if the routes cannot be read (it says why, and does not guess), or if a route that needs its own location has a path nginx cannot be told about exactly (anything other than letters, digits, `. _ ~ -` and whole `{name}` segments). A route whose own ceiling is *below* the shared one is left to the gateway.

Checked against the real project's 205 routes: a private nginx in front of the real gateway turned 11 MB to an ordinary path away with `413` while 9 MB reached the gateway; `/files/upload` and an employee documents path took 20 MB (also with a trailing slash) and answered `413` above 60 MB; the WebSocket path reached the gateway (which answered `403`, since that route needs a role). Against a stub backend, nginx sent `Upgrade` only on WebSocket paths, a request the app did not answer in time got `504` after the configured timeout while a WebSocket path kept waiting, and the `/login` and `/metrics` limits were unchanged. Not run: the whole of `sudo arc deploy prod` on a machine with certbot and systemd. The switch to the project owner's account when run as root was checked with a stand-in for `subprocess.run`, not as root.

**Choosing how many workers.** The defaults are only a starting point. Set what you want, then run the same `arc deploy` command again — the counts are read when it runs, and it removes the units of workers you no longer have:

| Setting | Meaning |
|---|---|
| `gateway_workers` | web worker processes (any number; yours wins over the default in both modes) |
| `lineup_workers_per_queue` | job workers for **every** queue |
| `lineup_workers_<queue>` | job workers for one queue, e.g. `arc settings set lineup_workers_mail 3` |

The scheduler is always exactly one: more would fire every cron job twice.

**Database connections.** Every process holds its own connection pool, so a deployment opens *(processes × pool size)* connections, and PostgreSQL refuses the ones past its limit. You do not have to work that out: `arc deploy` (and `arc run`) ask PostgreSQL what this application may open, keep a few free for yourself, and split the rest over the processes. It reads the real limit — `max_connections` minus the superuser reserve, or the role's or database's own `CONNECTION LIMIT` if that is lower — never an assumed 100. For the dev sizing on a default server it prints:

```
Database connections: 97 usable (max_connections 100 − 3 reserved for superusers), 10 kept free
  gateway-worker    2 × 10 = 20
  lineup-worker     2 × 5  = 10
  lineup-scheduler  1 × 2  = 2
  32 of 87 available for the application
```

If the processes would not fit even at the smallest useful pool (2 per web or job worker, 1 for the scheduler), the command stops before it writes anything and says by how much; run fewer workers or raise `max_connections`. If the pools only fit by shrinking, they are all shrunk together, and what is left over goes back to the web workers first.

| Setting | Meaning |
|---|---|
| `pgdb_connection_reserve` | Connections kept free for migrations, `arc console`, backups and your own `psql` (default 10) |
| `pgdb_connection_budget` | Total connections ARC may use, when PostgreSQL's own numbers are not the real limit — behind PgBouncer, or on a managed database that hides them. Default `0` = ask the server. *(PgBouncer itself was not run; the setting replaces the number read from the server and nothing else.)* |
| `pgdb_pool_max_size_<role>` | Fix one kind of process's pool yourself — `gateway_worker`, `lineup_worker` or `lineup_scheduler` (e.g. `pgdb_pool_max_size_lineup_worker`). Counted first; a deployment never changes it |
| `lineup_concurrency` | How many jobs one worker process runs at once. Default `0` = as many as its pool has connections |

A job worker runs no more jobs at once than it has connections, so a burst of jobs waits in the queue instead of piling up inside the workers. If PostgreSQL's limit is lowered after you deployed, every process notices when it starts and shrinks its pool to a share that fits (it logs a warning); run `arc deploy` again to make the new numbers official. If a request still finds every connection busy for `pgdb_pool_acquire_timeout_ms` (10 s), it gets **HTTP 503 with `Retry-After`**, not a 500 and not a traceback.

Measured on one machine, PostgreSQL's default 100 connections, 8 web workers and 8 job workers: 600 queued jobs each holding a connection for 2 s, while 64 clients read records for 30 s. Before: connections peaked at 97, PostgreSQL turned away 83 attempts and **1,438 requests failed with HTTP 500**. After: the plan was 82 connections, the peak was 81, and **all 119,790 requests returned 200**; every job ran. With a deliberately tiny pool, the requests that could not get a connection all got 503 + `Retry-After: 2` (95,245 of them in two runs) and none a 500.

**Adding a queue later** (a new plugin that declares one): run the `arc deploy` command again, or nothing consumes it and its jobs just wait. `arc doctor --deep` tells you if that happened: *"queue(s) mail have no worker unit, so jobs for them are never consumed."*

**Checking it.** `arc status` is the one command that answers *how many web workers, which queues, how many schedulers are running*, and whether that is what was deployed:

```bash
arc status
```

```
arc-desk — per-user systemd units, dev sizing
┏━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┓
┃ process               ┃ expected ┃ running ┃ restarts ┃ configured by    ┃
│ gateway (web workers) │        2 │       2 │        - │ dev default      │
│ queue default         │        1 │       1 │        - │ dev default      │
│ queue mail            │        1 │       1 │        - │ dev default      │
│ scheduler             │        1 │       1 │        - │ fixed (one only) │
│ log writer            │        1 │       1 │        - │ fixed            │
units: 6 of 6 active, 0 failed · restarts since they started: 0
database connections: 6 open now · PostgreSQL lets this application open 97, 10 kept free
  plan: 2 gateway-worker × 10 + 1 lineup-scheduler × 2 + 2 lineup-worker × 5 = 32 of the 87 available when it was made
Everything matches.
```

*Expected* is read from the unit files `arc deploy` wrote; *running* comes from systemd and from the process list `arc ps` shows. It flags a failed unit (with the command to recover it), a count that does not match, a leftover `arc run` running workers a second time, and a setting you changed but have not applied yet (*"settings give 4 web worker(s) but the units run 2 — run `arc deploy dev` to apply"*). It also checks the connection plan against PostgreSQL as it is *now*: a plan made for other worker counts, or one larger than a limit that was lowered since (*"the plan uses 32 connections but PostgreSQL now allows 20 — run `arc deploy dev` to re-plan"*). It is read-only, does not boot the kernel (it opens one short database connection to read the limits), and exits 1 when anything is wrong, so it also works as a monitoring check; `--json` gives the same thing for scripts. `arc ps` alone lists the processes; `arc doctor --deep` additionally compares the queues your plugins declare with the queues that have workers.

**Running it.** For a production install drop `--user` and use `sudo` where needed:

```bash
systemctl --user list-units 'arc-*'              # everything, and which are failed
systemctl --user status arc-<project>-gateway    # one process
journalctl --user -u arc-<project>-worker-mail-1 # its output
systemctl --user reset-failed arc-<project>-worker-mail-1 && systemctl --user start arc-<project>-worker-mail-1
```

An older install that used a single `arc run` unit is switched over by running the new command with `--enable` (it stops and disables the old unit, and leaves its file in place).

**An office behind one address.** Three limits count per source address, and 300 people sharing one public address share each of them:

| Limit | Counts | Default | What an office does |
|---|---|---|---|
| nginx, on `/login` | login requests | 10 a second, burst 50: 300 people logging in within 25 seconds pass | `nginx_login_rate` / `nginx_login_burst`, or list the office in `nginx_login_allow` |
| gateway, anonymous requests | every request before sign-in, a login page being about ten | 300 a minute | `gateway_rate_limit_requests`, or list the office in `gateway_rate_limit_exempt` |
| authn, failed logins | failures only | 60 a minute | successes never count; `authn_login_throttle`, or `gateway_rate_limit_exempt` |

For an office at `203.0.113.0/24`: `arc settings set gateway_rate_limit_exempt 203.0.113.0/24` and `arc settings set nginx_login_allow 203.0.113.0/24`, then restart and run `arc deploy prod` again. Listing an office exempts it from these per-address limits only. What still protects against a compromised machine inside it: per-account lockout, the bound on concurrent password hashing (`authn_argon2`), and per-user limits once signed in. A `429` tells you which limit it was: nginx's is an HTML page, the gateway's is `{"code": "rate_limited"}` with `rate limit exceeded`, authn's is `{"code": "too_many_failures"}` or `rate_limited` with `too many login attempts`.

**Before going public**, review these settings (`arc settings set <key> <value>`):

| Setting | Why |
|---|---|
| `gateway_force_https` → `true` | Sends `Strict-Transport-Security`. Only turn on once HTTPS works. |
| `gateway_trusted_proxies` | The IP of your reverse proxy, so the real client IP is read from `X-Forwarded-For` rather than being nginx's address. Login rate limiting is keyed on the pair (client IP, account), so without this ARC sees nginx's address for everyone and that per-source separation is lost. |
| `gateway_cors_origins` | Left empty, CORS is disabled — the safe default. Only list origins that genuinely need cross-origin access. |
| `gateway_metrics_token` | Left empty, the app serves `/metrics` to anyone who can reach its port. The nginx site `arc deploy prod` writes already restricts `/metrics` (and anything under it) to this machine, so the internet gets a 403; this token is a second gate, and the only one if you front the app some other way. |
| `nginx_login_rate`, `nginx_login_burst`, `nginx_login_allow` | The per-address limit on `/login` in the nginx site `arc deploy prod` writes. Rate: `10` or `10r/s` (per second) or `300r/m` (default `10r/s`); burst: how many logins beyond the rate one address may make at once, 0 to 1000 (default 50); allow: comma-separated IP addresses or CIDR ranges that are **not limited at all**, e.g. `203.0.113.0/24,10.1.2.3`. Read when `arc deploy prod` runs, so re-run it after changing them; a bad value stops the command before anything is installed. |
| `gateway_max_json_body_bytes` | The largest **JSON** request body the gateway will decode, in bytes (default 2 MiB = 2097152); a bigger one gets `413` `{"error": "JSON body too large", "limit_bytes": …}` before any decoding starts. Decoding runs on the event loop and holds the GIL, so a big body pauses every other request on that web worker: about 7 ms per MB of ordinary JSON (10 MB: ~70 ms to decode, ~90 ms pause through the real gateway). It cannot be moved off the loop - a thread still holds the GIL (60 ms pause), a process pool is no better (66 ms, and slower overall) - so it is bounded. It applies to every route: an upload route's own `max_body_bytes` (e.g. `/files/upload`, 60 MB) lets big files in, and files are not decoded as JSON; the general `gateway_max_body_bytes` (10 MB) still applies to everything else. **If a client of yours posts JSON bodies over 2 MiB** (a bulk import in one call), raise this, or better send the file to an upload route and process it as a background job; `0` removes this ceiling. Restart to apply. |
| `gateway_max_inflight_body_bytes` | The most bytes of request bodies one web worker holds in memory at once (bodies over 64 KB; default 256 MiB = 268435456, `0` = no bound). A body is read whole before its handler runs, so a crowd of big uploads is a crowd of big allocations: about 0.7 MB of memory per MB of body (four anonymous 59 MB uploads took a worker from 425 to 594 MB). A request that does not fit waits up to 5 s for room, then gets `503` with `Retry-After: 2`; a body bigger than the whole bound is let in on its own. Two things work with it: a caller relay would refuse (no sign-in, wrong role) is refused **before** a large body is read, so an anonymous crowd cannot fill the bound, and an early reply no longer makes nginx answer `502`. Size it from RAM: web workers × this should stay well under what the machine can spare. `/metrics`: `arc_gateway_inflight_body_bytes` (in memory now), `arc_gateway_inflight_body_waiting`, `arc_gateway_inflight_body_rejected_total`. Restart to apply. |
| `error_tracking_dsn`, `error_tracking_environment`, `error_tracking_max_per_minute` | Where errors are sent besides the log and `/metrics`: a Sentry or GlitchTip DSN (**secret**; needs `uv add sentry-sdk`), the environment name they are filed under (default `production`), and the most events per bug per minute sent there (default 10, `0` = no limit). Unset DSN = errors are only counted and written to `logs/errors.jsonl`. Restart to apply. See *Know when it breaks*. |
| `tracing_export_timeout_seconds`, `tracing_shutdown_timeout_seconds` | Only matter when `tracing_otlp_endpoint` is set. The first (1 to 60, default 3) is how long one batch of spans, retries included, may spend reaching the collector before it is dropped; the second (0 to 30, default 5, `0` = do not wait) is how long a **stopping** process waits for the spans still queued before giving up on them. Keep the second well under the 30 s a systemd unit gets to stop: with the collector unreachable and traffic flowing, OpenTelemetry's own shutdown waits for every queued batch to fail, and `arc restart` ends in a kill after 30 s (measured: 30 s with 32 clients connected, against about 2 s idle). Failed exports are logged once a minute per kind of failure, saying how many more were held back. Restart to apply. |
| `redix_max_connections`, `redix_pool_wait_ms` | The most connections each process (web worker, queue worker, scheduler) opens to Redis/Valkey, at least 1 (default 512; opened only when needed), and how long a call waits for a free one when they are all in use before failing (default 1000 ms, `0` = fail at once). A busy web worker needs about one per request in flight; redis-py's own limit is 100, and past it every rate-limit check and session-cache lookup failed and fell back (to an in-process counter, to the database). Keep processes × `redix_max_connections` under the server's `maxclients` (10000 by default). `arc_redix_pool_wait_timeouts_total` above zero on `/metrics` means calls ran out of patience: raise it. (`/metrics` adds the processes up, so `arc_redix_pool_max_connections` reads processes × this setting: 3584 for 7 processes at 512.) Restart to apply. |
| `gateway_rate_limit_exempt` | Comma-separated IP addresses or CIDR ranges whose **anonymous** traffic is not counted by the per-address request limit (`gateway_rate_limit_requests`, 300 a minute by default); signed-in users keep their own per-user limit. Needs `gateway_trusted_proxies` set (`arc deploy prod` does) or the address the gateway sees is nginx's own, and boot says so. Restart to apply. |
| `nginx_read_timeout` | Seconds nginx waits for the app on an ordinary request before answering `504`, 1 to 3600 (default 60). WebSocket routes keep their own one-hour idle timeout whatever this is. Raise it only for a route that really needs longer, and better still make that work a background job. Read when `arc deploy prod` runs, so re-run it after changing it; a bad value stops the command before anything is installed. |
| `deploy_metrics_allow` | Comma-separated IP addresses or CIDR ranges, e.g. `203.0.113.7,10.0.0.0/24`, that may read `/metrics` through that nginx site besides this machine (your Prometheus server). Read when `arc deploy prod` runs, so re-run it after changing this; a bad value stops the command before anything is installed. A scraper on the same machine can use `127.0.0.1:8001/metrics` directly. |
| `authn_login_throttle` | What a source address pays for **failed** logins, **one JSON object**; a key you leave out keeps its default: `{"max_failures": 60, "window_seconds": 60, "failure_delay_ms": 250}`. After `max_failures` failed logins from one address in a window, further attempts from it get `429` (`too_many_failures`) until the window passes, before any password hashing is spent on them (0 turns the budget off). **Successful logins do not count**, so an office behind one address is not affected, while someone trying passwords or spraying usernames fails nearly every time and runs out. `failure_delay_ms` is a pause before any failure is reported (0 turns it off). A failure is treated identically whether or not the account exists (same count, same pause, same message): an unknown username that cost more or less than a known one would show an attacker which accounts exist. Addresses in `gateway_rate_limit_exempt` are not held to the budget. Read at startup. |
| `authn_argon2` | Password hashing, **one JSON object**; a key you leave out keeps its default: `{"memory_mb": 64, "time_cost": 3, "parallelism": 1, "max_concurrent": 2}`. `memory_mb` is the memory one hash uses (19 to 4096), `time_cost` the passes over it, `parallelism` the threads one hash uses, `max_concurrent` how many hashes one web worker runs at once. **What makes a password costly to crack is the memory and the passes; `parallelism` only spreads the same work over more threads** (OWASP's Argon2id profiles all use 1), so it is the setting that decides how much of the machine a flood of logins can take: web workers × `max_concurrent` × `parallelism` threads at once. Measured on a 12-core machine, one hash at 64 MiB and 3 passes takes **66 ms on 4 threads and 163 ms on 1**; four at once cost 326 CPU-ms each on 4 threads and 214 on 1. The default was 4 threads: 2 web workers × 2 hashes × 4 = 16 threads, and a flood of logins for accounts that do not exist took 9 to 10 of the 12 cores. At 1 it is 4 threads, a third of that machine, with the same strength; a login takes about 100 ms longer, and the most logins a second the server can do is web workers × `max_concurrent` ÷ the time one takes (2 × 2 ÷ 0.23 s ≈ 17 a second here; an office signing in at nine is a few a second). `arc doctor --deep` warns when a deployment could use more than half of the cores for it. **Passwords stored before the default changed** (made at `parallelism` 4) keep working: they verify as before, and that user's next login stores a new one. Peak memory per worker is `memory_mb` × `max_concurrent`; a few more hashes wait and the rest get `503` with `Retry-After`, so a login flood cannot run the server out of memory. **On a machine short of memory**, use OWASP's smallest profile: `arc settings set authn_argon2 '{"memory_mb": 19, "time_cost": 2, "parallelism": 1}'`, then restart. Existing hashes keep verifying with their own parameters and are replaced by one made under the new settings at the owner's next login. Measured with 2 web workers and 48 concurrent wrong-password logins: before, the workers grew by 1,098 MB (the run was stopped there to protect the machine) and a bystander's p99 was 266 ms; now +263 MB at the default and +47 MB with the 19 MiB profile, p99 96 ms and 37 ms. A bad value stops startup with a message naming the key. |
| `provider` under `[secrets]` in `.arc/arc.toml` | `local_file` (the default) keeps secrets encrypted in `.arc/arc.store.db`. **`env`** reads each one from an environment variable and stores nothing: `ARC_SECRET_PGDB_DSN`, `ARC_SECRET_REDIX_URL`, `ARC_SECRET_AUTHN_CSRF_SECRET`, `ARC_SECRET_FILER_SIGNING_SECRET`, … (the setting's name in capitals). `arc settings set … --secret` then refuses and names the variable to set, and a secret that is not set stays unset: ARC will not invent a CSRF or signing secret per worker. **Every ARC process needs the variables**, for example through an `EnvironmentFile=` line in each systemd unit (`arc deploy` does not write one). `.arc/arc.mkey` is still a file: it protects `arc.encrypt()` data, not these secrets. Any other value is an error at startup and in `arc doctor`; it used to be ignored, silently leaving the secrets in the local file. Tested: the gateway ran with all four secrets supplied only through the environment. |
| *permissions of `.arc/`* | New projects are private from the start (`.arc/` mode 700; the settings store and master key 600). A project created before that has `.arc/` readable by every local user: `arc doctor` says so, and **`arc doctor --fix`** repairs it. It is never changed silently, because a setup that shares `.arc/` with another account through a group must be able to keep doing that. |
| `gateway_access_log` | Where each request's access line goes. `file` (the default) writes it to `logs/gateway.jsonl` only; `console` also prints it through `arc run` (handy on a dev machine, but at thousands of requests per second that relay becomes the bottleneck); `off` disables the line. Request metrics are kept in every mode. Restart to apply. |
| `log_max_bytes`, `log_backup_count` | Size of each `logs/<category>.jsonl` before it rotates (default 10 MB) and how many rotated copies are kept (default 5). The access log is one ~370-byte line per request, so at several thousand requests per second the defaults hold only tens of seconds of history. Raise both if you need longer. |
| `log_collector` | `true` (default): `arc run` starts one writer for `logs/*.jsonl`, so no line is lost to rotation races between workers (8 processes at ~8,000 requests per second: every line kept). `false`: every process writes the files itself. If the collector goes away while the stack is running, each process keeps logging to files of its own, `logs/<category>.fallback-<pid>.jsonl`, and never to the shared ones. Restart to apply. |
| `log_flush_ms` | How long (ms) each process accumulates log lines before sending them to the collector, and how long the collector accumulates before one write (default 10). Lower means lines reach disk sooner; higher means fewer writes. |
| `arc_server_timezone` | Defaults to `UTC`; set an IANA name such as `Asia/Kolkata` if you want naive date-times interpreted in local time. |

Keep `arc run` on `127.0.0.1` (the default) and let nginx be the only public listener. Back it up with `arc backup` (next section): the database alone is not enough to bring the site back.

---

## Back it up, and prove you can restore it

A database dump alone cannot bring an instance back. Without `.arc/arc.mkey` every secret in the settings store is lost for good, and without the uploaded files every upload is gone. `arc backup create` writes **one file** with all of it:

| Inside the file | What it is |
|---|---|
| `database.dump` | the whole PostgreSQL database (`pg_dump`, custom format) |
| `state/arc.store.db` | the settings store, as a consistent snapshot (taken with SQLite's own backup call, not a copy of a file that may be mid-write) |
| `state/arc.toml`, `state/plugins.lock`, `config/` | project config and which plugin versions were running |
| `files/` | the filer's local files (read after the database was dumped, so every row in the dump has its file) |
| `key.enc` | the master key, encrypted with your passphrase (scrypt + AES-256-GCM) |
| `manifest.json` | what is inside, and a SHA-256 for every member; written last |

**Not inside:** the code (`arc/`, `plugins/`: reinstall it from your repositories at the versions in `plugins.lock`), logs, `.venv`, and files kept in S3 (`filer_default_storage = s3`; the command says so; use the bucket's versioning or replication).

```bash
export ARC_BACKUP_PASSPHRASE='a long passphrase, at least 12 characters'   # or --passphrase-file, or it asks
arc backup create --keep 14      # backups/full/arc-backup-<project>-<time>.tar, and delete all but the newest 14
arc backup verify backups/full/arc-backup-shop-20261107T020000Z.tar
```

`create` never overwrites a backup, refuses a short passphrase before it dumps anything, adds `backups/full/*` to `.gitignore`, and needs `pg_dump` on the machine (`apt install postgresql-client`). `verify` re-reads every checksum, opens the key with your passphrase, checks that the settings store is intact and that the database dump is a readable `pg_dump` archive. It exits 1 on any problem, so it can sit in a schedule.

**What the passphrase protects.** Only the master key. The database dump and the files are not encrypted (they hold your business data and password hashes), and the checksums detect damage, not tampering. Keep the file on encrypted storage, or encrypt it before it leaves the machine, for example `age -p -o backup.tar.age backup.tar`. **Lose the passphrase and the secrets cannot be recovered**; store it somewhere other than the server it protects.

**Keep a copy somewhere else.** A backup on the same disk as the database is not a backup. Copy it off the machine (`rsync`, `rclone`, an object-store bucket with versioning). Schedule it, for example from cron as the project's owner, with the passphrase in a file only that user can read:

```bash
# /etc/cron.d/arc-backup  (the passphrase file: mode 600, owned by the project owner, outside the project)
30 2 * * *  shop  cd /srv/shop && "$(command -v arc)" backup create --passphrase-file /etc/arc/backup.pass --keep 14 && rsync -a backups/full/ backup-host:/srv/arc-backups/shop/
```

### The restore drill

Do this once now, and again after any large change; a backup nobody has restored is a hope. It uses a **new empty database and a new directory**, so it cannot touch the live site:

```bash
createdb -O shop shop_restore_test         # an empty database (as a role that may create one)
arc backup restore backups/full/arc-backup-shop-20261107T020000Z.tar \
    --into /srv/shop-restore-test \
    --database-url postgresql://shop:PASSWORD@localhost/shop_restore_test
```

It verifies every checksum and decrypts the key **before it writes anything**; a wrong passphrase or a damaged file changes nothing. Then bring the code back and look at it:

```bash
cd /srv/shop-restore-test
# reinstall arc/ and plugins/ from your repositories at the versions in .arc/plugins.lock, then:
arc doctor && arc pgdb status && arc status
arc run --port 8011                         # a different port, then sign in as a real user
```

The restored project's `pgdb_dsn` points at the database you named, never the original, and an absolute `filer_local_root` is repointed at where the files now are. When you are satisfied: `dropdb shop_restore_test && rm -rf /srv/shop-restore-test`.

Restore refuses to guess: it needs `--database-url` explicitly (a backup never goes back to its original database on its own), refuses a target that already has tables or a directory that already has `.arc/` unless you pass `--force` (which replaces them), restores without the original owners and grants (so a new machine without the old role works) and stops at the first error rather than leave a half-restored database, and rejects archive paths that would write outside the target directory. `--skip-database` / `--skip-files` restore only part. To restore **into production** after a disaster, point `--database-url` at the new empty production database and `--into` at the new project directory.

**Measured.** On this project's own data (a 205-route gateway, 2,673 uploaded files): the backup was 20.8 MiB, 2,682 members, made in 2.0 s and verified in 0.9 s. The restore is exercised automatically (`tests/integration/test_backup_restore_drill.py`, 13 tests) against a private PostgreSQL server started for the test: a project is backed up with the real commands, restored into a *different* database and directory, then booted, its rows read back and a secret decrypted with the restored key; a live database is refused, a wrong passphrase and a flipped byte change nothing, and a dump `pg_restore` cannot read is caught by `verify`. Not run: a restore on a second machine, a backup of a multi-gigabyte database (the time and size grow with it), and the `cron` line above.

---

## Know when it breaks

An error nobody sees is as good as no error handling. ARC counts and records every unhandled failure in one place, with or without an error-tracking service.

**What is recorded**, without any setup: every `500` the gateway sends; a response stream or WebSocket handler that failed after it started; a failed job (however it was queued, including one picked up again after a worker died); a worker or scheduler process that stopped because of an error; and a failed look-up of who a request belongs to (the gateway carries on as if the caller were signed out, so without this a Redis outage would only look like "everyone got logged out"). **Not recorded:** `4xx` answers, `503` for "no database connection free" (that is capacity, not a bug), and exceptions your own code catches, unless it calls `arc.errors.capture(exc, source="shop", order=order_number)` itself.

Each one is a line in `logs/errors.jsonl`: when, which process type, the exception type, its message, the last frames (`file:line in function`, no variables), the route **template** (`/employee/{id}/documents`, never the real path), the queue and job name, the request id, and a **fingerprint** that is the same for the same bug whatever data triggered it (exception type plus the innermost frame in your code). Secret setting values are removed from messages and e-mail addresses are masked; request bodies, headers and ids in paths are never stored.

```bash
arc errors                        # the last 24 hours, most frequent bug first
arc errors --hours 1 --check      # prints the same, and exits 1 if anything failed in the last hour
arc errors --source lineup --json
```

The output looks like this (illustrative values):

```
7 error(s), 2 distinct problem(s) in the last 24 h

    6 ×  GET /employee/{employee_id}/documents  (gateway)  last 11-07 11:55:00 UTC  id 3f9c1a07d2be
         KeyError: 'department'

    1 ×  job hrms.payroll_run (default)  (lineup)  last 11-07 02:00:04 UTC  id b81e0c4492af
         ValueError: could not convert string to float: '12,50'
```

**Counters on `/metrics`** (summed over every web and job process, like the other metrics): `arc_errors_total`, `arc_errors_by_source_total{bucket="gateway"}`, and `arc_errors_unforwarded_total` (errors held back from the tracker by its rate limit). One limit to know: a counter lives in its process, so a process that **dies** takes its count with it; for crashes rely on `arc errors --check`, which reads the log, and on `arc status`.

Two more counters say when Redis is not keeping up, rather than when the app is failing: `arc_gateway_rate_limit_fallbacks_total` (request-limit checks Redis could not answer, decided from each web worker's own counter instead, so the limit is per worker until it recovers) and `arc_redix_pool_wait_timeouts_total` (calls that gave up waiting for a free Redis connection; see `redix_max_connections`). Either rising means look at Redis, or the pool size, before anything else.

And for request bodies: `arc_gateway_inflight_body_bytes` is what the web workers hold in memory for uploads and large posts right now, `arc_gateway_inflight_body_waiting` how many requests are waiting for room, and `arc_gateway_inflight_body_rejected_total` how many were told to come back (`503`). Rejections that keep rising mean either a flood of big uploads or a bound (`gateway_max_inflight_body_bytes`) too small for the RAM you have.

**Sentry or GlitchTip (optional).** The SDK is not a dependency of ARC; install it, give it the project's DSN, restart:

```bash
uv add sentry-sdk
arc settings set error_tracking_dsn 'https://<key>@<host>/<project>' --secret
arc settings set error_tracking_environment production
```

Boot says so if the DSN is set and the package is missing (errors are still counted and logged). It is started on the first error, not at boot. It sends the exception type, the message after the same scrubbing, stack frames without local variables and with project-relative paths, tags (source, route template, queue, job), the fingerprint above as its grouping key, and the release (`arc@<version>`). It is configured to send **no** request headers, cookies or bodies, no client address, no user and no breadcrumbs, and only errors reported through `arc.errors` (none of the SDK's own automatic hooks). At most `error_tracking_max_per_minute` (default 10) events per fingerprint reach the tracker; the rest are only counted and logged, so one bug in a hot path cannot use up your quota. Another destination is one function: `arc.errors.add_sink(lambda event: ...)`.

**A minimal monitoring setup** that tells you before your users do. Everything here already exists in ARC; it needs a place that notices (an uptime service, cron with mail, or Prometheus):

| What to check | How | It catches |
|---|---|---|
| The site answers and can reach its dependencies | An external uptime monitor on `GET https://app.example.com/readyz` (`200` = PostgreSQL and Redis answered; `/healthz` only says the process is alive) | an outage, or the database or Redis being unreachable |
| Something is failing | cron, hourly: `arc errors --hours 1 --check` | a new bug, a job failing every run |
| The processes are what was deployed | `arc status` (exits 1 on a failed unit or a wrong count) | a dead worker or scheduler |
| The backup is still good | `arc backup verify` on the newest file after each backup | a backup that stopped being written or is damaged |
| Trends and rates | Prometheus scraping `/metrics` (from this machine, or from an address in `deploy_metrics_allow`) | error rate, 5xx share, database pool saturation |

```bash
# /etc/cron.d/arc-monitor: cron mails whatever a command prints, so print only on failure
MAILTO=ops@example.com
5 * * * *  shop  cd /srv/shop && "$(command -v arc)" errors --hours 1 --check >/dev/null || "$(command -v arc)" errors --hours 1
```

```yaml
# Prometheus alert rules for the metrics above
groups:
  - name: arc
    rules:
      - alert: ArcErrors
        expr: increase(arc_errors_total[10m]) > 0
        labels: {severity: warning}
      - alert: ArcServerErrors
        expr: sum(increase(arc_gateway_requests_by_status_class{bucket="5xx"}[5m])) / sum(increase(arc_gateway_requests_total[5m])) > 0.02
        for: 5m
        labels: {severity: page}
      - alert: ArcDown
        expr: up{job="arc"} == 0
        for: 2m
        labels: {severity: page}
```

**Checked:** each failure above reaches `arc.errors` (gateway `500`, stream, WebSocket handler, identity look-up; lineup task, declared task, reaped job, process crash) and a `404`, `422` or `503` does not; the fingerprint is the same for the same bug and differs between bugs; secrets and e-mail addresses do not survive into the log line, a context value or what would be sent; a sink or logging failure never reaches the code that reported the error; the rate limit holds and expires after a minute; `arc errors` reads what the writer produces (a test writes through the real log format and reads it back); one `capture` costs about 0.1 ms. **Not run:** against a real Sentry or GlitchTip server (the SDK was replaced by a stand-in that records what it is asked to do, so what the SDK puts on the wire is untested here), the Prometheus rules and the cron line.

---

## Troubleshooting

**`permission denied to create extension "pgcrypto"`** during `arc pgdb migrate`
The database is not owned by the role ARC connects as. Fix it with `sudo -u postgres psql -c 'ALTER DATABASE arc OWNER TO arc'`, or recreate it as in step 4.

**`'pgdb_dsn' is not set`** (or `Warning: arc stubs failed to regenerate IDE stubs` while installing)
You have not run `arc pgdb setup` yet. Harmless until you do. The same applies to `redix_url` and `arc redix setup`.

**`password authentication failed for user "arc"`**, or `no pg_hba.conf entry`
Connect to `127.0.0.1` rather than a socket, and check that `pg_hba.conf` allows password (`scram-sha-256` or `md5`) logins from `127.0.0.1/32`. Reload with `sudo systemctl reload postgresql`.

**`arc build` ends with `Not restarted: 'restart_command' isn't set`**
Informational, and the exit code is 0. The build completed (look for `Backend build complete`); ARC just does not know how to restart your processes, so a running `arc run` is still on the old code until you restart it yourself. Run `arc deploy dev` once to set `restart_command`, or set it by hand to whatever restarts your processes. (A restart command you *did* configure that fails makes `arc build` exit non-zero. An explicit `arc restart` with nothing configured is still an error.)

**`sh: 1: tsc: not found` while building the UI**
Dependencies were never installed. Follow [step 9](#9-build-the-admin-ui) with `yarn install --frozen-lockfile` first.

**`/admin` returns 404**
The UI is not built. Do step 9, then restart `arc run`.

**Admin pages load but show errors, or a mail you queued never arrives**
Check that `arc run` printed no `warning:` lines and that its `queues` list includes `mail`. If you have set `lineup_queues` by hand (step 10), it replaces auto-discovery, so it must list `default,mail` too.

**`another arc run is already active for this project (pid N)`**
A previous `arc run` is still alive. Run `arc kill` (add `--yes` in scripts) and start again. If you are certain nothing is running, delete `.arc/runtime/arc_run.lock`.

**`502 Bad Gateway` from nginx**
nginx is up but nothing is listening on the port it proxies to — `arc run` is not running. Start it (or `systemctl --user start` the unit) and check `arc doctor`.

**Port 8001 is already in use**
`arc run --port 9000`. If you use `arc deploy prod`, pass the same `--port`.

**`Connection budget: ... connections are needed at the very least`** from `arc deploy` or `arc run`
The workers you asked for cannot fit in the connections PostgreSQL allows (the message says how many are needed and how many exist). Run fewer workers (`gateway_workers`, `lineup_workers_per_queue`), raise `max_connections` in `postgresql.conf` (then restart PostgreSQL), or lower `pgdb_connection_reserve`. If a pooler such as PgBouncer sits in front, set `pgdb_connection_budget` to what it can serve.

**Logins fail with `503` and `Retry-After`, log says `password hashes are already running or waiting`**
The web worker already has `max_concurrent` password hashes running and a short queue behind them (4 per slot), usually during a flood of login attempts. It is capacity, not a bug, and a real user succeeds on retry. If it happens to ordinary traffic, raise `max_concurrent` in `authn_argon2` if you have the memory (each slot costs `memory_mb`), or check `arc status` for what else is using the machine.

**Requests fail with `503` and `Retry-After`, log says `no database connection available`**
Every connection in that process's pool was busy for `pgdb_pool_acquire_timeout_ms`. It is capacity, not a bug, and clients can retry. Check `arc status` for the pool sizes and how many connections are open; the usual fixes are fewer concurrent jobs (`lineup_concurrency`), a slow query holding connections (`slow_query_threshold_ms` logs it), or more connections (see *Database connections* above).

**`uv sync` removed all my plugins**
A bare `uv sync` at the project root uninstalls every workspace member. Always use `uv sync --all-packages`. ARC's own commands already do.

**`lineup scheduler: cannot reach Redis`**
The scheduler keeps running and retries (every 1 s, backing off to every 10 s) until Redis answers, then takes the leader lock again; it does not exit, so systemd never marks it failed because of an outage. A second scheduler waits as a standby (`another scheduler is the leader — standing by`) and takes over when the first stops. Check `redis-cli ping`. If a scheduler that is leading can no longer confirm its lock (a renewal failed), it stops dispatching at once and says so (`no longer sure it is the leader`), then contends again — so two schedulers never fire the same cron job. Any other error that ends a scheduler is printed and exits with code 1.

**`redix: FAILED to connect`**
Check `redis-cli ping`, then re-run `arc redix setup`. If Redis has a password, enter it there.

---

## Updating

```bash
arc update
arc pgdb plan
arc restart
```

`arc update` runs `git pull --ff-only` on the kernel and every enabled plugin, skipping any with uncommitted changes. It does **not** touch the database or the running process: review `arc pgdb plan` for schema changes that came with the update, apply them with `arc pgdb migrate`, then `arc restart` once you are ready for the new code to take effect. (`arc restart` needs `restart_command`; `arc deploy dev` sets it.)

Use `arc build` after you add a plugin dependency by hand or change the Admin UI's source; it re-resolves dependencies and rebuilds frontends.
