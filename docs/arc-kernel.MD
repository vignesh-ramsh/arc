# The ARC Kernel — A Complete, Plain-English Guide

This document explains what the **Kernel** is, what it does, every method on
it, and exactly when and how to use each one. It's written for someone who
has never seen ARC's internals before — every term is explained the first
time it shows up, and every method has a short "when do I use this" note
plus sample pseudo-code.

If you only read one section, read **"The Big Picture"** — everything else
is detail on top of that one idea.

---

## 1. The Big Picture

ARC is built out of **plugins**. Each plugin does one job — `psqldb` talks
to Postgres, `authn` handles login, `filer` stores files, and so on. A
plugin needs a way to:

1. Tell the rest of the app "I'm ready, here's what I offer."
2. Find and use *other* plugins' work, without importing their internal code.

The **Kernel** is the one object that makes both of those possible. Think of
it as a **reception desk** for the whole application:

- When a plugin starts up, it walks up to the desk and **registers** what it
  offers ("I'm `psqldb`, here's my database toolbox").
- When any code — a plugin, or your own application code — needs something
  a plugin offers, it asks the desk for it by name ("give me `psqldb`").

That's the entire idea. Nearly every method on the Kernel is either the
"register" half or the "ask for it" half of that story.

### Where the Kernel comes from

You almost never create a Kernel yourself. `arc.boot()` creates one, uses
it to start every enabled plugin, and hands it back to you:

```
kernel = arc.boot()
```

After `boot()` finishes, you don't even need to hold onto `kernel` — every
plugin's offering becomes available as `arc.<name>`:

```
import arc
arc.boot()

# arc.psqldb, arc.relay, arc.filer, ... are all live now
rows = await arc.psqldb.fetch("SELECT * FROM employee")
```

The ONE place you actually touch the Kernel object directly is inside a
plugin's own `register(kernel)` function — the function every plugin must
define, which `boot()` calls once per plugin, in the right order.

```python
# plugins/mything/mything/__init__.py

def register(kernel):
    provider = MyThingProvider(kernel)
    kernel.export("mything", provider)
```

That's a complete (if minimal) plugin. `register(kernel)`:
1. Builds whatever object your plugin wants to offer (`provider` above —
   it can be anything: a class instance, a plain object, even a function).
2. Calls `kernel.export(name, instance)` **exactly once** to hand that
   object to the Kernel under a name.

After boot finishes, anyone can now call `arc.mything.whatever()`.

### What the Kernel is *not*

- It is **not** a dependency-injection framework with types/interfaces. It
  only ever deals with a plain string name and a plain Python object. It
  never knows or cares what that object actually is.
- It is **not** the thing that decides *which* plugins are installed, or in
  what order they load — that's a separate piece (`arc.resolver` /
  `.arc/plugins.lock`). By the time your `register(kernel)` runs, that
  decision has already been made; the Kernel just carries out the plan.
- It does **not** survive between processes. It's rebuilt fresh every time
  a process boots (a web worker, a CLI command, a background job — each one
  calls `arc.boot()` for itself).

---

## 2. The Core Idea: a "Capability"

Every single thing a plugin exports is called a **capability**. A
capability is just this bundle of information:

| Field               | What it is                                                          |
|---------------------|----------------------------------------------------------------------|
| `name`              | The string other code will use — becomes `arc.<name>`               |
| `instance`          | The actual object/value being shared — anything at all               |
| `requires`          | Names of OTHER capabilities this one cannot work without             |
| `optional_requires` | Names of other capabilities this one can use *if present*, but doesn't need |
| `plugin`            | Which plugin exported it (tracked automatically, for good error messages) |

You will see this word "capability" everywhere in ARC's own code and
error messages — it just means "one named thing a plugin registered."

---

## 3. Kernel Methods — One at a Time

Below is every method you'll actually use, in the order you'll typically
meet them.

### 3.1 `kernel.export(name, instance, requires=(), optional_requires=())`

**What it does:** Registers `instance` under `name`, making it reachable
later as `arc.<name>`.

**When to use it:** Exactly once, inside your plugin's `register(kernel)`
function. This is the "I'm ready" announcement — every plugin must call it,
and must call it exactly once (calling it twice, or not at all, is a hard
startup error).

**How to use it:**

```python
def register(kernel):
    provider = MyThingProvider(kernel)

    kernel.export(
        "mything",                 # name -> becomes arc.mything
        provider,                  # the object being shared
        requires=["psqldb"],       # "I cannot work without psqldb"
        optional_requires=["redix"],  # "I'll use redix if it's around"
    )
```

- `requires` — list the capabilities you genuinely cannot function
  without. If any of them isn't available when you call `export()`, the
  whole boot fails immediately with a clear message — much better than
  failing later, mid-request, with a confusing crash.
- `optional_requires` — list capabilities you can take advantage of, but
  don't strictly need. Nothing is checked or enforced for these; they exist
  so tooling and error messages understand your plugin's *real*
  relationships. Check for them yourself at runtime with `kernel.has(...)`
  (see below) before using them.

