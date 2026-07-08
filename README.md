# kernel

The ARC kernel — repo/project name is `kernel`; the Python package it
provides is `arc` (flat layout, no `src/`). This is what every project's
`arc.<capability>` namespace is ultimately built on.

```
kernel/                # this repo
├── pyproject.toml      # name = "kernel" (distribution), packages = ["arc"]
├── README.md
├── .gitignore
└── arc/                # the actual package — `import arc`
    ├── __init__.py
    ├── cli.py           # the `arc` command: init, install, build, settings, plugin
    ├── registry.py
    ├── settings.py
    └── secrets.py
```

## Two-tier install model (mirrors frappe-bench / bench)

1. **Bootstrap globally, once** — `uv tool install --editable .` from this repo
   gives you the `arc` command anywhere on your machine.
2. **Per project** — `arc init` clones THIS repo into `<project>/arc/` and wires
   up a uv workspace. From then on, use `<project>/.venv/bin/arc` for project
   commands — that copy is pinned to the exact commit this project was built
   against, independent of the global bootstrap CLI's version.

Note: inside a scaffolded project you'll see `project/arc/arc/` — the outer
`arc/` is this project's kernel *slot* (a workspace member, named the way
`apps/frappe` names its slot in a bench); the inner `arc/` is the actual
Python package. Two different things, same name for a real reason — not
duplication.

## Commands

```bash
arc init [project_name] --kernel-repo <git-url-or-path> [--kernel-branch main] [--env dev]
arc install <git-url> [--branch BRANCH] [--name NAME]
arc build [-p/--plugin NAME] [--no-lock]
arc settings get <key> [--reveal]
arc settings set <key> <value> [--secret]
arc settings delete <key>
arc plugin list
arc plugin enable <name>
arc plugin disable <name>
```

`ARC_KERNEL_REPO` env var can be set once so `--kernel-repo` doesn't need to
be typed on every `arc init`.

## Plugin manifest format

Every plugin repo has, at its root:

```toml
# plugin.toml — ARC-specific metadata only
[plugin]
name = "psqldb"
version = "0.1.0"
capability = "psqldb"          # exported as arc.<capability> at boot
requires = []
optional_requires = ["redix"]
```

```toml
# pyproject.toml — standard Python packaging; real dependencies live HERE
[project]
name = "psqldb"
dependencies = ["asyncpg>=0.29"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

Following this same kernel's convention, plugin packages should also use a
flat layout (`psqldb/psqldb/__init__.py` inside the plugin's own repo, not
`psqldb/src/psqldb/`) — same reasoning, same fix, applied consistently.
