"""
arc.deploy
-----------------
Two layers, two privilege levels:

  * `arc deploy dev`  — generates and installs a systemd (--user) unit
    whose ExecStart is `arc run` (gateway + lineup worker(s) + lineup
    scheduler, supervised as ONE unit). No root needed. `--enable` also
    runs `loginctl enable-linger` so the unit survives logout/reboot
    without needing a system-wide (root-owned) unit instead — "not tied
    to one login session" without giving up the no-sudo posture.

  * `arc deploy prod` — nginx (+ certbot for SSL) in front of an already-
    running `arc run`, on the standard public port (80, or 443 with
    --ssl). Needs root: writes to /etc/nginx/, calls apt/certbot. Every
    operation here is scoped to THIS project's own site file by name —
    nginx is very often already fronting OTHER, unrelated domains on the
    same box, and nothing here may ever touch those.

Deliberately narrow, matching docs/arc-kernel-event-process-notification-
proposal.md §13: the Kernel itself stays supervisor-blind (arc.events,
`arc run`, every other core command know nothing about systemd/nginx) —
this module is OPT-IN TOOLING, invoked only by the explicit, user-run
`arc deploy` commands. Nothing else in ARC depends on it existing.

Safe by default: `arc deploy dev` always writes a STOPPED, not-enabled
unit unless --enable is passed — the server should start only when a
developer actually runs `arc run`, never silently on the next reboot,
quietly consuming DB/Redis connections nobody asked for.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path

UNIT_TEMPLATE = """[Unit]
Description=ARC server ({project_name}) — gateway + lineup, via `arc run`
After=network.target redis-server.service postgresql.service

[Service]
Type=simple
WorkingDirectory={project_root}
ExecStart={arc_bin} run --host {host} --port {port}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""


class DeployError(RuntimeError):
    pass


def unit_name(project_root: Path, *, name: str | None = None) -> str:
    # Derived from the project directory, not a fixed "arc-server" —
    # avoids a silent collision if this ever runs for a second ARC
    # project on the same box.
    return f"{name or ('arc-' + project_root.name)}.service"


def unit_path(unit: str) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / unit


def generate_unit_text(*, project_root: Path, arc_bin: str, host: str, port: int) -> str:
    return UNIT_TEMPLATE.format(
        project_name=project_root.name,
        project_root=project_root,
        arc_bin=arc_bin,
        host=host,
        port=port,
    )


def _systemctl(*args: str) -> None:
    result = subprocess.run(["systemctl", "--user", *args])
    if result.returncode != 0:
        raise DeployError(
            f"systemctl --user {' '.join(args)} exited with code {result.returncode}."
        )


def is_enabled(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-enabled", unit],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "enabled"


def install(
    *,
    project_root: Path,
    arc_bin: str,
    host: str,
    port: int,
    name: str | None = None,
    enable: bool,
) -> tuple[str, Path, bool]:
    """Writes the unit file and runs daemon-reload unconditionally — safe
    to call repeatedly, always refreshes the content (a changed port, a
    moved venv). `enable=True` additionally enables (survives reboot) and
    (re)starts it now. `enable=False` — the default — NEVER disables or
    stops a unit that's already enabled/running from a previous run: it
    only rewrites the file and reloads, leaving whatever state the
    operator already chose untouched, so re-running `arc deploy setup`
    with no flags can never silently take a production instance down.

    Returns (unit_name, unit_path, already_existed)."""
    unit = unit_name(project_root, name=name)
    path = unit_path(unit)
    already_existed = path.is_file()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        generate_unit_text(project_root=project_root, arc_bin=arc_bin, host=host, port=port)
    )

    _systemctl("daemon-reload")

    if enable:
        _systemctl("enable", unit)
        _systemctl(
            "restart", unit
        )  # restart, not start — picks up new content even if already running

    return unit, path, already_existed


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
server {{
    listen {public_port};
    listen [::]:{public_port};
    server_name {domain};

    client_max_body_size 100m;

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


def generate_nginx_conf(*, project_name: str, domain: str, internal_port: int, public_port: int) -> str:
    return NGINX_SITE_TEMPLATE.format(
        project_name=project_name, domain=domain, internal_port=internal_port, public_port=public_port
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
