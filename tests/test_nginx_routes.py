"""nginx limits and timeouts per route (arc/deploy.py `make_route_plan`, `generate_nginx_conf`).

The site `arc deploy prod` writes used to give every path the same treatment: a 100 MB body
ceiling (the gateway's own is 10 MB, so nginx buffered ten times what any route but an upload could
use) and a one-hour read timeout (needed by WebSockets, which are meant to sit open, and by nothing
else, so a hung request held a connection for an hour). Now the gateway reports what each route
declares (`arc gateway routes --json`), and nginx gets the shared body ceiling and an ordinary
timeout everywhere, with a location of its own for each upload path and each WebSocket.

The last class runs a real nginx (a private instance in a temp directory, never the system one)
in front of a stub backend, because what matters is what nginx does with the request: which paths
it lets a big body through to, whether the backend sees upgrade headers, who times out and when.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from arc import cli, deploy

MB = 1024 * 1024


def route(path, *, method="POST", body=None, websocket=False) -> dict:
    return {"method": "WEBSOCKET" if websocket else method, "path": path, "max_body_bytes": body, "websocket": websocket}


def plan(*routes, default=10 * MB) -> deploy.RoutePlan:
    return deploy.make_route_plan({"max_body_bytes": default, "routes": list(routes)})


def conf(**kw) -> str:
    return deploy.generate_nginx_conf(project_name="shop", domain="shop.example", internal_port=8001, public_port=80, **kw)


def location_block(text: str, header: str) -> str:
    start = text.index(header)
    return text[start : text.index("    }\n", start)]


class TestWhichPathsGetALocation:
    def test_a_gateway_with_nothing_special_needs_no_locations(self):
        result = plan(route("/api/v1/things"), route("/login"), route("/things/{id}", method="GET"))
        assert result.locations == () and result.default_body_bytes == 10 * MB

    def test_an_upload_route_gets_its_own_ceiling(self):
        (loc,) = plan(route("/files/upload", body=60 * MB)).locations
        assert (loc.path, loc.body_bytes, loc.websocket) == ("/files/upload", 60 * MB, False)

    def test_a_websocket_gets_a_location_with_no_body_ceiling_of_its_own(self):
        (loc,) = plan(route("/ws/chat/{room}", websocket=True)).locations
        assert (loc.path, loc.body_bytes, loc.websocket) == ("/ws/chat/{room}", None, True)

    def test_a_ceiling_at_or_below_the_shared_one_is_left_to_the_gateway(self):
        result = plan(route("/small", body=1 * MB), route("/same", body=10 * MB), default=10 * MB)
        assert result.locations == ()

    def test_methods_sharing_a_path_share_a_location_with_the_largest_ceiling(self):
        (loc,) = plan(route("/things/{id}/file", method="PUT", body=20 * MB), route("/things/{id}/file", method="POST", body=50 * MB)).locations
        assert loc.body_bytes == 50 * MB

    def test_a_path_that_is_both_gets_one_location(self):
        (loc,) = plan(route("/live", body=30 * MB), route("/live", websocket=True)).locations
        assert (loc.body_bytes, loc.websocket) == (30 * MB, True)

    def test_the_gateway_ignores_extra_slashes_so_paths_that_differ_only_by_them_are_one(self):
        (loc,) = plan(route("/files/upload", body=20 * MB), route("//files//upload/", body=30 * MB)).locations
        assert loc.path == "/files/upload" and loc.body_bytes == 30 * MB

    def test_the_shared_ceiling_is_the_gateways_own(self):
        assert plan(default=3 * MB).default_body_bytes == 3 * MB
        assert plan(route("/up", body=4 * MB), default=3 * MB).locations[0].body_bytes == 4 * MB
        assert plan(route("/up", body=3 * MB), default=3 * MB).locations == ()  # now not above it

    def test_the_biggest_ceiling_is_tried_first_so_overlapping_patterns_never_refuse_an_upload(self):
        result = plan(
            route("/a/b", body=20 * MB), route("/a/{x}", body=50 * MB), route("/z", body=20 * MB), route("/ws", websocket=True)
        )
        assert [loc.path for loc in result.locations] == ["/a/{x}", "/a/b", "/z", "/ws"]

    def test_the_summary_says_what_was_scoped(self):
        text = plan(route("/up", body=60 * MB), route("/up2", body=20 * MB), route("/ws", websocket=True)).describe()
        assert "10 MB everywhere" in text and "up to 60 MB on 2 upload path(s)" in text and "1 WebSocket path(s)" in text
        assert plan().describe() == "bodies up to 10 MB everywhere"


class TestPaths:
    @pytest.mark.parametrize(
        "path, pattern",
        [
            ("/files/upload", r"^/files/upload/?$"),
            ("/employee/{employee_id}/documents", r"^/employee/[^/]+/documents/?$"),
            ("/api/v1/admin.import", r"^/api/v1/admin\.import/?$"),
            ("/a-b/c_d/e~f", r"^/a-b/c_d/e~f/?$"),
            ("/{x}", r"^/[^/]+/?$"),
            ("/", r"^/?$"),
        ],
    )
    def test_a_path_becomes_one_regular_expression(self, path, pattern):
        (loc,) = plan(route(path, body=20 * MB)).locations
        assert loc.pattern == pattern

    @pytest.mark.parametrize(
        "path",
        ["/a b", "/a;b", "/a{b}", "/a/{b}c", "/a/{}", "/a/{b c}", "/x\n/y", "/a/*", "/a/(b)", "/ünï", "/a/{b;c}", "/a/b$", "/a/{b}}"],
    )
    def test_a_path_nginx_cannot_be_told_about_exactly_is_refused_and_named(self, path):
        with pytest.raises(deploy.DeployError, match="cannot be told about this path exactly") as caught:
            plan(route(path, body=20 * MB))
        assert "POST" in str(caught.value)

    def test_such_a_path_is_only_a_problem_when_it_needs_a_location(self):
        assert plan(route("/a b/c;d")).locations == ()  # no big body, no websocket: the gateway handles it alone

    def test_a_websocket_with_such_a_path_is_refused_too(self):
        with pytest.raises(deploy.DeployError, match="WEBSOCKET /a b"):
            plan(route("/a b", websocket=True))


class TestTheReport:
    @pytest.mark.parametrize(
        "facts",
        [
            None,
            [],
            "text",
            {},
            {"max_body_bytes": 0, "routes": []},
            {"max_body_bytes": -5, "routes": []},
            {"max_body_bytes": "10", "routes": []},
            {"max_body_bytes": True, "routes": []},
            {"max_body_bytes": 1.5, "routes": []},
            {"max_body_bytes": MB},
            {"max_body_bytes": MB, "routes": {}},
            {"max_body_bytes": MB, "routes": ["/x"]},
            {"max_body_bytes": MB, "routes": [{"method": "GET"}]},
            {"max_body_bytes": MB, "routes": [{"path": "/x"}]},
            {"max_body_bytes": MB, "routes": [{"method": "GET", "path": "/x", "max_body_bytes": None}]},
            {"max_body_bytes": MB, "routes": [{"method": "GET", "path": "/x", "max_body_bytes": None, "websocket": "no"}]},
            {"max_body_bytes": MB, "routes": [{"method": "GET", "path": "/x", "max_body_bytes": 0, "websocket": False}]},
            {"max_body_bytes": MB, "routes": [{"method": "GET", "path": "/x", "max_body_bytes": "big", "websocket": False}]},
            {"max_body_bytes": MB, "routes": [{"method": "GET", "path": "/x", "max_body_bytes": True, "websocket": False}]},
            {"max_body_bytes": MB, "routes": [{"method": 1, "path": "/x", "max_body_bytes": None, "websocket": False}]},
            {"max_body_bytes": MB, "routes": [{"method": "GET", "path": 7, "max_body_bytes": None, "websocket": False}]},
        ],
    )
    def test_anything_that_is_not_a_route_report_is_refused(self, facts):
        with pytest.raises(deploy.DeployError):
            deploy.make_route_plan(facts)

    def test_the_last_line_of_the_output_is_the_report(self):
        report = json.dumps({"max_body_bytes": MB, "routes": [route("/up", body=5 * MB)]})
        result = deploy.parse_gateway_routes(f"booting...\nwarning: something\n{report}\n\n")
        assert result.default_body_bytes == MB and result.locations[0].path == "/up"

    @pytest.mark.parametrize("output", ["", "\n\n", "not json", '{"max_body_bytes": 1,', "[1, 2]"])
    def test_output_that_is_not_the_report_is_refused(self, output):
        with pytest.raises(deploy.DeployError):
            deploy.parse_gateway_routes(output)


class TestReadTimeout:
    @pytest.mark.parametrize("text, seconds", [(None, 60), ("", 60), ("  ", 60), ("1", 1), ("300", 300), (" 3600 ", 3600)])
    def test_the_timeout(self, text, seconds):
        assert deploy.parse_read_timeout(text) == seconds

    @pytest.mark.parametrize("bad", ["0", "-1", "3601", "slow", "1.5", "60s", "60; deny all", "1e3"])
    def test_a_bad_timeout_is_refused_and_named(self, bad):
        with pytest.raises(deploy.DeployError, match="nginx_read_timeout"):
            deploy.parse_read_timeout(bad)


class TestTheGeneratedSite:
    def test_without_routes_the_site_has_the_gateway_default_and_an_ordinary_timeout(self):
        text = conf()
        assert "client_max_body_size 10485760;" in text and "100m" not in text
        assert "proxy_read_timeout 60s;" in text and "proxy_send_timeout 60s;" in text
        assert "3600" not in text

    def test_the_shared_ceiling_and_the_timeout_are_whatever_was_configured(self):
        text = conf(routes=plan(default=3 * MB), read_timeout=20)
        assert "client_max_body_size 3145728;" in text
        assert "proxy_read_timeout 20s;" in text and "proxy_send_timeout 20s;" in text

    def test_websocket_headers_are_only_on_websocket_locations(self):
        text = conf(routes=plan(route("/files/upload", body=60 * MB), route("/ws/chat/{room}", websocket=True)))
        assert text.count("proxy_set_header Upgrade $http_upgrade;") == 1
        assert text.count("proxy_read_timeout 3600s;") == 1
        assert "Connection \"upgrade\"" not in text
        ws = location_block(text, "location ~ ^/ws/chat/[^/]+/?$ {")
        assert "proxy_set_header Upgrade $http_upgrade;" in ws and "proxy_read_timeout 3600s;" in ws
        assert "proxy_set_header Connection $arc_connection_shop;" in ws
        rest = text[text.index("    location / {") :]
        assert "Upgrade" not in rest and "3600" not in rest

    def test_the_connection_header_is_only_upgrade_when_the_visitor_asked_for_one(self):
        text = conf(routes=plan(route("/ws", websocket=True)))
        assert 'map $http_upgrade $arc_connection_shop {\n    default upgrade;\n    "" "";\n}' in text

    def test_no_map_when_there_is_no_websocket(self):
        assert "map " not in conf(routes=plan(route("/up", body=20 * MB)))
        assert "map " not in conf()

    def test_two_sites_do_not_share_the_connection_variable(self):
        a = conf(site="arc-shop", routes=plan(route("/ws", websocket=True)))
        b = deploy.generate_nginx_conf(
            project_name="blog", domain="b.example", internal_port=8002, public_port=80, site="arc-blog",
            routes=plan(route("/ws", websocket=True)),
        )
        assert "$arc_connection_arc_shop" in a and "$arc_connection_arc_blog" in b
        assert "arc_blog" not in a and "arc_shop" not in b

    def test_an_upload_location_raises_the_ceiling_for_that_path_alone(self):
        text = conf(routes=plan(route("/files/upload", body=60 * MB)))
        block = location_block(text, "location ~ ^/files/upload/?$ {")
        assert "client_max_body_size 62914560;" in block and "proxy_pass http://127.0.0.1:8001;" in block
        assert "proxy_http_version 1.1;" in block and "proxy_set_header X-Forwarded-Proto $scheme;" in block
        assert "Upgrade" not in block
        assert text.count("client_max_body_size") == 2  # the shared one and this path's, nothing else

    def test_the_route_locations_come_after_metrics_and_login_so_those_keep_priority(self):
        text = conf(routes=plan(route("/{page}", body=20 * MB)))
        assert text.index("location ~ ^/metrics") < text.index("location = /login") < text.index("location ~ ^/[^/]+/?$")

    def test_every_location_is_commented_with_the_path_and_what_it_allows(self):
        text = conf(routes=plan(route("/files/upload", body=60 * MB), route("/ws/chat/{room}", websocket=True)))
        assert "# /files/upload: bodies up to 62914560 bytes (60 MB)" in text
        assert "# /ws/chat/{room}: WebSocket, idle for up to 3600 s" in text

    def test_the_other_locations_keep_the_proxy_headers_and_the_ordinary_timeout(self):
        text = conf(routes=plan(route("/ws", websocket=True)))
        for header in ("location ~ ^/metrics(/|$) {", "location = /login {", "location / {"):
            block = location_block(text, header)
            assert "proxy_set_header X-Real-IP $remote_addr;" in block and "proxy_read_timeout" not in block, header


# --------------------------------------------------------------------------- #
# Reading the gateway's report as the project owner
# --------------------------------------------------------------------------- #
def script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


class TestReadingTheGatewaysRoutes:
    REPORT = json.dumps({"max_body_bytes": 5 * MB, "routes": [route("/up", body=9 * MB)]})

    def test_it_runs_the_gateway_command_in_the_project_and_reads_what_it_prints(self, tmp_path):
        project = tmp_path / "shop"
        project.mkdir()
        fake = script(tmp_path / "arc", f'echo "$PWD $*" >&2\ncat <<\'EOF\'\n{self.REPORT}\nEOF\n')
        result = deploy.read_gateway_routes(project, fake)
        assert result.default_body_bytes == 5 * MB and result.locations[0].body_bytes == 9 * MB

    def test_the_argument_list_is_what_the_gateway_expects(self, tmp_path, monkeypatch):
        seen = {}

        def fake_run(argv, **kw):
            seen.update(argv=argv, **kw)
            return subprocess.CompletedProcess(argv, 0, stdout=self.REPORT + "\n", stderr="")

        monkeypatch.setattr(deploy.subprocess, "run", fake_run)
        deploy.read_gateway_routes(tmp_path, "/venv/bin/arc")
        assert seen["argv"] == ["/venv/bin/arc", "gateway", "routes", "--json"] and seen["cwd"] == tmp_path
        assert seen["timeout"] > 0 and seen["capture_output"] is True

    def test_sudos_variables_are_not_passed_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "someone")
        monkeypatch.setenv("SUDO_COMMAND", "arc deploy prod")
        monkeypatch.setenv("KEEP_ME", "yes")
        seen = {}

        def fake_run(argv, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0, stdout=self.REPORT, stderr="")

        monkeypatch.setattr(deploy.subprocess, "run", fake_run)
        deploy.read_gateway_routes(tmp_path, "arc")
        assert "SUDO_USER" not in seen["env"] and "SUDO_COMMAND" not in seen["env"] and seen["env"]["KEEP_ME"] == "yes"

    def test_as_root_it_runs_as_the_projects_owner_with_their_groups_and_home(self, tmp_path, monkeypatch):
        import pwd

        owner = pwd.getpwuid(tmp_path.stat().st_uid)
        monkeypatch.setattr(deploy.os, "geteuid", lambda: 0)
        # the temp directory belongs to whoever runs the tests; pretend that is not root
        assert tmp_path.stat().st_uid != 0
        seen = {}

        def fake_run(argv, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0, stdout=self.REPORT, stderr="")

        monkeypatch.setattr(deploy.subprocess, "run", fake_run)
        deploy.read_gateway_routes(tmp_path, "arc")
        assert seen["user"] == tmp_path.stat().st_uid and seen["group"] == tmp_path.stat().st_gid
        assert tmp_path.stat().st_gid in seen["extra_groups"]
        assert seen["env"]["HOME"] == owner.pw_dir and seen["env"]["USER"] == owner.pw_name == seen["env"]["LOGNAME"]

    def test_not_as_root_nothing_is_switched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(deploy.os, "geteuid", lambda: 1000)
        seen = {}

        def fake_run(argv, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0, stdout=self.REPORT, stderr="")

        monkeypatch.setattr(deploy.subprocess, "run", fake_run)
        deploy.read_gateway_routes(tmp_path, "arc")
        assert "user" not in seen and "group" not in seen

    def test_a_failing_command_is_an_error_carrying_what_it_said(self, tmp_path):
        fake = script(tmp_path / "arc", 'echo "noise" >&2\necho "Traceback ..." >&2\necho "RuntimeError: database is down" >&2\nexit 3\n')
        with pytest.raises(deploy.DeployError, match=r"exit 3.*database is down"):
            deploy.read_gateway_routes(tmp_path, fake)

    def test_a_command_that_prints_something_else_is_an_error(self, tmp_path):
        with pytest.raises(deploy.DeployError, match="did not print JSON"):
            deploy.read_gateway_routes(tmp_path, script(tmp_path / "arc", "echo hello\n"))

    def test_a_missing_executable_is_an_error_not_a_traceback(self, tmp_path):
        with pytest.raises(deploy.DeployError, match="could not run"):
            deploy.read_gateway_routes(tmp_path, str(tmp_path / "nowhere" / "arc"))

    def test_a_command_that_hangs_is_given_up_on(self, tmp_path):
        fake = script(tmp_path / "arc", "sleep 30\n")
        started = time.monotonic()
        with pytest.raises(deploy.DeployError, match="could not run"):
            deploy.read_gateway_routes(tmp_path, fake, timeout=1)
        assert time.monotonic() - started < 10


# --------------------------------------------------------------------------- #
# `arc deploy prod` uses all of it, before installing anything
# --------------------------------------------------------------------------- #
class TestDeployProdUsesIt:
    @pytest.fixture
    def prod(self, project: Path, monkeypatch):
        """Runs `arc deploy prod` as far as writing the nginx site, which then fails on purpose so
        nothing else (certbot, systemd) is reached. Records what was and was not done."""
        from arc.settings import SettingsManager

        monkeypatch.chdir(project)
        (project / ".arc" / "plugins.lock").write_text('[plugins.gateway]\ncapability = "gateway"\nversion = "0.1.0"\n')
        events: dict = {"installed": [], "nginx_site": None, "read": []}
        report = plan(route("/files/upload", body=60 * MB), route("/ws/chat/{room}", websocket=True))

        def read(root, arc_bin, **kw):
            events["read"].append((root, arc_bin))
            if events.get("read_error"):
                raise deploy.DeployError(events["read_error"])
            return report

        def install_site(*, site, text):
            events["nginx_site"] = text
            raise deploy.DeployError("stop here")

        monkeypatch.setattr(deploy, "require_root", lambda: None)
        monkeypatch.setattr(deploy, "port_blocked_by_other", lambda port: False)
        monkeypatch.setattr(deploy, "resolve_domain", lambda domain: None)
        monkeypatch.setattr(deploy, "domain_already_configured_elsewhere", lambda domain, own_site: None)
        monkeypatch.setattr(deploy, "ensure_nginx_and_certbot_installed", lambda **kw: events["installed"].append(kw))
        monkeypatch.setattr(deploy, "read_gateway_routes", read)
        monkeypatch.setattr(deploy, "install_nginx_site", install_site)
        monkeypatch.setattr(cli, "_find_arc_bin", lambda: "/venv/bin/arc")

        def run(**settings):
            mgr = SettingsManager(project / ".arc")
            for key, value in settings.items():
                mgr.set(key, value)
            return CliRunner().invoke(cli.app, ["deploy", "prod", "--domain", "shop.example", "--no-ssl", "--yes"])

        events["run"] = run
        events["project"] = project
        return events

    def test_the_site_is_written_with_the_limits_the_gateway_reported(self, prod):
        result = prod["run"]()
        assert prod["read"] == [(prod["project"], "/venv/bin/arc")]
        text = prod["nginx_site"]
        assert "location ~ ^/files/upload/?$ {" in text and "client_max_body_size 62914560;" in text
        assert "location ~ ^/ws/chat/[^/]+/?$ {" in text and "proxy_read_timeout 60s;" in text
        assert result.exit_code == 1  # the stand-in installer refused, which ends the run here

    def test_the_timeout_setting_reaches_the_site(self, prod):
        prod["run"](nginx_read_timeout="25")
        assert "proxy_read_timeout 25s;" in prod["nginx_site"]

    def test_a_bad_timeout_stops_the_command_before_anything_is_installed(self, prod):
        result = prod["run"](nginx_read_timeout="forever")
        assert result.exit_code == 1 and "nginx_read_timeout" in result.output
        assert prod["installed"] == [] and prod["nginx_site"] is None and prod["read"] == []

    def test_without_an_answer_from_the_gateway_nothing_is_installed(self, prod):
        prod["read_error"] = "`arc gateway routes --json` failed (exit 1): database is down"
        result = prod["run"]()
        assert result.exit_code == 1
        assert "Could not read the gateway's routes" in result.output and "database is down" in result.output
        assert prod["installed"] == [] and prod["nginx_site"] is None

    def test_a_project_without_the_gateway_is_not_asked(self, prod):
        (prod["project"] / ".arc" / "plugins.lock").write_text('[plugins.pgdb]\ncapability = "pgdb"\nversion = "0.1.0"\n')
        prod["run"]()
        assert prod["read"] == []
        assert "location ~ ^/files" not in prod["nginx_site"] and "client_max_body_size 10485760;" in prod["nginx_site"]

    def test_a_disabled_gateway_is_not_asked_either(self, prod):
        (prod["project"] / ".arc" / "plugins.lock").write_text('[plugins.gateway]\ncapability = "gateway"\nenabled = false\n')
        prod["run"]()
        assert prod["read"] == []

    def test_the_operator_is_told_what_was_scoped(self, prod):
        said = " ".join(prod["run"]().output.split())  # the console wraps long lines
        assert "nginx limits: bodies up to 10 MB everywhere, up to 60 MB on 1 upload path(s); 1 WebSocket path(s)" in said
        assert "ordinary requests time out after 60 s" in said


# --------------------------------------------------------------------------- #
# A real nginx
# --------------------------------------------------------------------------- #
class Backend(BaseHTTPRequestHandler):
    """Answers with what it saw: the path, how many body bytes arrived, and the upgrade headers."""

    protocol_version = "HTTP/1.1"

    def handle_any(self):
        length = int(self.headers.get("Content-Length") or 0)
        received = len(self.rfile.read(length)) if length else 0
        path, _, query = self.path.partition("?")
        if query.startswith("sleep="):
            time.sleep(float(query[6:]))
        body = json.dumps(
            {
                "path": path,
                "received": received,
                "upgrade": self.headers.get("Upgrade"),
                "connection": self.headers.get("Connection"),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = handle_any  # noqa: N815

    def log_message(self, *args):
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(shutil.which("nginx") is None, reason="nginx is not installed")
class TestAgainstARealNginx:
    DEFAULT = 64 * 1024
    UPLOAD = 256 * 1024

    @pytest.fixture
    def running(self, tmp_path):
        """start(routes, read_timeout) -> request(method, path, body_size) -> (status, answer) against a private nginx."""
        backend_port = free_port()
        backend = ThreadingHTTPServer(("127.0.0.1", backend_port), Backend)
        backend.daemon_threads = True
        threading.Thread(target=backend.serve_forever, daemon=True).start()
        procs: list[subprocess.Popen] = []

        def launch(site_kw) -> tuple[subprocess.Popen, int]:
            nginx_port = free_port()
            site = deploy.generate_nginx_conf(
                project_name="shop", domain="shop.example", internal_port=backend_port, public_port=nginx_port, **site_kw
            )
            (tmp_path / "site.conf").write_text(site)
            (tmp_path / "nginx.conf").write_text(
                f"worker_processes 1;\npid {tmp_path}/nginx.pid;\nerror_log {tmp_path}/error.log;\n"
                f"daemon off;\nevents {{}}\nhttp {{\n  access_log off;\n"
                f"  client_body_temp_path {tmp_path}/b; proxy_temp_path {tmp_path}/p;\n"
                f"  fastcgi_temp_path {tmp_path}/f; uwsgi_temp_path {tmp_path}/u; scgi_temp_path {tmp_path}/s;\n"
                f"  include {tmp_path}/site.conf;\n}}\n"
            )
            proc = subprocess.Popen(
                ["nginx", "-p", str(tmp_path), "-c", str(tmp_path / "nginx.conf")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            procs.append(proc)
            for _ in range(50):
                if proc.poll() is not None:
                    break
                try:
                    socket.create_connection(("127.0.0.1", nginx_port), timeout=0.2).close()
                    return proc, nginx_port
                except OSError:
                    time.sleep(0.1)
            raise OSError(f"nginx did not come up on {nginx_port} (exit {proc.poll()})")

        def start(routes, **site_kw):
            last_error = None
            for _ in range(5):
                try:
                    _, nginx_port = launch({"routes": routes, **site_kw})
                    break
                except OSError as exc:
                    last_error = exc
                    (tmp_path / "error.log").unlink(missing_ok=True)
            else:
                log = (tmp_path / "error.log").read_text() if (tmp_path / "error.log").exists() else "(no error log)"
                pytest.fail(f"the private nginx would not start: {last_error}\n{log}")

            def request(method: str, path: str, size: int = 0, headers: dict | None = None, timeout: float = 15):
                conn = http.client.HTTPConnection("127.0.0.1", nginx_port, timeout=timeout)
                try:
                    conn.putrequest(method, path, skip_host=True)  # the path exactly as given
                    conn.putheader("Host", "shop.example")
                    conn.putheader("Content-Length", str(size))
                    for name, value in (headers or {}).items():
                        conn.putheader(name, value)
                    conn.endheaders()
                    try:
                        if size:
                            conn.send(b"x" * size)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # nginx answered 413 without waiting for the rest; its answer is still readable
                    resp = conn.getresponse()
                    raw = resp.read().decode()
                    try:
                        return resp.status, json.loads(raw)
                    except ValueError:
                        return resp.status, raw
                finally:
                    conn.close()

            request.port = nginx_port
            return request

        yield start
        for proc in procs:
            proc.terminate()
            proc.wait(timeout=5)
        backend.shutdown()

    def routes(self) -> deploy.RoutePlan:
        return plan(
            route("/files/upload", body=self.UPLOAD),
            route("/employee/{employee_id}/documents", body=self.UPLOAD),
            route("/ws/chat/{room}", websocket=True),
            default=self.DEFAULT,
        )

    # -- body sizes -------------------------------------------------------- #
    def test_an_ordinary_path_takes_a_body_up_to_the_shared_ceiling_and_no_more(self, running):
        request = running(self.routes())
        assert request("POST", "/api/things", self.DEFAULT)[0] == 200
        assert request("POST", "/api/things", self.DEFAULT + 1)[0] == 413

    def test_the_root_and_login_and_metrics_keep_the_shared_ceiling_too(self, running):
        request = running(self.routes())
        for path in ("/", "/login", "/metrics"):
            assert request("POST", path, self.DEFAULT + 1)[0] == 413, path

    def test_an_upload_path_takes_up_to_its_own_ceiling(self, running):
        request = running(self.routes())
        status, answer = request("POST", "/files/upload", self.UPLOAD)
        assert (status, answer["received"]) == (200, self.UPLOAD)
        assert request("POST", "/files/upload", self.UPLOAD + 1)[0] == 413

    def test_the_ceiling_of_an_upload_path_does_not_spread_to_its_neighbours(self, running):
        request = running(self.routes())
        for path in ("/files", "/files/other", "/files/upload/more", "/files/uploads", "/x/files/upload", "/files/upload.php"):
            assert request("POST", path, self.DEFAULT + 1)[0] == 413, path

    def test_a_path_with_a_parameter_gets_its_ceiling_for_any_value(self, running):
        request = running(self.routes())
        for path in ("/employee/7/documents", "/employee/abc-123/documents", "/employee/a%20b/documents"):
            assert request("POST", path, self.UPLOAD)[0] == 200, path
        assert request("POST", "/employee/7/documents", self.UPLOAD + 1)[0] == 413
        # the parameter is exactly one segment
        assert request("POST", "/employee/7/8/documents", self.DEFAULT + 1)[0] == 413

    @pytest.mark.parametrize(
        "spelling",
        ["/files/upload/", "/%66iles/upload", "//files/upload", "/files//upload", "/./files/upload", "/a/../files/upload", "/files/upload?x=1"],
    )
    def test_every_spelling_of_an_upload_path_the_gateway_would_accept_gets_the_upload_ceiling(self, running, spelling):
        request = running(self.routes())
        assert request("POST", spelling, self.UPLOAD)[0] == 200, spelling

    def test_methods_do_not_matter_to_the_ceiling(self, running):
        request = running(self.routes())
        assert request("PUT", "/files/upload", self.UPLOAD)[0] == 200
        assert request("PUT", "/api/things", self.DEFAULT + 1)[0] == 413

    def test_a_chunked_upload_is_held_to_the_ceiling_too(self, running):
        request = running(self.routes())
        for path, expected in (("/api/things", 413), ("/files/upload", 200)):
            conn = http.client.HTTPConnection("127.0.0.1", request.port, timeout=15)
            conn.putrequest("POST", path, skip_host=True)
            conn.putheader("Host", "shop.example")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            chunk = b"x" * 16384
            try:
                for _ in range((self.DEFAULT * 2) // len(chunk)):
                    conn.send(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                conn.send(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            assert conn.getresponse().status == expected, path
            conn.close()

    # -- WebSockets -------------------------------------------------------- #
    def test_the_backend_sees_the_upgrade_only_on_the_websocket_path(self, running):
        request = running(self.routes())
        upgrade = {"Upgrade": "websocket", "Connection": "Upgrade"}
        _, ws = request("GET", "/ws/chat/lobby", headers=upgrade)
        assert ws["upgrade"] == "websocket" and ws["connection"].lower() == "upgrade"
        _, other = request("GET", "/api/things", headers=upgrade)
        assert other["upgrade"] is None and (other["connection"] or "").lower() != "upgrade"

    def test_a_plain_request_to_a_websocket_path_is_not_told_to_upgrade(self, running):
        request = running(self.routes())
        _, answer = request("GET", "/ws/chat/lobby")
        assert answer["upgrade"] is None and (answer["connection"] or "").lower() != "upgrade"

    def test_ordinary_requests_no_longer_carry_a_forced_upgrade(self, running):
        request = running(self.routes())
        for path in ("/", "/api/things", "/files/upload"):
            _, answer = request("GET", path)
            assert (answer["connection"] or "").lower() != "upgrade", path

    # -- timeouts ---------------------------------------------------------- #
    def test_an_ordinary_request_the_app_does_not_answer_in_time_is_given_up_on(self, running):
        request = running(self.routes(), read_timeout=1)
        started = time.monotonic()
        assert request("GET", "/api/slow?sleep=3")[0] == 504
        assert time.monotonic() - started < 2.5  # nginx did not wait for the app
        assert request("GET", "/api/fast?sleep=0")[0] == 200

    def test_an_upload_path_has_the_ordinary_timeout_too(self, running):
        request = running(self.routes(), read_timeout=1)
        assert request("POST", "/files/upload?sleep=3", 10)[0] == 504

    def test_a_websocket_path_keeps_waiting_far_longer(self, running):
        request = running(self.routes(), read_timeout=1)
        status, answer = request("GET", "/ws/chat/lobby?sleep=2.5")
        assert (status, answer["path"]) == (200, "/ws/chat/lobby")

    def test_the_login_and_metrics_locations_have_the_ordinary_timeout(self, running):
        request = running(self.routes(), read_timeout=1)
        assert request("GET", "/metrics?sleep=3")[0] == 504
        assert request("POST", "/login?sleep=3")[0] == 504