**Rules `export()` enforces for you (so you don't have to remember them):**

- The name must be a valid Python identifier (`my_thing`, not `my-thing`
  or `2thing`), because it becomes a literal attribute: `arc.my_thing`.
- The name can't start with `_` (reserved for internals).
- The name can't collide with something ARC itself already uses
  (`arc.boot`, `arc.settings`, `arc.log`, etc.) — pick a name that isn't
  already meaningful to ARC.
- The name can't already be taken by another plugin — two plugins cannot
  both export `"psqldb"`, for instance.
- You can only export **one** capability per plugin, and its name must
  match what your plugin's manifest (`plugin.toml`) says it will export.
- Every capability listed in `requires` must already exist at the moment
  you call `export()`. (This is why load *order* matters — ARC works this
  out for you before your `register()` ever runs, as long as your
  `plugin.toml` accurately lists what you require.)

**What happens if you break a rule:** `export()` raises `ExportError`
(see §5 of this same document) with a message that tells you exactly what's wrong and, usually,
exactly how to fix it.

---

### 3.2 `kernel.get(name)`

**What it does:** Returns the exact object that was registered under
`name`. This is what `arc.<name>` uses behind the scenes — you rarely call
`kernel.get()` directly yourself; you just write `arc.psqldb` and Python's
attribute-lookup machinery calls it for you.

**When to use it directly:** Almost never in application code — prefer
`arc.<name>`. You *would* call it directly if you only have the `kernel`
object itself and not the `arc` module (rare — mostly internal tooling), or
inside `register(kernel)` if you want the raw object rather than going
through `arc.` (both work identically once boot is underway).

**How to use it:**

```python
def register(kernel):
    psqldb = kernel.get("psqldb")     # same object as arc.psqldb
    psqldb.register_model(...)
```

**If the name doesn't exist:** raises `KernelError`, and the message lists
every capability that *is* currently registered, so you can immediately see
the typo or the missing dependency.

---

### 3.3 `kernel.has(name)`

**What it does:** Returns `True`/`False` — is this capability registered
right now?

**When to use it:** Whenever something is *optional* for you. This is the
method that goes with `optional_requires` — you declared "I can use redix
if it's there," and `has()` is how you check whether it actually is, before
you try to use it.

**How to use it:**

```python
def register(kernel):
    provider = MyThingProvider(kernel)
    kernel.export("mything", provider, optional_requires=["redix"])

    if kernel.has("redix"):
        provider.enable_caching(kernel.get("redix"))
    else:
        kernel.advise("redix isn't installed — mything will run without caching.")
```

This pattern — `requires` for hard dependencies, `has()` + graceful
fallback for optional ones — is used throughout ARC's own plugins (e.g.
`gateway` is optional for many plugins that can serve an API but don't
strictly need to).

---

### 3.4 `kernel.capabilities()`

**What it does:** Returns a read-only view of *every* registered
capability, as a mapping from name to a `Capability` object (name,
instance, requires, optional_requires, plugin — see §2).

**When to use it:** When you need to inspect the whole system rather than
one specific capability — for example, listing every installed plugin,
building a health-check dashboard, or writing tooling that walks the whole
capability graph. Application code almost never needs this; it's mostly
for introspection/tooling (this is exactly how `admin`'s own health page
lists every plugin).

**How to use it:**

```python
def list_installed_plugins():
    names = set()
    for cap in kernel.capabilities().values():
        names.add(cap.plugin)
    return sorted(names)
```

Because it's read-only (`MappingProxyType`), you can safely hand this out
to callers without worrying about someone accidentally mutating the
Kernel's internal registry.

---

### 3.5 `kernel.current_plugin()`

