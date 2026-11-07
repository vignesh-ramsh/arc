"""
arc.deploy
-----------------
Two layers, two privilege levels:

  * `arc deploy dev`  — installs systemd (--user) units. No root needed.
    `--enable` also runs `loginctl enable-linger` so they survive
    logout/reboot without needing system-wide (root-owned) units instead —
    "not tied to one login session" without giving up the no-sudo posture.

  * `arc deploy prod` — the same units, installed SYSTEM-wide under
    /etc/systemd/system and run as the project's owner (never root), plus
    nginx (+ certbot for SSL) in front of them on the standard public port
    (80, or 443 with --ssl). Needs root: writes to /etc/systemd/system and
    /etc/nginx/, calls apt/certbot. Every operation here is scoped to THIS
    project's own units and site file by name — nginx is very often already
    fronting OTHER, unrelated domains on the same box, and nothing here may
    ever touch those.

Both install ONE UNIT PER PROCESS, grouped under `<name>.target`: the log
writer, the gateway, one lineup worker process per queue (or more — see
arc.sizing), and the scheduler. systemd restarts exactly the process that
died, in its own cgroup, with a start limit so a crash loop ends in a visible
`failed` unit instead of spinning; no unit requires another, so one process
dying never takes a sibling with it. (Measured against a real user-systemd
manager: killing a job worker, a web worker, the gateway master or the log
writer never took the site down longer than the gateway's own restart.)

Deliberately narrow, matching docs/arc-kernel-event-process-notification-
proposal.md §13: the Kernel itself stays supervisor-blind (arc.events, `arc
run`, every other core command know nothing about systemd/nginx) — this
module is OPT-IN TOOLING, invoked only by the explicit, user-run `arc deploy`
commands. Nothing else in ARC depends on it existing.

Safe by default: `arc deploy dev` writes STOPPED, not-enabled units unless
--enable is passed — the server should start only when a developer asks it
to, never silently on the next reboot, quietly consuming DB/Redis
connections nobody asked for. (`arc deploy prod` is the always-on posture.)
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import subprocess
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import sizing


class DeployError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Where units go: per-user (dev) or system-wide (prod)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Scope:
    name: str  # "user" | "system"
    unit_dir: Path
    ctl: tuple[str, ...]  # the systemctl invocation for this manager
    wanted_by: str  # what the target hangs off, so it starts on boot/login
    run_as: tuple[str, str] | None = None  # (user, group) for system units


def user_scope() -> Scope:
    return Scope(
        "user",
        Path.home() / ".config" / "systemd" / "user",
        ("systemctl", "--user"),
        "default.target",
    )


def system_scope(user: str, group: str) -> Scope:
    """Units in /etc/systemd/system, run as `user:group` — the project's owner,
    never root."""
    return Scope(
        "system",
        Path("/etc/systemd/system"),
        ("systemctl",),
        "multi-user.target",
        (user, group),
    )


def project_owner(project_root: Path) -> tuple[str, str]:
    """(user, group) that own the project directory: who the system units run as."""
    import grp
    import pwd

    st = project_root.stat()
    return pwd.getpwuid(st.st_uid).pw_name, grp.getgrgid(st.st_gid).gr_name


def _ctl(scope: Scope, *args: str) -> None:
    result = subprocess.run([*scope.ctl, *args])
    if result.returncode != 0:
        raise DeployError(f"{' '.join(scope.ctl)} {' '.join(args)} exited with code {result.returncode}.")


def _ctl_try(scope: Scope, *args: str) -> bool:
    """Best effort: a unit that doesn't exist or isn't running is not an error here."""
    return subprocess.run([*scope.ctl, *args], capture_output=True).returncode == 0


# --------------------------------------------------------------------------- #
# The units
# --------------------------------------------------------------------------- #
#: First line of every unit file we write. It is how a later run recognises what is
#: its own (stale units from an older, bigger deployment) — never a guess from the
#: file name, which a queue called "gateway" or a project called "shop-worker" would fool.
MARKER = "# managed by `arc deploy` — base={base}"
_MARKER_RE = re.compile(r"^# managed by `arc deploy` — base=(\S+)$")

#: Restart=always rather than on-failure: a lineup worker that exits 0 on its own is
#: still lost capacity. `systemctl stop` is honoured either way. The start limit (5
#: starts in 120 s) is what turns a crash loop into a visible `failed` unit instead of
#: an endless restart storm. (systemd 249, e.g. Ubuntu 22.04, has no exponential
#: RestartSteps, so the delay is fixed.)
SERVICE_TEMPLATE = """{marker}
[Unit]
Description=ARC {role} ({project_name})
PartOf={base}.target
After=network.target redis-server.service postgresql.service{logs_after}
{logs_wants}StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
{run_as}WorkingDirectory={project_root}
Environment=ARC_LOG_SOCKET={log_socket}
{role_env}ExecStart={exec_start}
Restart=always
RestartSec=3
TimeoutStopSec=30
"""

TARGET_TEMPLATE = """{marker}
[Unit]
Description=ARC ({project_name}) — every process, supervised separately
Wants={wants}

