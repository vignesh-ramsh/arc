"""`arc deploy prod` keeps /metrics off the internet (arc/deploy.py `generate_nginx_conf`,
`parse_metrics_allow`).

gateway_metrics_token is empty by default and the generated nginx site proxies every path, so
/metrics used to answer anyone who could reach the domain. The site now has a location for it that
only this machine and the addresses in the `deploy_metrics_allow` setting get through.

The last class runs a real nginx (a private instance in a temp directory, never the system one)
in front of a stub backend, because what matters is what nginx does with the request, URL
encodings and dot segments included, not what the text of the config looks like.
"""

from __future__ import annotations

import http.client
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from arc import deploy


def conf(**kw) -> str:
    return deploy.generate_nginx_conf(project_name="shop", domain="shop.example", internal_port=8001, public_port=80, **kw)


class TestParseMetricsAllow:
    def test_nothing_set_means_this_machine_only(self):
        assert deploy.parse_metrics_allow(None) == [] and deploy.parse_metrics_allow("") == []
        assert deploy.parse_metrics_allow(" , ,") == []

    def test_addresses_and_ranges_are_normalised(self):
        assert deploy.parse_metrics_allow("203.0.113.7, 10.0.0.0/24 ,2001:db8::/32") == [
            "203.0.113.7/32", "10.0.0.0/24", "2001:db8::/32",
        ]

    @pytest.mark.parametrize(
        "bad",
        [
            "prometheus.example.com",  # a name is not an address (nginx would resolve it at reload)
            "1.2.3.4; allow all",  # would add a directive if pasted in as text
            "1.2.3.4\n    allow all",
            "all",
            "10.0.0.0/33",
            "1.2.3",
        ],
    )
    def test_anything_else_is_refused_and_named(self, bad):
        with pytest.raises(deploy.DeployError) as caught:
            deploy.parse_metrics_allow(f"10.0.0.1, {bad}")
        assert "deploy_metrics_allow" in str(caught.value)


class TestTheGeneratedSite:
    def block(self, text: str) -> str:
        start = text.index("location ~ ^/metrics")
        return text[start : text.index("    }\n", start)]

    def test_metrics_is_allowed_to_this_machine_and_nobody_else_by_default(self):
        block = self.block(conf())
        assert "allow 127.0.0.1;" in block and "allow ::1;" in block
        assert block.count("allow ") == 2
        assert block.index("allow ::1;") < block.index("deny all;")  # deny comes after every allow

    def test_the_configured_addresses_are_added_before_the_deny(self):
        block = self.block(conf(metrics_allow=["203.0.113.7/32", "2001:db8::/32"]))
        assert "allow 203.0.113.7/32;" in block and "allow 2001:db8::/32;" in block
        assert block.index("allow 2001:db8::/32;") < block.index("deny all;")

    def test_it_still_proxies_to_the_app(self):
        assert "proxy_pass http://127.0.0.1:8001;" in self.block(conf())

    def test_everything_else_is_still_proxied_with_websocket_support(self):
        text = conf()
        rest = text[text.index("location / {"):]
        assert "proxy_pass http://127.0.0.1:8001;" in rest and 'proxy_set_header Upgrade $http_upgrade;' in rest
        assert "deny all" not in rest

    def test_the_locked_down_path_is_metrics_and_what_is_under_it(self):
        assert "location ~ ^/metrics(/|$) {" in conf()


class StubBackend(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = f"backend saw {self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def can_use_second_loopback_address() -> bool:
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.2", 0))
        return True
    except OSError:
        return False


@pytest.mark.skipif(shutil.which("nginx") is None, reason="nginx is not installed")
@pytest.mark.skipif(not can_use_second_loopback_address(), reason="needs 127.0.0.2 as a second client address")
class TestAgainstARealNginx:
    @pytest.fixture
    def running(self, tmp_path):
        """start(metrics_allow) -> function (source_ip, path) -> (status, body) against a private nginx."""
        backend_port, nginx_port = free_port(), free_port()
        backend = HTTPServer(("127.0.0.1", backend_port), StubBackend)
        threading.Thread(target=backend.serve_forever, daemon=True).start()
        procs: list[subprocess.Popen] = []

        def start(metrics_allow=()):
            site = deploy.generate_nginx_conf(
                project_name="shop", domain="shop.example", internal_port=backend_port,
                public_port=nginx_port, metrics_allow=list(metrics_allow),
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
                try:
                    socket.create_connection(("127.0.0.1", nginx_port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                pytest.fail("the private nginx did not come up")

            def get(source_ip: str, path: str) -> tuple[int, str]:
                conn = http.client.HTTPConnection("127.0.0.1", nginx_port, source_address=(source_ip, 0), timeout=5)
                try:
                    conn.putrequest("GET", path, skip_host=True)  # the path exactly as given
                    conn.putheader("Host", "shop.example")
                    conn.endheaders()
                    resp = conn.getresponse()
                    return resp.status, resp.read().decode()
                finally:
                    conn.close()

            return get

        yield start
        for proc in procs:
            proc.terminate()
            proc.wait(timeout=5)
        backend.shutdown()

    def test_this_machine_may_read_metrics_and_other_addresses_may_not(self, running):
        get = running()
        assert get("127.0.0.1", "/metrics") == (200, "backend saw /metrics")
        assert get("127.0.0.2", "/metrics")[0] == 403

    @pytest.mark.parametrize(
        "path",
        ["/metrics?x=1", "/metrics/", "/metrics/anything", "/%6detrics", "//metrics", "/./metrics", "/a/../metrics", "/%2e/metrics"],
    )
    def test_no_spelling_of_the_path_gets_an_outside_caller_through(self, running, path):
        get = running()
        assert get("127.0.0.2", path)[0] == 403, path

    def test_everything_else_is_open_to_everyone(self, running):
        get = running()
        for path in ("/", "/healthz", "/readyz", "/api/v1/whatever"):
            assert get("127.0.0.2", path) == (200, f"backend saw {path}")

    def test_a_configured_address_may_read_metrics_and_others_still_may_not(self, running):
        get = running(metrics_allow=["127.0.0.2"])
        assert get("127.0.0.2", "/metrics")[0] == 200
        assert get("127.0.0.1", "/metrics")[0] == 200  # this machine is always allowed
        assert get("127.0.0.3", "/metrics")[0] == 403  # an address that is neither