**What it does:** Returns the name of whichever plugin's `register(kernel)`
is running *right now* — or `None` if no plugin registration is currently
in progress (e.g. you're calling this after boot has already finished).

**When to use it:** When you're building a provider that OTHER plugins call
INTO during their own `register()` — and you want to remember *which*
plugin handed you something, without making every caller pass their own
name by hand. `psqldb` uses exactly this to remember which plugin declared
which schema file, so error messages can say "this table came from plugin
X" automatically.

**How to use it:**

```python
class SchemaRegistry:
    def register_model(self, schema_dir):
        plugin_name = self._kernel.current_plugin()  # e.g. "hrms"
        # ... remember that these schemas came from `plugin_name` ...
```

If some OTHER plugin calls `arc.psqldb.register_model(...)` from inside
*its own* `register(kernel)`, `current_plugin()` correctly reports THAT
caller's name — not psqldb's own name — because it tracks whoever's
`register()` is on the call stack right now, not who owns the method being
called.

---

### 3.6 `kernel.advise(message)`

**What it does:** Records a **non-fatal** warning. Advisories are for
things worth knowing about but that should never stop the app from
starting — "you're using a dev-only setting," "this optional feature isn't
configured," and so on. An advisory is never a crash.

Calling `advise()` does three things at once:
1. Adds the message to `kernel.advisories` (a running list, in memory).
2. Sends it through Python's `logging` system, so it lands in
   `logs/system.jsonl` for later review.
3. Emits a Python `warnings.warn(...)` (as the `ArcAdvisory` category), so
   tools that watch for warnings (and tests) can catch it too.

**When to use it:** Any time you detect something worth flagging, but it
would be wrong to stop the whole application over it. Rule of thumb: if the
right response is "the developer should probably look at this," it's an
advisory. If the right response is "this literally cannot work," raise a
real exception instead — don't use `advise()` to paper over a real failure.

**How to use it:**

```python
def register(kernel):
    provider = MyThingProvider(kernel)
    kernel.export("mything", provider)

    if kernel.settings.get("mything_api_key") is None:
        kernel.advise(
            "mything: no API key configured — outbound calls will be "
            "disabled until you run `arc settings set mything_api_key ...`."
        )
```

**Reading advisories back:** anything already recorded is available on
`kernel.advisories` (a plain list of strings) — useful for a health page
that wants to show "3 warnings" somewhere in the UI.

---

### 3.7 `kernel.settings` (not a method — an attribute)

**What it is:** The project's settings manager. Every plugin that needs
configuration (API keys, feature flags, tunable numbers) goes through this
same object — there's no per-plugin config file.

**When to use it:** Inside `register(kernel)`, to declare which settings
your plugin cares about, and to read their current values.

**How to use it:**

```python
def register(kernel):
    kernel.settings.declare("mything_api_key", secret=True)
    kernel.settings.declare("mything_timeout_seconds")

    api_key = kernel.settings.get("mything_api_key")
    timeout = kernel.settings.get("mything_timeout_seconds") or "30"
```

- `declare(key, secret=False)` — tells ARC "this key exists and belongs to
  me." This is documentation-only for a plain key (it just makes the key
  show up in tooling like `arc settings list`), but for a `secret=True` key
  it also makes sure it's handled through the encrypted secrets store,
  never written to the project's plain settings file.
- `get(key)` — reads the current value (or `None` if it was never set).
- Values are always set from OUTSIDE your plugin — normally via
  `arc settings set <key> <value>` on the command line, or through the
  admin UI's Settings page. Your `register()` only ever *reads*.

This is the same object the module-level `arc.settings.get(...)` /
`arc.settings.set(...)` functions proxy to — `kernel.settings` is just the
one you already have in hand while `register()` is running.

**Where a `secret=True` value is actually stored:** `arc.secrets` — an
internal, pluggable backend module (`.arc/arc.secrets`, Fernet-encrypted
with `.arc/arc.mkey`), swappable for a cloud provider (Vault, AWS Secrets
Manager, Azure Key Vault) via `[secrets].provider` in `arc.toml` without
changing anything above it. You should never call it directly — it's not
part of the public surface a plugin author reaches for; `kernel.settings`/
`arc.settings` (above) is the whole interface. It's worth knowing it
exists mainly because `arc.encrypt`/`arc.decrypt` (§4.5)'s no-key default
path derives its own key from the same `.arc/arc.mkey` file this backend
uses — a deliberately *separate* derived key, never the same one.

---

### 3.8 `kernel.project_root` (attribute)

**What it is:** The absolute filesystem path to the ARC project's root
directory (the folder containing `.arc/arc.toml`) — or `None` if the
Kernel was built without a real project (some tests do this on purpose).

**When to use it:** Whenever your plugin needs to read or write a file
relative to the project (a local file-storage root, a generated report, a
cache directory).

**How to use it:**

```python
def register(kernel):
    from pathlib import Path

    root = kernel.project_root or Path.cwd()
    upload_dir = root / "files"
    upload_dir.mkdir(parents=True, exist_ok=True)
```

---

### 3.9 `kernel.advisories` (attribute)

**What it is:** A plain Python list of every message passed to
`kernel.advise(...)` so far, in the order they happened. Read-only in
spirit (nothing stops you from mutating it, but nothing expects you to).

**When to use it:** Building a diagnostics or health view that wants to
show every warning collected during this boot, in one place.

```python
async def health():
    return {
        "ok": True,
        "advisories": list(kernel.advisories),
    }
```

---

## 4. The Kernel's Sibling Services: Events, Logging, Health, Codec

The `Kernel` object itself (everything in §3) is deliberately small — it
only ever does "register a name" and "look a name up." But once
`arc.boot()` has run, a handful of **separate modules** become available
right alongside it, always reachable the same simple way (`arc.events`,
not a method on the `kernel` object). They're not capabilities either — you
never `kernel.export()` them, they're just always there. Think of them as
the Kernel's own built-in toolbox, available to every plugin for free.

The two you'll use constantly are **events** and **logging** — the user
question this section exists to answer. Health and codec are covered
briefly too, since they live in the same family.