[Install]
WantedBy={wanted_by}
"""


@dataclass(frozen=True)
class SplitSpec:
    project_root: Path
    arc_bin: str
    host: str
    port: int
    log_socket: Path
    #: None = the gateway plugin is not installed/enabled
    gateway_workers: int | None
    #: queue name -> number of worker processes for it; empty = lineup is not installed
    queue_workers: dict[str, int]
    scheduler: bool


def base_name(project_root: Path, *, name: str | None = None) -> str:
    return name or f"arc-{project_root.name}"


def _unit_part(queue: str) -> str:
    """A queue name as it appears in a unit name. systemd allows [A-Za-z0-9:_.-];
    '-' is kept out so the `-<index>` suffix stays unambiguous."""
    return re.sub(r"[^A-Za-z0-9_.]", "_", queue)


def split_unit_texts(base: str, spec: SplitSpec, scope: Scope | None = None) -> dict[str, str]:
    """{unit file name: text} for every unit of the deployment, including the
    target that groups them. Pure."""
    scope = scope or user_scope()
    marker = MARKER.format(base=base)
    run_as = f"User={scope.run_as[0]}\nGroup={scope.run_as[1]}\n" if scope.run_as else ""
    all_queues = ",".join(spec.queue_workers)

    # (unit stem, description, command, process role). The role is what ARC_PROCESS_ROLE tells the
    # process it is, so it can take its share of the database connection budget; the log writer
    # has no database connection and no role.
    processes: list[tuple[str, str, str, str | None]] = [
        ("logs", "log writer", f"{spec.arc_bin} logs serve", None),
    ]
    if spec.gateway_workers is not None:
        processes.append(
            (
                "gateway",
                "gateway",
                f"{spec.arc_bin} gateway serve --host {spec.host} --port {spec.port} "
                f"--workers {spec.gateway_workers}",
                sizing.ROLE_GATEWAY,
            )
        )
    seen: dict[str, str] = {}
    for queue, count in spec.queue_workers.items():
        part = _unit_part(queue)
        if part in seen:
            raise DeployError(
                f"Queues {seen[part]!r} and {queue!r} would share unit names; rename one."
            )
        seen[part] = queue
        for i in range(1, count + 1):
            processes.append(
                (
                    f"worker-{part}-{i}",
                    f"lineup worker, queue {queue} ({i}/{count})",
                    f"{spec.arc_bin} lineup worker --queues {queue}",
                    sizing.ROLE_LINEUP_WORKER,
                )
            )
    if spec.scheduler and all_queues:
        processes.append(
            (
                "scheduler",
                "lineup scheduler",
                f"{spec.arc_bin} lineup scheduler --queues {all_queues}",
                sizing.ROLE_LINEUP_SCHEDULER,
            )
        )

    texts: dict[str, str] = {}
    for stem, role, command, process_role in processes:
        is_logs = stem == "logs"
        texts[f"{base}-{stem}.service"] = SERVICE_TEMPLATE.format(
            marker=marker,
            role=role,
            role_env=f"Environment=ARC_PROCESS_ROLE={process_role}\n" if process_role else "",
            project_name=spec.project_root.name,
            base=base,
            run_as=run_as,
            project_root=spec.project_root,
            log_socket=spec.log_socket,
            exec_start=command,
            # Wants (not Requires): the log writer going down must never take a
            # process with it; they fall back to logging to files of their own.
            logs_after="" if is_logs else f" {base}-logs.service",
            logs_wants="" if is_logs else f"Wants={base}-logs.service\n",
        )
    texts[f"{base}.target"] = TARGET_TEMPLATE.format(
        marker=marker,
        project_name=spec.project_root.name,
        wants=" ".join(name for name in texts if name.endswith(".service")),
        wanted_by=scope.wanted_by,
    )
    return texts


def split_unit_names(base: str, spec: SplitSpec, scope: Scope | None = None) -> list[str]:
    return list(split_unit_texts(base, spec, scope))


def _owned_units(scope: Scope, base: str) -> list[str]:
    """Every unit file in the unit directory carrying OUR marker for `base`."""
    if not scope.unit_dir.is_dir():
        return []
    owned = []
    for path in sorted(scope.unit_dir.glob("*.service")):
        try:
            first = path.read_text(encoding="utf-8").split("\n", 1)[0]
        except OSError:
            continue
        match = _MARKER_RE.match(first)
        if match and match.group(1) == base:
            owned.append(path.name)
    return owned


# --------------------------------------------------------------------------- #
# Reading back what is deployed (arc status, arc doctor --deep)
# --------------------------------------------------------------------------- #
_EXEC_RE = re.compile(r"^ExecStart=(?P<arc>\S+) (?P<cmd>.+?)\s*$", re.MULTILINE)
_GATEWAY_RE = re.compile(r"^gateway serve --host (\S+) --port (\d+) --workers (\d+)$")
_WORKER_RE = re.compile(r"^lineup worker --queues (\S+)$")
_SCHEDULER_RE = re.compile(r"^lineup scheduler --queues (\S+)$")


@dataclass(frozen=True)
class DeployedUnit:
    """One process unit as written by install_split, read back from its file."""

    name: str
    role: str  # "logs" | "gateway" | "worker" | "scheduler"
    queue: str | None = None  # worker: the one queue it consumes
    queues: tuple[str, ...] = ()  # scheduler: every queue it polls
    workers: int | None = None  # gateway: its --workers
    host: str | None = None
    port: int | None = None


@dataclass(frozen=True)
class Deployment:
    base: str
    scope: Scope
    units: tuple[DeployedUnit, ...]
    target_installed: bool

    @property
    def target(self) -> str:
        return f"{self.base}.target"


def _parse_unit(name: str, text: str) -> DeployedUnit | None:
    match = _EXEC_RE.search(text)
    if not match:
        return None
    cmd = match.group("cmd")
    if cmd == "logs serve":
        return DeployedUnit(name, "logs")
    if m := _GATEWAY_RE.match(cmd):
        return DeployedUnit(name, "gateway", workers=int(m.group(3)), host=m.group(1), port=int(m.group(2)))
    if m := _WORKER_RE.match(cmd):
        return DeployedUnit(name, "worker", queue=m.group(1))
    if m := _SCHEDULER_RE.match(cmd):
        return DeployedUnit(name, "scheduler", queues=tuple(m.group(1).split(",")))
    return None


def read_deployments(project_root: Path, scopes: list[Scope] | None = None) -> list[Deployment]:
    """Every deployment `arc deploy` has installed for THIS project, read from the unit
    files themselves: a unit is ours when its first line is our marker and its
    WorkingDirectory is the project. Empty when the project isn't deployed that way."""
    scopes = scopes if scopes is not None else [user_scope(), system_scope("", "")]
    found: list[Deployment] = []
    for scope in scopes:
        if not scope.unit_dir.is_dir():
            continue
        by_base: dict[str, list[DeployedUnit]] = {}
        for path in sorted(scope.unit_dir.glob("*.service")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            marker = _MARKER_RE.match(text.split("\n", 1)[0])
            if not marker or f"\nWorkingDirectory={project_root}\n" not in text:
                continue
            unit = _parse_unit(path.name, text)
            if unit is not None:
                by_base.setdefault(marker.group(1), []).append(unit)
        for base, units in by_base.items():
            found.append(
                Deployment(base, scope, tuple(units), (scope.unit_dir / f"{base}.target").is_file())
            )
    return found


def deployed_worker_queues(project_root: Path, scopes: list[Scope] | None = None) -> set[str] | None:
    """The queues the worker units `arc deploy` installed for THIS project consume, or None
    if no such units exist (the project isn't deployed that way, so there is nothing to
    compare against)."""
    deployments = read_deployments(project_root, scopes)
    if not deployments:
        return None
    return {u.queue for d in deployments for u in d.units if u.role == "worker" and u.queue}


def install_split(*, base: str, spec: SplitSpec, scope: Scope, enable: bool) -> list[Path]:
    """Write every unit and daemon-reload. With enable=False it only writes files and
    never changes what is enabled or running, so re-running can't take a live site
    down. With enable=True it also switches over: a leftover single `arc run` unit for
    this project (the old shape — same ports) is stopped and disabled first, units from
    an older, bigger deployment are stopped and removed, then the target is enabled and
    (re)started."""
    texts = split_unit_texts(base, spec, scope)
    scope.unit_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for unit, text in texts.items():
        path = scope.unit_dir / unit
        path.write_text(text, encoding="utf-8")
        written.append(path)
    stale = [u for u in _owned_units(scope, base) if u not in texts]
    _ctl(scope, "daemon-reload")

    if enable:
        legacy = f"{base}.service"
        legacy_path = scope.unit_dir / legacy
        if legacy_path.is_file() and not _MARKER_RE.match(
            legacy_path.read_text(encoding="utf-8").split("\n", 1)[0]
        ):
            _ctl_try(scope, "stop", legacy)  # same ports; two supervisors must not both run
            _ctl_try(scope, "disable", legacy)
        for unit in stale:
            _ctl_try(scope, "stop", unit)
            (scope.unit_dir / unit).unlink(missing_ok=True)
        if stale:
            _ctl(scope, "daemon-reload")
        _ctl(scope, "enable", f"{base}.target")
        _ctl(scope, "restart", f"{base}.target")  # restart: picks up new unit content too
    return written


def enable_linger(user: str) -> None:
    """A `systemctl --user` unit normally only runs while `user` has an
    active login session — lingering keeps systemd running that user's
    units across logout and reboot without a system-wide (root-owned)
    unit instead. Best-effort from the caller's side (cli.py only warns,
    never aborts, if this fails) — some systems require polkit/admin
    approval for a user to enable their own lingering, which `arc deploy
    dev` shouldn't be blocked on."""
    result = subprocess.run(["loginctl", "enable-linger", user])
    if result.returncode != 0:
        raise DeployError(f"loginctl enable-linger {user} exited with code {result.returncode}.")




# ------------------------------------------------------------------------ #
# arc deploy prod / arc disable prod
# ------------------------------------------------------------------------ #
NGINX_MAIN_CONF = Path("/etc/nginx/nginx.conf")
NGINX_SITES_AVAILABLE = Path("/etc/nginx/sites-available")
NGINX_SITES_ENABLED = Path("/etc/nginx/sites-enabled")
NGINX_CONF_D = Path("/etc/nginx/conf.d")


def nginx_active_sites_dir() -> Path:
    """Where THIS nginx actually loads extra site configs from — read
    directly from nginx.conf's own `include` directives, never assumed.
    The standard sites-available/sites-enabled Debian convention isn't
    always what's actually wired up: confirmed directly against this
    project's own box, whose nginx.conf only includes conf.d/*.conf —
    sites-enabled sits there unreferenced, a dead leftover from an
    earlier setup, and a site written there is never actually served
    even though `nginx -t`/reload both report success (they're just not
    looking at it). conf.d has no separate available/enabled split — a
    file's mere presence there is what serves it. Falls back to
    sites-enabled (the more common from-scratch-install default) if
    nginx.conf can't be read, or names neither pattern."""
    try:
        text = NGINX_MAIN_CONF.read_text()
    except OSError:
        return NGINX_SITES_ENABLED
    if "sites-enabled" in text:
        return NGINX_SITES_ENABLED
    if "conf.d" in text:
        return NGINX_CONF_D
    return NGINX_SITES_ENABLED

NGINX_SITE_TEMPLATE = """# Managed by `arc deploy prod` for {project_name} — regenerated on every
# re-run, so hand edits below will be lost. certbot --nginx may add its
# own server block(s) (443/redirect) alongside this one; those are left
# alone by a re-run and only removed entirely by `arc disable prod`.
{login_zone}
server {{
    listen {public_port};
    listen [::]:{public_port};
    server_name {domain};

    client_max_body_size 100m;
{metrics_block}{login_block}
    location / {{
        proxy_pass http://127.0.0.1:{internal_port};
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket upgrade — without these, gateway's /ws/* routes (e.g.
        # wschat) fail to connect the moment this sits behind nginx, even
        # though they work talking to the app directly. proxy_read_timeout
        # is bumped well past nginx's 60s default too: a WS connection is
        # meant to sit open and mostly idle, which the default would kill.
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }}
}}
"""


def site_name(project_root: Path, *, name: str | None = None) -> str:
    # Same convention as unit_name() — derived from the project directory,
    # not domain, so this can never collide with an unrelated site's own
    # config file just because two projects happen to share a domain
    # naming scheme.
    return name or ("arc-" + project_root.name)


#: Always allowed to read /metrics through nginx: this machine. A Prometheus on the same host can
#: also scrape the app's own 127.0.0.1:<port> directly, which does not go through nginx at all.
METRICS_ALWAYS_ALLOWED = ("127.0.0.1", "::1")


def parse_ip_list(setting: str, text: str | None) -> list[str]:
    """A comma-separated setting of IP addresses or CIDR ranges (e.g. `203.0.113.7,10.0.0.0/24`),
    normalised to CIDR form. Each is parsed as an address, never passed through as text, so a value
    cannot add its own nginx directives. Raises DeployError naming the setting and the bad entry."""
    networks: list[str] = []
    for item in (text or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            raise DeployError(
                f"{setting}: {item!r} is not an IP address or CIDR range "
                f"(expected e.g. 203.0.113.7 or 10.0.0.0/24)."
            ) from None
        networks.append(str(network))
    return networks


def parse_metrics_allow(text: str | None) -> list[str]:
    """`deploy_metrics_allow`: who may read /metrics through nginx besides this machine (e.g. the
    Prometheus server)."""
    return parse_ip_list("deploy_metrics_allow", text)


def _metrics_block(internal_port: int, extra_allowed: Sequence[str]) -> str:
    allow = "".join(f"        allow {addr};\n" for addr in (*METRICS_ALWAYS_ALLOWED, *extra_allowed))
    return f"""
    # /metrics is for scrapers, not the internet. gateway_metrics_token is empty by default, so
    # without this block every request for it would be proxied and answered. Only this machine
    # and the addresses in the `deploy_metrics_allow` setting get through.
    location ~ ^/metrics(/|$) {{
{allow}        deny all;

        proxy_pass http://127.0.0.1:{internal_port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
"""


#: Logins one IP address may make through nginx: this many per second, with room for a burst. The
#: app limits attempts per (IP, account), which an attacker beats by changing the account on every
#: try - and each attempt costs a memory-hungry Argon2 hash. This bounds one source regardless of
#: the account it names; beyond it nginx answers 429 without bothering the app. An office of
#: hundreds behind one address must still fit: 10/s with a burst of 50 passes 300 logins that arrive
#: within 25 seconds, and `nginx_login_allow` exempts a trusted network altogether.
DEFAULT_LOGIN_RATE = "10r/s"
DEFAULT_LOGIN_BURST = 50
_LOGIN_RATE = re.compile(r"(\d{1,5})(?:r/([sm]))?")


def parse_login_rate(text: str | None) -> str:
    """`nginx_login_rate` as an nginx rate: `10` or `10r/s` (per second), `300r/m` (per minute).
    Empty means the default. Raises DeployError."""
    if text is None or not str(text).strip():
        return DEFAULT_LOGIN_RATE
    match = _LOGIN_RATE.fullmatch(str(text).strip())
    if match is None or int(match.group(1)) < 1:
        raise DeployError(
            f"nginx_login_rate: {text!r} is not a rate (expected e.g. 10, 10r/s or 300r/m; at least 1)."
        )
    return f"{int(match.group(1))}r/{match.group(2) or 's'}"


def parse_login_burst(text: str | None) -> int:
    """`nginx_login_burst`: how many logins beyond the rate one address may make at once (0 to
    1000). Empty means the default. Raises DeployError."""
    if text is None or not str(text).strip():
        return DEFAULT_LOGIN_BURST
    try:
        burst = int(str(text).strip())
    except ValueError:
        burst = -1
    if not 0 <= burst <= 1000:
        raise DeployError(f"nginx_login_burst: {text!r} is not a whole number from 0 to 1000.")
    return burst


def _login_zone_name(site: str) -> str:
    # one zone per site file: nginx refuses two zones of the same name anywhere in its config
    return "arc_login_" + re.sub(r"[^A-Za-z0-9]", "_", site)


def _login_zone(zone: str, rate: str, exempt: Sequence[str]) -> str:
    """The limit_req_zone, keyed by the visitor's address. With exempt networks the key is built
    through geo + map so that those addresses get an EMPTY key, which nginx does not count at all
    (the documented way to exempt a source from limit_req). The variables are named after the zone
    because nginx variables are global across everything it loads."""
    if not exempt:
        return f"limit_req_zone $binary_remote_addr zone={zone}:10m rate={rate};\n"
    networks = "".join(f"    {network} 1;\n" for network in exempt)
    return (
        f"geo $remote_addr ${zone}_trusted {{\n    default 0;\n{networks}}}\n"
        f"map ${zone}_trusted ${zone}_key {{\n    0 $binary_remote_addr;\n    1 \"\";\n}}\n"
        f"limit_req_zone ${zone}_key zone={zone}:10m rate={rate};\n"
    )


def _login_block(internal_port: int, zone: str, burst: int) -> str:
    return f"""
    # Per-address limit on /login (settings nginx_login_rate, nginx_login_burst, nginx_login_allow).
    # This assumes nginx sees each visitor's real address; behind a CDN or another proxy configure
    # real_ip first, or every visitor shares one allowance.
    location = /login {{
        limit_req zone={zone} burst={burst} nodelay;
        limit_req_status 429;

        proxy_pass http://127.0.0.1:{internal_port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
"""


def generate_nginx_conf(
    *,
    project_name: str,
    domain: str,
    internal_port: int,
    public_port: int,
    metrics_allow: Sequence[str] = (),
    site: str | None = None,
    login_rate: str = DEFAULT_LOGIN_RATE,
    login_burst: int = DEFAULT_LOGIN_BURST,
    login_allow: Sequence[str] = (),
) -> str:
    zone = _login_zone_name(site or project_name)
    return NGINX_SITE_TEMPLATE.format(
        project_name=project_name,
        domain=domain,
        internal_port=internal_port,
        public_port=public_port,
        metrics_block=_metrics_block(internal_port, metrics_allow),
        login_block=_login_block(internal_port, zone, login_burst),
        login_zone=_login_zone(zone, login_rate, login_allow),
    )


def require_root() -> None:
    if os.geteuid() != 0:
        raise DeployError(
            "This needs root — /etc/nginx and certbot aren't writable otherwise. "
            "Re-run with sudo: `sudo arc deploy prod ...`."
        )


def nginx_active() -> bool:
    result = subprocess.run(["systemctl", "is-active", "nginx"], capture_output=True, text=True)
    return result.stdout.strip() == "active"


def port_blocked_by_other(port: int) -> bool:
    """True only if something OTHER than nginx already owns `port`. An
    ALREADY-RUNNING nginx owning 80/443 is the normal, correct state for
    adding another domain — nginx virtual-hosts many domains on the same
    port via server_name, so that's never a real conflict (confirmed
    directly against this project's own box: nginx already had 80/443
    bound, serving unrelated sites, and that's fine). The raw bind-test
    below is only meaningful before nginx is even running yet, to catch a
    genuine squatter (Apache, some other process) that would keep nginx
    itself from starting."""
    if nginx_active():
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return True
        return False


def domain_already_configured_elsewhere(domain: str, *, own_site: str) -> str | None:
    """Best-effort check for a DIFFERENT site file already claiming this
    exact domain (nginx would silently pick whichever matches first,
    shadowing the loser) — returns that file's name, or None. Checks
    every directory a config might plausibly live in (not just whichever
    one nginx.conf currently wires up) since the goal here is just
    catching an existing claim, not deciding where to write. Never
    raises; a box with none of these directories just reports no
    collision."""
    for directory in (NGINX_SITES_AVAILABLE, NGINX_CONF_D):
        if not directory.is_dir():
            continue
        for path in directory.glob("*"):
            if path.name in (own_site, f"{own_site}.conf") or not path.is_file():
                continue
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            if f"server_name {domain}" in text or f"server_name {domain};" in text:
                return f"{directory}/{path.name}"
    return None


def resolve_domain(domain: str) -> str | None:
    try:
        return socket.gethostbyname(domain)
    except OSError:
        return None


def public_ip() -> str | None:
    """Best-effort, advisory only — used to warn (never hard-block) when a
    domain doesn't look like it points at this box yet, before certbot's
    own HTTP-01 challenge would fail on it anyway with a noisier error.
    No internet egress, or the lookup service itself being unreachable,
    just silently skips the warning rather than blocking on it."""
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=3) as resp:
            return resp.read().decode().strip()
    except OSError:
        return None


def ensure_nginx_and_certbot_installed(*, need_certbot: bool) -> None:
    missing = []
    if shutil.which("nginx") is None:
        missing.append("nginx")
    if need_certbot and shutil.which("certbot") is None:
        missing += ["certbot", "python3-certbot-nginx"]
    if not missing:
        return
    if shutil.which("apt-get") is None:
        raise DeployError(
            f"Missing: {', '.join(missing)}. This isn't a Debian/Ubuntu (apt) box — "
            f"install these yourself, then re-run."
        )
    result = subprocess.run(["apt-get", "install", "-y", *missing])
    if result.returncode != 0:
        raise DeployError(f"apt-get install failed (exit {result.returncode}).")


def install_nginx_site(*, site: str, text: str) -> Path:
    """Writes THIS site's own config file wherever nginx.conf actually
    loads extra sites from (nginx_active_sites_dir()) — sites-enabled
    (via a sites-available/sites-enabled symlink, the standard Debian
    convention) or conf.d (a single directory, no separate enabled step —
    see nginx_active_sites_dir's own docstring for why this isn't just
    assumed). Validates with `nginx -t` BEFORE reloading, and rolls the
    file back out if validation fails. nginx here is very often already
    serving other, unrelated sites; this must never leave it in a state
    where a plain reload would take THEM down too."""
    target_dir = nginx_active_sites_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{site}.conf"

    if target_dir == NGINX_SITES_ENABLED:
        NGINX_SITES_AVAILABLE.mkdir(parents=True, exist_ok=True)
        available = NGINX_SITES_AVAILABLE / f"{site}.conf"
        previous = available.read_text() if available.is_file() else None
        available.write_text(text)
        if not target.exists():
            target.symlink_to(available)
    else:
        previous = target.read_text() if target.is_file() else None
        target.write_text(text)

    check = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if check.returncode != 0:
        if target_dir == NGINX_SITES_ENABLED:
            if previous is not None:
                available.write_text(previous)
            else:
                available.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
        elif previous is not None:
            target.write_text(previous)
        else:
            target.unlink(missing_ok=True)
        raise DeployError(f"nginx config is invalid, rolled back:\n{check.stderr}")

    reload_result = subprocess.run(["systemctl", "reload", "nginx"])
    if reload_result.returncode != 0:
        raise DeployError(f"systemctl reload nginx exited with code {reload_result.returncode}.")

    return target


def run_certbot(*, domain: str, email: str | None) -> None:
    args = ["certbot", "--nginx", "-d", domain, "--non-interactive", "--agree-tos", "--redirect"]
    args += ["-m", email] if email else ["--register-unsafely-without-email"]
    result = subprocess.run(args)
    if result.returncode != 0:
        raise DeployError(
            f"certbot exited with code {result.returncode} — see its output above. Common "
            f"cause: '{domain}' doesn't resolve to this server yet, or port 80 isn't reachable "
            f"from the internet (firewall/security group) — certbot's default validation needs "
            f"that regardless of which port you end up serving on."
        )


def disable_nginx_site(*, site: str) -> bool:
    """Removes THIS project's own site file (+ its sites-enabled symlink,
    for the layout that has one) only — every OTHER site on this nginx
    instance is untouched. Checks BOTH possible layouts, not just
    whichever nginx.conf currently wires up — a stray file left in the
    inactive one (e.g. from before a prior run corrected which layout is
    real) should still get cleaned up. Deliberately leaves any SSL
    certificate on disk (under /etc/letsencrypt/) rather than revoking it
    — a future `arc deploy prod --ssl` re-run reuses it instead of
    requesting a new one, avoiding Let's Encrypt's issuance rate limits on
    a disable/re-enable cycle. Returns whether anything was actually there
    to remove."""
    available = NGINX_SITES_AVAILABLE / f"{site}.conf"
    enabled = NGINX_SITES_ENABLED / f"{site}.conf"
    conf_d = NGINX_CONF_D / f"{site}.conf"
    existed = available.is_file() or enabled.is_symlink() or conf_d.is_file()
    enabled.unlink(missing_ok=True)
    available.unlink(missing_ok=True)
    conf_d.unlink(missing_ok=True)
    if existed and shutil.which("nginx") is not None:
        check = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
        if check.returncode == 0:
            subprocess.run(["systemctl", "reload", "nginx"])
        else:
            raise DeployError(f"nginx config invalid after removing the site:\n{check.stderr}")
    return existed
