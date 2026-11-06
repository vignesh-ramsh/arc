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

The worker counts are sized to your machine's CPU and memory (never more than 8). On a small VM you can lower them: `arc settings set gateway_workers 2` and `arc settings set lineup_workers 2`. To use another port, `arc run --port 9000`.

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

This writes an nginx site for that domain that proxies to the app and obtains a Let's Encrypt certificate with certbot (`--no-ssl` for plain HTTP; `--port` to change the app's internal port, default 8001). It then installs the same units under `/etc/systemd/system`, run as the project's owner (never root), started on boot, **sized from the machine**: as many web workers as the CPU and memory allow (never more than 8 automatically), and the same number of job workers in total, shared out over the queues with at least one each. The app starts last, so it comes up with the final settings. `arc restart` runs `sudo systemctl restart arc-<project>.target`, so the project's owner needs passwordless sudo for exactly that command. It only touches this project's own units and nginx site. Run it from inside your project with the virtualenv active: `sudo` does not inherit your `PATH`, and `"$(command -v arc)"` hands it the full path to the project's copy.

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

**Before going public**, review these settings (`arc settings set <key> <value>`):

| Setting | Why |
|---|---|
| `gateway_force_https` → `true` | Sends `Strict-Transport-Security`. Only turn on once HTTPS works. |
| `gateway_trusted_proxies` | The IP of your reverse proxy, so the real client IP is read from `X-Forwarded-For` rather than being nginx's address. Login rate limiting is keyed on the pair (client IP, account), so without this ARC sees nginx's address for everyone and that per-source separation is lost. |
| `gateway_cors_origins` | Left empty, CORS is disabled — the safe default. Only list origins that genuinely need cross-origin access. |
| `gateway_metrics_token` | Left empty, `/metrics` is open to anyone who can reach the port. Set a token if it is reachable. |
| `gateway_access_log` | Where each request's access line goes. `file` (the default) writes it to `logs/gateway.jsonl` only; `console` also prints it through `arc run` (handy on a dev machine, but at thousands of requests per second that relay becomes the bottleneck); `off` disables the line. Request metrics are kept in every mode. Restart to apply. |
| `log_max_bytes`, `log_backup_count` | Size of each `logs/<category>.jsonl` before it rotates (default 10 MB) and how many rotated copies are kept (default 5). The access log is one ~370-byte line per request, so at several thousand requests per second the defaults hold only tens of seconds of history. Raise both if you need longer. |
| `log_collector` | `true` (default): `arc run` starts one writer for `logs/*.jsonl`, so no line is lost to rotation races between workers (8 processes at ~8,000 requests per second: every line kept). `false`: every process writes the files itself. If the collector goes away while the stack is running, each process keeps logging to files of its own, `logs/<category>.fallback-<pid>.jsonl`, and never to the shared ones. Restart to apply. |
| `log_flush_ms` | How long (ms) each process accumulates log lines before sending them to the collector, and how long the collector accumulates before one write (default 10). Lower means lines reach disk sooner; higher means fewer writes. |
| `arc_server_timezone` | Defaults to `UTC`; set an IANA name such as `Asia/Kolkata` if you want naive date-times interpreted in local time. |

Keep `arc run` on `127.0.0.1` (the default) and let nginx be the only public listener. Back up `.arc/` (especially `arc.mkey`) and dump the database with `pg_dump`.

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

**Requests fail with `503` and `Retry-After`, log says `no database connection available`**
Every connection in that process's pool was busy for `pgdb_pool_acquire_timeout_ms`. It is capacity, not a bug, and clients can retry. Check `arc status` for the pool sizes and how many connections are open; the usual fixes are fewer concurrent jobs (`lineup_concurrency`), a slow query holding connections (`slow_query_threshold_ms` logs it), or more connections (see *Database connections* above).

**`uv sync` removed all my plugins**
A bare `uv sync` at the project root uninstalls every workspace member. Always use `uv sync --all-packages`. ARC's own commands already do.

**`lineup scheduler: cannot reach Redis`**
The scheduler keeps running and retries (every 1 s, backing off to every 10 s) until Redis answers, then takes the leader lock again; it does not exit, so systemd never marks it failed because of an outage. A second scheduler waits as a standby (`another scheduler is the leader — standing by`) and takes over when the first stops. Check `redis-cli ping`. Any other error that ends a scheduler is printed and exits with code 1.

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