### 4.1 `arc.events` — talking between plugins without importing each other

**What it is:** A simple, **same-process only** publish/subscribe system.
One plugin `emit`s a named event; any number of OTHER plugins can `on`
(subscribe to) that name and react — without either plugin importing the
other's code, or even knowing the other exists.

**When to use it:** When plugin A does something that plugin B might care
about, but A shouldn't have to know B exists (and B might not even be
installed). Classic example already inside ARC: when a database schema
changes, other parts of the system that cache schema-derived state want to
know, without `psqldb` having to import every possible listener.

**How to subscribe** — normally done once, inside your own
`register(kernel)`:

```python
import arc

def register(kernel):
    provider = MyThingProvider(kernel)
    kernel.export("mything", provider)

    @arc.events.on("psqldb.schema.changed")
    async def _on_schema_changed(**payload):
        provider.invalidate_cache()
```

- The handler **must be `async def`** — a plain function raises `TypeError`
  immediately, on purpose (no silently-ignored subscriptions).
- `on()` returns the handler back to you, so it also works as a decorator
  (as above) or a plain call: `arc.events.on("name", my_handler)`.

**How to publish** an event — from wherever the interesting thing actually
happens (not necessarily inside `register()`):

```python
async def apply_schema_change(...):
    ...
    await arc.events.emit("psqldb.schema.changed", table="employee")
```

**What you get back:** `emit()` returns a small dict describing what
happened per-handler (`"ok"` or `"error: ..."`) — informational only. This
matters because of the next rule:

