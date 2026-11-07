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


class TestTheLoginLimit:
    def test_the_site_declares_a_zone_and_applies_it_to_login_only(self):
        text = conf()
        assert "limit_req_zone $binary_remote_addr zone=arc_login_shop:10m rate=10r/s;" in text
        start = text.index("location = /login {")
        block = text[start : text.index("    }\n", start)]
        assert "limit_req zone=arc_login_shop burst=50 nodelay;" in block and "limit_req_status 429;" in block
        assert "proxy_pass http://127.0.0.1:8001;" in block
        assert text.count("limit_req zone=") == 1  # nothing else is limited

    def test_the_zone_is_named_after_the_site_so_two_sites_never_collide(self):
        a = conf(site="arc-shop")
        b = deploy.generate_nginx_conf(project_name="blog", domain="blog.example", internal_port=8002, public_port=80, site="arc-blog")
        assert "zone=arc_login_arc_shop:" in a and "zone=arc_login_arc_blog:" in b

    def test_a_site_name_cannot_inject_nginx_syntax_through_the_zone_name(self):
        text = conf(site="evil; deny all; x=")
        assert "zone=arc_login_evil__deny_all__x_:" in text and "deny all; x" not in text.split("limit_req_zone")[1].split("\n")[0]


class TestLoginSettings:
    @pytest.mark.parametrize(
        "text, nginx_rate",
        [(None, "10r/s"), ("", "10r/s"), ("  ", "10r/s"), ("20", "20r/s"), ("20r/s", "20r/s"), ("300r/m", "300r/m"), (" 5 ", "5r/s")],
    )
    def test_the_rate(self, text, nginx_rate):
        assert deploy.parse_login_rate(text) == nginx_rate

    @pytest.mark.parametrize("bad", ["0", "-5", "fast", "10r/h", "10 r/s", "10r/s; deny all", "1.5", "100000"])
    def test_a_bad_rate_is_refused_and_named(self, bad):
        with pytest.raises(deploy.DeployError, match="nginx_login_rate"):
            deploy.parse_login_rate(bad)

    @pytest.mark.parametrize("text, burst", [(None, 50), ("", 50), ("0", 0), ("1000", 1000), (" 80 ", 80)])
    def test_the_burst(self, text, burst):
        assert deploy.parse_login_burst(text) == burst

    @pytest.mark.parametrize("bad", ["-1", "1001", "many", "5.5", "10; deny all"])
    def test_a_bad_burst_is_refused_and_named(self, bad):
        with pytest.raises(deploy.DeployError, match="nginx_login_burst"):
            deploy.parse_login_burst(bad)

    def test_the_exempt_list_names_its_own_setting_in_the_error(self):
        assert deploy.parse_ip_list("nginx_login_allow", "203.0.113.0/24, 10.1.2.3") == ["203.0.113.0/24", "10.1.2.3/32"]
        with pytest.raises(deploy.DeployError, match="nginx_login_allow: 'office'"):
            deploy.parse_ip_list("nginx_login_allow", "203.0.113.0/24, office")

    def test_the_values_reach_the_site(self):
        text = conf(login_rate="20r/s", login_burst=80)
        assert "rate=20r/s;" in text and "burst=80 nodelay;" in text

    def test_without_an_exempt_list_the_zone_is_keyed_by_address_directly(self):
        text = conf()
        assert "geo " not in text and "map " not in text

    def test_with_one_the_zone_is_keyed_through_geo_and_map_so_those_addresses_are_not_counted(self):
        text = conf(login_allow=["203.0.113.0/24", "10.1.2.3/32"], site="arc-shop")
        assert "geo $remote_addr $arc_login_arc_shop_trusted {" in text
        assert "203.0.113.0/24 1;" in text and "10.1.2.3/32 1;" in text and "default 0;" in text
        assert 'map $arc_login_arc_shop_trusted $arc_login_arc_shop_key {' in text and '1 "";' in text
        assert "limit_req_zone $arc_login_arc_shop_key zone=arc_login_arc_shop:10m rate=10r/s;" in text
        assert "$binary_remote_addr zone" not in text

    def test_two_sites_with_exempt_lists_do_not_share_nginx_variables(self):
        a = conf(login_allow=["10.0.0.0/8"], site="arc-shop")
        b = deploy.generate_nginx_conf(project_name="blog", domain="b.example", internal_port=8002, public_port=80, site="arc-blog", login_allow=["10.0.0.0/8"])
        assert "$arc_login_arc_shop_trusted" in a and "$arc_login_arc_blog_trusted" in b
        assert "arc_login_arc_blog" not in a and "arc_login_arc_shop" not in b


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

    def test_everything_else_is_still_proxied(self):
        # WebSocket headers and the hour-long timeout used to be here, for every path; they now
        # belong to the WebSocket locations alone (tests/test_nginx_routes.py)
        text = conf()
        rest = text[text.index("location / {"):]
        assert "proxy_pass http://127.0.0.1:8001;" in rest and "Upgrade" not in rest
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

    do_POST = do_GET  # noqa: N815 - same answer, so a login attempt gets through to be counted

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
        backend_port = free_port()
        backend = HTTPServer(("127.0.0.1", backend_port), StubBackend)
        threading.Thread(target=backend.serve_forever, daemon=True).start()
        procs: list[subprocess.Popen] = []

        def launch(metrics_allow, conf) -> tuple[subprocess.Popen, int]:
            # nginx listens on the wildcard address, and a port that was free on 127.0.0.1 can still
            # be taken by some other local socket (a client connection from an earlier test): when
            # nginx cannot bind, try another port rather than fail the test.
            nginx_port = free_port()
            site = deploy.generate_nginx_conf(
                project_name="shop", domain="shop.example", internal_port=backend_port,
                public_port=nginx_port, metrics_allow=list(metrics_allow), **conf,
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

        def start(metrics_allow=(), **conf):
            last_error = None
            for _ in range(5):
                try:
                    proc, nginx_port = launch(metrics_allow, conf)
                    break
                except OSError as exc:
                    last_error = exc
                    (tmp_path / "error.log").unlink(missing_ok=True)
            else:
                log = (tmp_path / "error.log").read_text() if (tmp_path / "error.log").exists() else "(no error log)"
                pytest.fail(f"the private nginx would not start: {last_error}\n{log}")

            def get(source_ip: str, path: str, method: str = "GET") -> tuple[int, str]:
                conn = http.client.HTTPConnection("127.0.0.1", nginx_port, source_address=(source_ip, 0), timeout=5)
                try:
                    conn.putrequest(method, path, skip_host=True)  # the path exactly as given
                    conn.putheader("Host", "shop.example")
                    conn.putheader("Content-Length", "0")
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

    def test_one_address_hammering_login_is_cut_off_with_429_the_rest_still_work(self, running):
        get = running(login_rate="5r/s", login_burst=10)
        statuses = [get("127.0.0.2", "/login", "POST")[0] for _ in range(40)]
        let_through = statuses.count(200)
        assert 8 <= let_through <= 16, statuses  # 1 + a burst of 10, give or take the clock
        assert set(statuses) == {200, 429} and statuses.count(429) == 40 - let_through
        assert get("127.0.0.3", "/login", "POST")[0] == 200  # another visitor has their own allowance

    def test_the_default_lets_a_big_office_behind_one_address_log_in(self, running):
        """300 people arriving within 25 s: a burst of 50 plus 10 a second is enough, where the first
        version (5 a second, burst 10) would have turned most of them away."""
        get = running()  # the defaults
        statuses = [get("127.0.0.2", "/login", "POST")[0] for _ in range(45)]
        assert set(statuses) == {200}, statuses  # all of a first wave of 45 pass at once
        more = [get("127.0.0.2", "/login", "POST")[0] for _ in range(60)]
        assert 429 in more and more.count(200) <= 20  # but one address cannot go on forever

    def test_the_rate_and_burst_are_what_was_configured(self, running):
        get = running(login_rate="2r/s", login_burst=3)
        statuses = [get("127.0.0.2", "/login", "POST")[0] for _ in range(20)]
        assert 3 <= statuses.count(200) <= 6, statuses  # 1 + burst 3, plus a little for elapsed time
        get = running(login_rate="1000r/s", login_burst=500)
        assert {get("127.0.0.2", "/login", "POST")[0] for _ in range(60)} == {200}

    def test_an_exempt_network_is_not_limited_at_all_and_others_still_are(self, running):
        get = running(login_rate="2r/s", login_burst=3, login_allow=["127.0.0.2"])
        assert {get("127.0.0.2", "/login", "POST")[0] for _ in range(60)} == {200}  # the office
        assert 429 in {get("127.0.0.3", "/login", "POST")[0] for _ in range(20)}  # everyone else

    def test_an_exempt_range_covers_the_addresses_in_it(self, running):
        get = running(login_rate="2r/s", login_burst=3, login_allow=["127.0.0.0/30"])  # .0 to .3
        assert {get("127.0.0.3", "/login", "POST")[0] for _ in range(40)} == {200}
        assert 429 in {get("127.0.0.4", "/login", "POST")[0] for _ in range(20)}

    def test_nothing_else_is_limited(self, running):
        get = running()
        assert {get("127.0.0.2", "/", "GET")[0] for _ in range(40)} == {200}
        assert {get("127.0.0.2", "/loginx", "GET")[0] for _ in range(40)} == {200}

    def test_a_configured_address_may_read_metrics_and_others_still_may_not(self, running):
        get = running(metrics_allow=["127.0.0.2"])
        assert get("127.0.0.2", "/metrics")[0] == 200
        assert get("127.0.0.1", "/metrics")[0] == 200  # this machine is always allowed
        assert get("127.0.0.3", "/metrics")[0] == 403  # an address that is neither