**A handler that raises never stops anything else.** Every subscribed
handler runs in order; if one raises, ARC logs it and moves on to the next
handler — the exception never propagates back to whoever called `emit()`.
This is deliberate: **an emitter must never depend on what a handler does**.
If you genuinely need to know the outcome of some work (not just "let
everyone who's interested know this happened"), events are the wrong tool —
call the other plugin directly, or use a relay hook instead.

**Other things worth knowing:**

- `arc.events.subscriptions()` — see every event name and which
  `plugin.handler` is listening to it. Good for a diagnostics page.
- `arc.events.stats()` — cumulative ok/error counts per handler, since this
  process booted (in-memory only, resets on restart).
- Event names are just strings, namespaced by convention
  (`"psqldb.schema.changed"`, not `"schema_changed"`) — the Kernel doesn't
  enforce a format, but everyone follows `plugin.thing.happened` so names
  don't collide.
- There's also a **process bridge** (`arc.events.install_process_bridge()`)
  that turns OS signals and a periodic "did anything change on disk"
  poll into a `system.reload` event — this is what makes `arc reload` and
  live schema-change pickup work. It's opt-in, used by long-running
  server processes (`gateway`, `lineup`), and you will not need to call it
  yourself unless you're building a new kind of long-running process.

### 4.2 Logging — how a plugin writes log lines

**What it is:** ARC configures Python's normal, built-in `logging` module
for you, once, at the very start of `arc.boot()` — **before** any plugin's
`register()` runs. That means you never set up logging yourself; you just
use it, exactly like you would in any other Python project:

```python
import logging

_logger = logging.getLogger("mything")

def register(kernel):
    ...
    _logger.info("mything: ready")
```

**When to use it:** For anything you'd normally reach for `print()` for —
routine status lines, request details, background-job progress, caught-but-
notable errors. This is different from `kernel.advise()` (§3.6): `advise()`
is specifically for **boot-time, developer-facing warnings** and also
happens to go through logging under the hood; plain `logging.getLogger(...)`
is for everything else, at any point during the process's life, not just
during `register()`.

**What happens to your log line, automatically:**

1. It prints to the console, nicely formatted with a timestamp
   (`[14:23:05] mything: ready`).
2. It's ALSO written as one JSON object per line into a file under your
   project's `logs/` folder — so every log line is both human-readable
   live, and machine-readable later.

**Which file it lands in** is decided purely by your logger's name:

| Logger name starts with… | File                  |
|----------------------------|------------------------|
| `gateway`                  | `logs/gateway.jsonl`  |
| `relay`                    | `logs/relay.jsonl`    |
| `psqldb`                   | `logs/db.jsonl`       |
| `lineup` (worker process)  | `logs/queue.jsonl`    |
| `lineup` (scheduler process) | `logs/scheduler.jsonl` |
| anything else               | `logs/system.jsonl`   |

So a brand-new plugin's logs land in `system.jsonl` automatically — no
setup needed. (If you ever build something that deserves its own file,
that mapping lives in `arc/arc/log.py`; it's not something a plugin
controls from the outside today.)

**Attaching extra structured data:** anything you pass via `extra={...}`
becomes a real field in the JSON line, not just text mashed into the
message:

```python
_logger.info(
    "mything: request handled",
    extra={"user_id": user.id, "duration_ms": 12.4},
)
```

That JSON line ends up looking like:
`{"timestamp": "...", "level": "info", "logger": "mything", "pid": 4821, "message": "mything: request handled", "user_id": "...", "duration_ms": 12.4}`
— genuinely queryable/grep-able data, not a string you'd have to parse
back apart later.

**Log level and rotation** are project settings, not something a plugin
configures: `log_level`, `log_max_bytes`, `log_backup_count` (set via
`arc settings set log_level DEBUG`, for example). You don't need to
declare or touch these yourself.

### 4.3 `arc.health` — one health check for every capability, for free

**What it is:** `await arc.health.check()` calls `health()` on every
currently-registered capability that happens to define one, and collects
the results into one dict — `arc.gateway.health()`, `arc.psqldb.health()`,
and so on, all swept in one call.

**When to use it:** If your plugin has something worth reporting on ("is my
connection pool open," "how many items are queued"), just define an
`async def health(self)` method on the object you export — you don't
register it anywhere separately, `arc.health.check()` finds it by simply
checking whether it exists (this is called "duck-typing": ARC doesn't
check *what kind* of object your capability is, only whether it happens to
have a `health` method).

```python
class MyThingProvider:
    async def health(self) -> dict:
        return {"ok": self._connected, "queued_jobs": len(self._queue)}
```

A capability with no `health()` method is simply skipped — you don't have
to define one if you have nothing worth reporting.

### 4.4 `arc.codec` — one shared way to encode/decode data

**What it is:** A single, shared serializer (built on `msgspec`) so every
plugin encodes/decodes JSON-like data the same way, instead of each one
picking its own library.

**When to use it:** Any time your plugin needs to turn Python objects into
bytes (or back), or validate that some data matches an expected shape.

```python
import arc

data = arc.codec.encode(my_value)               # -> bytes
value = arc.codec.decode(data)                   # -> Any
typed = arc.codec.decode(data, type=MyStruct)    # -> MyStruct, validated
```

`arc.codec.Struct` is the base class for declaring a typed shape (a bit
like a lightweight, fast dataclass with validation) — see `arc.codec`'s own
docstring for the full picture; it's a small, self-contained module, not
part of the Kernel's own registration story.

### 4.5 `arc.hash` / `arc.verify_hash` / `arc.encrypt` / `arc.decrypt` — hashing and encryption

**What it is:** Four bare, top-level functions (not namespaced under a
module, the same way `arc.boot`/`arc.shutdown` aren't) for one-way hashing
and reversible symmetric encryption. `arc.hash`/`arc.verify_hash` are pure
stdlib and work even before `arc.boot()` has run; `arc.encrypt`/
`arc.decrypt` only need a booted project for their no-key default path.

**When to use `hash`/`verify_hash`:** Any fast, general-purpose one-way
digest — an API token, a session id, a content checksum — plus a safe,
constant-time way to compare two of them. **Not for passwords** — `hash()`
is a fast, unsalted digest, the wrong primitive for a password, which
needs a slow, salted, purpose-built KDF instead (that's `argon2`, used
directly by `authn`/`admin`, not this).

```python
import arc

token_hash = arc.hash(raw_token)          # str or bytes -> hex digest
...
if arc.verify_hash(arc.hash(candidate), stored_hash):
    ...  # constant-time comparison — never compare hashes with `==`
```

`hash()` also accepts non-string payloads (a dict, a list, a number) —
anything `arc.codec` can encode — hashing its encoded form. A `str`/`bytes`
payload is always hashed as its own raw bytes directly, so it stays
byte-identical to a plain `hashlib.sha256(raw.encode()).hexdigest()`.

**When to use `encrypt`/`decrypt`:** Whenever you need to store or pass
around a value that must be recoverable later — unlike `hash()`, this is
reversible. Round-trips anything `arc.codec` can encode (a string, a dict,
a list, a number), not just strings.

```python
token = arc.encrypt({"user_id": user.id, "scope": "reset"})
...
payload = arc.decrypt(token)   # -> {"user_id": ..., "scope": "reset"}
```

- **No `key=` given (the common case):** encrypted/decrypted with a key
  derived from *this project's own* master key (`.arc/arc.mkey`) — the
  same file `arc.secrets` itself uses, but via a separate, purpose-scoped
  derivation, so a leak or rotation of one key never implicates the other.
  Requires `arc.boot()` to have already run; raises `CryptoError`
  otherwise, with a message telling you to either boot first or pass
  `key=`.
- **`key=<str | bytes>` given:** derived from that value instead — no
  project binding needed, usable even before `arc.boot()`.
- `decrypt()` must be called with the *same* `key=` (or lack of one)
  `encrypt()` used, or it raises `CryptoError` — also raised for a
  corrupted or tampered-with token, never silently returning garbage.

### 4.6 `arc.tz` — UTC "now plus/minus a delta", and the project's display timezone

**What it is:** Two unrelated things that happen to live in the same
module. First, `utcnow()`/`add()`/`ago()` — plain UTC delta math (an
expiry, a lease, a retention cutoff) that doesn't need a booted project at
all. Second, `server_timezone()` — the ONE configured `arc_server_timezone`
setting that decides what timezone Postgres timestamps and incoming
naive date/times are interpreted in project-wide; this half *does* need
`arc.boot()`, since it reads a real setting.

**When to use `utcnow`/`add`/`ago`:** Any internal bookkeeping timestamp —
a session/access-key expiry, a background-job lease, an auth lockout
window, a purge cutoff. Deliberately NOT timezone-aware in the
`server_timezone()` sense: an absolute instant like "expires in 30 days"
must stay an absolute instant regardless of what timezone the project
happens to *display* dates in.

```python
import arc

expires_at = arc.tz.add(days=30)              # utcnow() + a delta
lease_until = arc.tz.add(seconds=60)
cutoff = arc.tz.ago(days=retention_days)       # utcnow() - a delta
now = arc.tz.utcnow()                          # datetime.now(timezone.utc)
```

`add()`/`ago()` both take the same keyword deltas (`weeks`, `days`,
`hours`, `minutes`, `seconds`, `milliseconds`, all default `0`) and an
optional `base=` if you want to offset from something other than "now."

**When to use `server_timezone()`:** Rare in plugin code directly — mostly
internal to `psqldb` (connection-level timezone setup) and `relay`
(coercing an incoming naive date/time). Reach for it yourself only if
you're building something else that needs to interpret/display a date/time
the same way the rest of the project does.

```python
tz = arc.tz.server_timezone()   # a real zoneinfo.ZoneInfo, e.g. Asia/Kolkata
local_dt = some_utc_dt.astimezone(tz)
```

Configured via `arc settings set arc_server_timezone <IANA name>` (default
`UTC`); an invalid zone name raises `SettingsError` at the point you call
`server_timezone()`, naming the bad value and the fix command.

### 4.7 `arc.uuid7` — client-side, time-ordered ids

**What it is:** Generates the exact same RFC 9562 UUIDv7 layout psqldb's
own `arc_uuid_generate_v7()` already stamps onto every table's `id` column
server-side by default — bit-for-bit compatible either direction.
Stateless, no project binding needed, works even before `arc.boot()`.

**When to use it:** When you need a real row id BEFORE the `INSERT`
happens — building a batch of related rows in memory, or correlating a
client-generated id across two systems before either is written. If you
don't need the id ahead of time, you don't need this at all: `arc.relay`'s
own `save()` already lets Postgres generate one for you, the normal path.

```python
import arc

new_id = arc.uuid7.generate()   # a real, sortable uuid.UUID
rows = [
    {"id": arc.uuid7.generate(), "parent": batch_id, "name": n}
    for n in names
]
```

**What you get for free:** because the first 48 bits are a millisecond
timestamp, plain sorting by id already sorts by creation time — no
separate "created_at" column needed just to get chronological order
(only guaranteed ACROSS millisecond boundaries; two ids minted in the same
millisecond aren't ordered relative to each other, same as Postgres's own
generator).

```python
created_at = arc.uuid7.extract_timestamp(some_id)   # -> aware UTC datetime
```

Raises `ValueError` if `some_id` isn't actually a version-7 UUID — a v4 id
has no embedded timestamp to extract.

---

## 5. Errors You Might See

| Error class    | Base class     | Raised when… |
|-----------------|----------------|--------------|
| `KernelError`   | `RuntimeError` | The general parent class for anything the Kernel considers a hard failure. `kernel.get("unknown")` raises this directly. |
| `ExportError`   | `KernelError`  | Something about an `export()` call was invalid — bad name, duplicate name, missing `requires`, exporting twice, or not exporting at all. Always a **hard boot failure** — the process will not start. |
| `BootError`     | `KernelError`  | Raised by `arc.boot()` itself, one level up from the Kernel — e.g. "not inside an ARC project," or "a plugin's `register()` raised an unexpected exception." |
| `CryptoError`   | `ValueError`   | Raised by `arc.encrypt()`/`arc.decrypt()` (§4.5) — no project bound and no `key=` given, or (decrypt only) the wrong key / a corrupted or tampered token. Never raised by `arc.hash()`/`arc.verify_hash()`. |
| `ArcAdvisory`   | `UserWarning`  | Not really an error — this is the *warning category* used by `kernel.advise()`. It's never raised as an exception; it's emitted as a Python warning, which by default just prints and continues. |

**The most common one you'll actually hit while writing a plugin is
`ExportError`.** Every message it produces is written to tell you exactly
what to fix — read it fully; it usually names the exact file to change
(`plugin.toml`) and the exact command to run afterward (`arc build`).

---

## 6. A Complete Worked Example

Here's a full, small plugin from scratch — a "greeter" that says hello,
optionally in a custom language if a settings key is configured, and shows
off most of the Kernel methods AND its sibling services (§4) at once.

```python
# plugins/greeter/greeter/__init__.py

import logging

import arc

_logger = logging.getLogger("greeter")


class GreeterProvider:
    def __init__(self, kernel):
        self._kernel = kernel
        self._greeted = 0

    def hello(self, who: str) -> str:
        language = self._kernel.settings.get("greeter_language") or "en"
        greetings = {"en": "Hello", "es": "Hola", "fr": "Bonjour"}
        word = greetings.get(language, "Hello")
        self._greeted += 1
        _logger.info("greeted someone", extra={"who": who, "language": language})
        return f"{word}, {who}! (registered by {self._kernel.current_plugin() or 'nobody right now'})"

    async def health(self) -> dict:
        # Picked up automatically by arc.health.check() — see §4.3.
        return {"ok": True, "greeted_count": self._greeted}


def register(kernel):
    # 1. Read configuration this plugin cares about.
    kernel.settings.declare("greeter_language")

    # 2. Build the thing we're going to share.
    provider = GreeterProvider(kernel)

    # 3. Announce it. No hard requirements; relay is optional (only used
    #    if this project also has an HTTP layer wired up).
    kernel.export("greeter", provider, optional_requires=["relay"])

    # 4. Use an optional capability, if present.
    if kernel.has("relay"):
        relay = kernel.get("relay")
        # ... register an HTTP endpoint using relay, if this plugin wanted to ...

    # 5. Flag something worth knowing, without failing boot over it.
    if kernel.settings.get("greeter_language") is None:
        kernel.advise("greeter: no greeter_language set — defaulting to English.")

    # 6. React to something ANOTHER plugin announced (§4.1) — greeter
    #    doesn't need to know who emits "authn.user.logged_in", or even
    #    whether authn is installed at all.
    @arc.events.on("authn.user.logged_in")
    async def _welcome_new_login(**payload):
        _logger.info("welcoming a fresh login", extra={"user_id": payload.get("user_id")})
```

After boot, anywhere in the app:

```python
import arc
arc.boot()

print(arc.greeter.hello("Vignesh"))
# -> "Hello, Vignesh! (registered by greeter)"
# Also writes one console line + one JSON line in logs/system.jsonl.

print(await arc.health.check())
# -> {"greeter": {"ok": True, "greeted_count": 1}, "psqldb": {...}, ...}
```

---

## 7. Calling Into a Booted Kernel From the Terminal

Sometimes you don't want to write a whole whitelisted function (§4.1's
sibling, relay's own `@arc.relay.whitelist`) or stand up a gateway just to
try something — you want to poke at the running system directly, the way
Frappe's `bench execute`/`bench console` let you. ARC has two commands for
exactly this — both boot the project the same way every other command
does (§1's "where the Kernel comes from"), then additionally open EVERY
capability that has an `open()` method, so a real database connection
(and anything else a plugin needs opened) is already there waiting, with
nothing further to set up.

### 7.1 `arc perform <target> [--args '[...]'] [--kwargs '{...}']`

**What it does:** Calls any function, right now, from your shell, and
prints whatever it returns as JSON.

**TARGET has two forms:**
- `plugin.function_name` — an existing whitelisted function (relay's own
  `@arc.relay.whitelist` — see `docs/arc-relay.MD`), called exactly like
  `arc.relay.call()` would (keyword arguments only, via `--kwargs`).
- `module.path:function_name` (note the colon) — **any function at all**,
  whitelisted or not: a plain helper, a hook function, anything importable
  — imported directly by dotted path and called with `--args`/`--kwargs`.

```bash
arc perform hrms.get_employee --kwargs '{"employee_id": "E001"}'
arc perform hrms.reports:recompute_leave_balance --args '["E001"]'
```

A coroutine function is awaited automatically either way — you never need
to know or care whether the thing you're calling is `async`.

### 7.2 `arc console`

**What it does:** Drops you into a real, interactive Python prompt with
`arc` already booted and every capability already open — top-level
`await` works directly at the prompt, no `asyncio.run(...)` wrapper
needed (the same trick `python -m asyncio` uses, just with `arc` already
imported and booted for you).

```
$ arc console
>>> await arc.relay.list("employee", fields=["full_name"], limit=5)
[{'full_name': 'Vignesh'}, ...]
```

Ctrl-D (or `exit()`) leaves — every capability opened for the session is
closed cleanly on the way out, even after an error.

**When to reach for which:** `arc console` for open-ended poking around
(exploring, debugging, one glance at some data); `arc perform` for a
single, repeatable, scriptable call — a cron job, a one-off data fix, a
step in a CI pipeline.

---

## 8. Common Patterns (Recipes)

**"I need another plugin, and I genuinely can't work without it."**
→ List it in `requires=[...]` when you call `export()`. If it's missing,
boot fails immediately with a clear message — you never have to write your
own "is X installed?" check for a hard dependency.

**"I can use another plugin if it's there, but I have a fallback."**
→ List it in `optional_requires=[...]` (documentation/tooling only), then
actually gate your usage with `if kernel.has("thatplugin"):` at the point
you'd use it.

**"I want to warn about something without stopping the app."**
→ `kernel.advise("...")`.

**"I want to know who's currently registering, from inside a shared
helper."**
→ `kernel.current_plugin()`.

**"I want to list every plugin/capability in the running app."**
→ `kernel.capabilities()`.

**"I need a config value."**
→ `kernel.settings.declare(...)` once in `register()`, then
`kernel.settings.get(...)` whenever you need the value (before or after
boot — the module-level `arc.settings.get(...)` works the same way once
boot has happened).

---

## 9. Common Mistakes to Avoid

- **Calling `export()` more than once per plugin.** Each plugin gets
  exactly one capability. If you have two things to share, put them as two
  attributes/methods on the SAME exported object, not two separate
  `export()` calls.
- **Forgetting to call `export()` at all.** `register(kernel)` MUST export
  something — boot fails loudly if it doesn't, on purpose (a plugin that
  silently does nothing is almost always a bug, not an intentional
  no-op plugin).
- **Using `requires` for something you only sometimes need.** That forces
  every user of your plugin to also install the other one, even if they
  never touch the feature that needs it. Use `optional_requires` +
  `kernel.has(...)` instead.
- **Reaching for `kernel.get("x")` when `arc.x` would do.** Inside
  `register()` either works identically; everywhere else, prefer `arc.x` —
  it's the same lookup, just via the friendlier public spelling.
- **Treating `kernel.advise()` like error handling.** An advisory is for
  "worth knowing," not for "this is actually broken." If something is
  genuinely broken, raise a real exception so boot fails clearly instead of
  limping along.
- **Picking a capability name that's already meaningful to ARC** (like
  `settings`, `log`, `boot`). `export()` will reject this outright — the
  error message will say so — but it saves time to pick a distinctive name
  up front.

---

## 10. Quick Reference

| I want to…                                          | Use this                                   |
|-------------------------------------------------------|---------------------------------------------|
| Share something with the rest of the app               | `kernel.export(name, instance, ...)`        |
| Get another plugin's capability by name                 | `kernel.get(name)` (or `arc.<name>`)        |
| Check if a capability exists before using it            | `kernel.has(name)`                          |
| See every registered capability                         | `kernel.capabilities()`                     |
| Find out which plugin's `register()` is currently running | `kernel.current_plugin()`                 |
| Warn about something non-fatal                           | `kernel.advise(message)`                    |
| See every warning collected so far                       | `kernel.advisories`                         |
| Read/declare a configuration value                        | `kernel.settings.declare(...)` / `.get(...)` |
| Get the project's root folder on disk                    | `kernel.project_root`                       |
| React to something another plugin did                    | `arc.events.on("name", handler)`            |
| Announce that something happened                          | `await arc.events.emit("name", **payload)`  |
| Write a log line                                          | `logging.getLogger("mything").info(...)`    |
| Report per-capability health in one call                  | `await arc.health.check()`                  |
| Encode/decode data the same way every plugin does          | `arc.codec.encode(...)` / `.decode(...)`     |
| Hash a value / compare two hashes safely                   | `arc.hash(...)` / `arc.verify_hash(a, b)`    |
| Encrypt/decrypt a value (reversible)                        | `arc.encrypt(value, key=...)` / `arc.decrypt(token, key=...)` |
| Get "now", or "N days from/ago now", in UTC                 | `arc.tz.utcnow()` / `.add(days=...)` / `.ago(days=...)` |
| Get the project's configured display timezone               | `arc.tz.server_timezone()`                   |
| Generate a real row id before an INSERT happens              | `arc.uuid7.generate()`                       |
| Recover the creation instant embedded in a UUIDv7             | `arc.uuid7.extract_timestamp(id)`            |
| Call any function from the terminal, once                  | `arc perform <target> [--args ...] [--kwargs ...]` |
| Get an interactive prompt with everything booted            | `arc console`                                |

---

**In one sentence:** the Kernel is a name → object lookup table that every
plugin fills in once at startup (`export`) and everyone else reads from
afterward (`get`/`has`/`arc.<name>`), plus a few small helpers
(`advise`, `settings`, `current_plugin`, `project_root`) that make writing
a well-behaved plugin easier — and right alongside it sit `arc.events`
(plugins talking to each other), `arc.log`/`logging` (every plugin's log
lines, sorted into files automatically), `arc.health` (one health check for
every capability), `arc.codec` (one shared way to encode/decode data),
`arc.hash`/`arc.verify_hash`/`arc.encrypt`/`arc.decrypt` (one shared way to
hash and encrypt data), `arc.tz` (UTC delta math plus the project's
configured display timezone), and `arc.uuid7` (client-side, time-ordered
ids matching Postgres's own) — none of them methods on the Kernel object
itself, but all of them part of the same toolbox every plugin gets for
free after `arc.boot()`.
