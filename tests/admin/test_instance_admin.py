from __future__ import annotations

import asyncio
import errno
import html
import re
import secrets
import socket
import stat
import tempfile
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import yaml

from netizen.admin.auth import AdminAuth
from netizen.admin.port_config import (
    AdminPortConfigurationError,
    ConfigFileSnapshot,
    persist_admin_port,
    set_admin_port,
)
from netizen.admin.transport import AdminHttpState, AdminHttpTransport, Request
from netizen.admin.web import AdminWebApplication, AdminWebRunner, accepted_authorities, admin_access_urls
from netizen.instance import instance_digest
from tests.admin.test_web import FakeManagement


class PortConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.yaml"
        self.content = b"instance:\n  dataDir: /test/state\n  projectRoot: /project\nprojects: {}\n"
        self.path.write_bytes(self.content)

    def test_adds_only_port_atomically_with_private_mode(self) -> None:
        snapshot = ConfigFileSnapshot.read(self.path)
        original_inode = self.path.stat().st_ino
        persist_admin_port(snapshot, 8788)
        loaded = yaml.safe_load(self.path.read_bytes())
        self.assertEqual(loaded.pop("adminWeb"), {"port": 8788})
        self.assertEqual(loaded, yaml.safe_load(self.content))
        self.assertNotEqual(self.path.stat().st_ino, original_inode)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.glob(".admin-port-*")), [])

    def test_existing_admin_values_and_other_yaml_values_are_preserved(self) -> None:
        self.path.write_bytes(self.content + b"adminWeb:\n  host: 127.0.0.1\n  enabled: true\n  accessHost: admin.test\n")
        baseline = yaml.safe_load(self.path.read_bytes())
        persist_admin_port(ConfigFileSnapshot.read(self.path), 8800)
        expected = {**baseline, "adminWeb": {**baseline["adminWeb"], "port": 8800}}
        self.assertEqual(yaml.safe_load(self.path.read_bytes()), expected)

    def test_yaml_aliases_do_not_add_port_to_unrelated_fields(self) -> None:
        self.path.write_text("adminWeb: &shared\n  host: 127.0.0.1\nother: *shared\n")
        persist_admin_port(ConfigFileSnapshot.read(self.path), 8788)
        self.assertEqual(yaml.safe_load(self.path.read_bytes()), {
            "adminWeb": {"host": "127.0.0.1", "port": 8788},
            "other": {"host": "127.0.0.1"},
        })

    def test_manual_edit_or_equal_content_replacement_is_not_overwritten(self) -> None:
        for equal_content in (False, True):
            with self.subTest(equal_content=equal_content):
                self.path.write_bytes(self.content)
                snapshot = ConfigFileSnapshot.read(self.path)
                changed = self.content if equal_content else self.content + b"# human edit\n"
                replacement = self.path.parent / "replacement.yaml"
                replacement.write_bytes(changed)
                replacement.replace(self.path)
                with self.assertRaisesRegex(AdminPortConfigurationError, "changed since startup"):
                    persist_admin_port(snapshot, 8788)
                self.assertEqual(self.path.read_bytes(), changed)

    def test_symlink_or_missing_target_is_not_recreated(self) -> None:
        snapshot = ConfigFileSnapshot.read(self.path)
        target = self.path.parent / "elsewhere.yaml"
        target.write_bytes(b"unchanged")
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            persist_admin_port(snapshot, 8788)
        self.assertFalse(self.path.exists())
        self.path.symlink_to(target)
        with self.assertRaisesRegex(AdminPortConfigurationError, "regular file"):
            persist_admin_port(snapshot, 8788)
        self.assertEqual(target.read_bytes(), b"unchanged")

    def test_failed_replace_retains_config_and_cleans_temporary(self) -> None:
        with patch("netizen.admin.port_config.os.replace", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                persist_admin_port(ConfigFileSnapshot.read(self.path), 8788)
        self.assertEqual(self.path.read_bytes(), self.content)
        self.assertEqual(list(self.path.parent.glob(".admin-port-*")), [])

    def test_disabled_or_explicit_port_cannot_be_allocated(self) -> None:
        for values in ({"enabled": False}, {"port": 8787}, {"port": None}):
            with self.subTest(values=values):
                self.path.write_text(yaml.safe_dump({"adminWeb": values}))
                with self.assertRaisesRegex(AdminPortConfigurationError, "absent port"):
                    persist_admin_port(ConfigFileSnapshot.read(self.path), 8788)

    def test_explicit_installer_port_replaces_existing_port_even_when_disabled(self) -> None:
        self.path.write_text("adminWeb:\n  enabled: false\n  port: 8787\n  accessHost: admin.test\nprojects: {}\n")
        set_admin_port(self.path, 8890)
        self.assertEqual(yaml.safe_load(self.path.read_bytes()), {
            "adminWeb": {"enabled": False, "port": 8890, "accessHost": "admin.test"},
            "projects": {},
        })

    def test_explicit_invalid_installer_port_never_changes_configuration(self) -> None:
        for invalid in (0, -1, 65536, True, None, "8787"):
            with self.subTest(invalid=invalid), self.assertRaises(AdminPortConfigurationError):
                set_admin_port(self.path, invalid)
        self.assertEqual(self.path.read_bytes(), self.content)


class AdminPortAllocationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runners: list[AdminWebRunner] = []
        self.addCleanup(self.temp.cleanup)
        self.authorities = patch(
            "netizen.admin.web.accepted_authorities",
            side_effect=lambda _host, _port, addresses: tuple(
                f"127.0.0.1:{address[1]}" for address in addresses
            ),
        )
        self.authorities.start()
        self.addCleanup(self.authorities.stop)

    async def asyncTearDown(self) -> None:
        for runner in self.runners:
            if runner.transport.state is not AdminHttpState.NEW:
                await runner.drain(asyncio.get_running_loop().time() + 1)
            runner.close_auth()

    def runner(self, name: str, *, port: int | None = None, access_host: str | None = None):
        root = self.root / name
        root.mkdir()
        credential = root / "credential"
        credential.write_text(secrets.token_urlsafe(32))
        credential.chmod(0o600)
        config = root / "config.yaml"
        values = {"host": "127.0.0.1"}
        if port is not None:
            values["port"] = port
        config.write_text(yaml.safe_dump({"adminWeb": values, "projects": {}}))
        runner = AdminWebRunner(
            host="127.0.0.1", port=port, credential_path=credential,
            instance_root=root, config_path=config, config_snapshot=ConfigFileSnapshot.read(config),
            access_host=access_host,
        )
        self.runners.append(runner)
        return runner, config

    def occupied_socket(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        return listener, listener.getsockname()[1]

    async def test_first_start_skips_occupied_port_and_keeps_successful_listener(self) -> None:
        _listener, port = self.occupied_socket()
        runner, config = self.runner("automatic")
        self.assertEqual(runner.urls, ())
        with patch("netizen.admin.web._AUTO_PORTS", range(port, min(port + 100, 65536))):
            await runner.bind()
        selected = runner.addresses[0][1]
        self.assertGreater(selected, port)
        self.assertEqual(yaml.safe_load(config.read_bytes())["adminWeb"]["port"], selected)
        self.assertEqual(runner.urls, (f"http://127.0.0.1:{selected}/",))
        self.assertTrue(runner.loopback_only)
        self.assertFalse(runner.transport.admission_open)
        with socket.socket() as probe, self.assertRaises(OSError) as error:
            probe.bind(("127.0.0.1", selected))
        self.assertEqual(error.exception.errno, errno.EADDRINUSE)

    async def test_concurrent_roots_select_distinct_live_ports(self) -> None:
        listener, port = self.occupied_socket()
        listener.close()
        one, one_config = self.runner("one")
        two, two_config = self.runner("two")
        with patch("netizen.admin.web._AUTO_PORTS", range(port, min(port + 100, 65536))):
            await asyncio.gather(one.bind(), two.bind())
        selected = {yaml.safe_load(path.read_bytes())["adminWeb"]["port"] for path in (one_config, two_config)}
        self.assertEqual(len(selected), 2)
        self.assertNotEqual(one.urls, two.urls)

    async def test_explicit_collision_does_not_retry_or_change_file(self) -> None:
        _listener, port = self.occupied_socket()
        runner, config = self.runner("fixed", port=port)
        original = config.read_bytes()
        with self.assertRaisesRegex(OSError, str(config)):
            await runner.bind()
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(runner.urls, ())

    async def test_non_address_in_use_error_is_not_retried(self) -> None:
        runner, config = self.runner("denied")
        with patch.object(AdminHttpTransport, "bind", new=AsyncMock(side_effect=OSError(errno.EACCES, "denied"))) as bind:
            with self.assertRaises(OSError) as error:
                await runner.bind()
        self.assertEqual(error.exception.errno, errno.EACCES)
        self.assertEqual(bind.await_count, 1)
        self.assertNotIn("port", yaml.safe_load(config.read_bytes())["adminWeb"])

    async def test_all_100_occupied_ports_fail_without_persistence(self) -> None:
        runner, config = self.runner("exhausted")
        with patch.object(AdminHttpTransport, "bind", new=AsyncMock(side_effect=OSError(errno.EADDRINUSE, "occupied"))) as bind:
            with self.assertRaisesRegex(OSError, "8787–8886"):
                await runner.bind()
        self.assertEqual(bind.await_count, 100)
        self.assertNotIn("port", yaml.safe_load(config.read_bytes())["adminWeb"])

    async def test_changed_config_after_bind_closes_listener_without_publishing_url(self) -> None:
        listener, port = self.occupied_socket()
        listener.close()
        runner, config = self.runner("changed")
        original_bind = AdminHttpTransport.bind

        async def changed_bind(transport):
            await original_bind(transport)
            config.write_text("projects: {human: /project}\n")

        with patch("netizen.admin.web._AUTO_PORTS", range(port, min(port + 100, 65536))), patch.object(AdminHttpTransport, "bind", changed_bind):
            with self.assertRaisesRegex(AdminPortConfigurationError, "changed since startup"):
                await runner.bind()
        self.assertEqual(config.read_text(), "projects: {human: /project}\n")
        self.assertEqual(runner.transport.state, AdminHttpState.CLOSED)
        self.assertEqual(runner.urls, ())
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))

    async def test_persistence_failure_closes_actual_listener(self) -> None:
        listener, port = self.occupied_socket()
        listener.close()
        runner, config = self.runner("write-failed")
        before = config.read_bytes()
        with patch("netizen.admin.web._AUTO_PORTS", range(port, min(port + 100, 65536))), patch("netizen.admin.web.persist_admin_port", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                await runner.bind()
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(runner.transport.state, AdminHttpState.CLOSED)
        self.assertEqual(runner.urls, ())

    async def test_restart_reuses_recorded_port_without_rewriting_configuration(self) -> None:
        listener, port = self.occupied_socket()
        listener.close()
        first, config = self.runner("persisted")
        with patch("netizen.admin.web._AUTO_PORTS", range(port, min(port + 100, 65536))):
            await first.bind()
        selected = first.addresses[0][1]
        await first.drain(asyncio.get_running_loop().time() + 1)
        first.close_auth()
        snapshot = ConfigFileSnapshot.read(config)
        restarted = AdminWebRunner(
            host="127.0.0.1", port=selected, credential_path=config.parent / "credential",
            instance_root=config.parent, config_path=config, config_snapshot=snapshot,
        )
        self.runners.append(restarted)
        with patch("netizen.admin.web.persist_admin_port", side_effect=AssertionError("unexpected rewrite")):
            await restarted.bind()
        self.assertEqual(ConfigFileSnapshot.read(config), snapshot)
        self.assertEqual(restarted.addresses[0][1], selected)

    async def test_access_host_is_both_advertised_and_authorized(self) -> None:
        runner, _ = self.runner("access", port=0, access_host="admin.example.com")
        await runner.bind()
        port = runner.addresses[0][1]
        runner.attach_management(FakeManagement(self.root))
        runner.open_admission()
        self.assertEqual(runner.urls, (f"http://admin.example.com:{port}/",))
        response = await runner.application.handle(_request("GET", "/login", f"admin.example.com:{port}"))
        self.assertEqual(response.status, 200)
        bad_port = await runner.application.handle(_request("GET", "/login", f"admin.example.com:{port + 1}"))
        self.assertEqual(bad_port.status, 400)
        cookies = _response_cookies(response)
        nonce = re.search(rb"name='nonce' value='([^']+)'", response.body).group(1).decode()
        response = await runner.application.handle(_request(
            "POST", "/login", f"admin.example.com:{port}",
            cookies=cookies,
            origin=f"http://admin.example.com:{port}",
            form={"nonce": nonce, "credential": (self.root / "access" / "credential").read_text()},
        ))
        self.assertEqual(response.status, 303)


class AdminInstancePresentationTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_roots_have_independent_cookie_names_and_safe_root_display(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            applications = []
            credential = secrets.token_urlsafe(32)
            for name in ("one <unsafe>", "two"):
                instance = root / name
                instance.mkdir()
                secret = instance / "credential"
                secret.write_text(credential)
                secret.chmod(0o600)
                app = AdminWebApplication(auth=AdminAuth(secret), management=FakeManagement(root), instance_root=instance)
                app.configure_authorities(("127.0.0.1:8787",))
                app.open_admission()
                self.addCleanup(app.close_auth)
                applications.append((instance, app))
            browser_cookies = {}
            for instance, app in applications:
                response = await app.handle(_request("GET", "/login", cookies=browser_cookies))
                self.assertIn(html.escape(str(instance)).encode(), response.body)
                self.assertNotIn(b"one <unsafe>", response.body)
                browser_cookies.update(_response_cookies(response))
                nonce = re.search(rb"name='nonce' value='([^']+)'", response.body).group(1).decode()
                response = await app.handle(_request(
                    "POST", "/login", cookies=browser_cookies,
                    origin="http://127.0.0.1:8787", form={"nonce": nonce, "credential": credential},
                ))
                self.assertEqual(response.status, 303)
                issued = _response_cookies(response)
                self.assertIn(f"netizen_admin_session_{instance_digest(instance)}", issued)
                self.assertEqual(issued[f"netizen_admin_preauth_{instance_digest(instance)}"], "")
                browser_cookies.update(issued)
            for instance, app in applications:
                response = await app.handle(_request("GET", "/", cookies=browser_cookies))
                self.assertEqual(response.status, 200)
                self.assertIn(html.escape(str(instance)).encode(), response.body)
                self.assertNotIn(b"__NETIZEN_INSTANCE_ROOT__", response.body)
                response = await app.handle(_request("GET", "/api/v1/projects", cookies={"netizen_admin_session": "wrong-instance"}))
                self.assertEqual(response.status, 401)
            first_instance, first = applications[0]
            response = await first.handle(_request("POST", "/logout", cookies=browser_cookies, origin="http://127.0.0.1:8787"))
            self.assertEqual(response.status, 204)
            cleared = _response_cookies(response)
            self.assertEqual(cleared, {f"netizen_admin_session_{instance_digest(first_instance)}": ""})
            browser_cookies.update(cleared)
            second = applications[1][1]
            self.assertEqual((await second.handle(_request("GET", "/", cookies=browser_cookies))).status, 200)

    def test_url_projection_uses_actual_port_and_avoids_wildcard_or_unbound_hosts(self) -> None:
        authorities = ("0.0.0.0:8890", "[::]:8890", "localhost:8890", "127.0.0.1:8890", "192.0.2.4:8890", "server.test:8890")
        urls = admin_access_urls("0.0.0.0", (("0.0.0.0", 8890),), authorities)
        self.assertEqual(urls, ("http://192.0.2.4:8890/",))
        self.assertEqual(admin_access_urls("::1", (("::1", 8890, 0, 0),), ("[::1]:8890",)), ("http://[::1]:8890/",))
        self.assertEqual(admin_access_urls("127.0.0.1", (), authorities), ())
        self.assertEqual(admin_access_urls("192.0.2.4", (("192.0.2.4", 8890),), authorities), ("http://192.0.2.4:8890/",))

    def test_wildcard_url_candidates_match_the_actually_bound_address_families(self) -> None:
        authorities = (
            "192.0.2.4:8890", "[2001:db8::1]:8890", "[::1]:8890", "127.0.0.1:8890",
        )
        for host, addresses, expected in (
            ("0.0.0.0", (("0.0.0.0", 8890),), ("http://192.0.2.4:8890/",)),
            ("::", (("::", 8890, 0, 0),), ("http://[2001:db8::1]:8890/",)),
            ("::", (("::", 8890, 0, 0), ("0.0.0.0", 8890)),
             ("http://192.0.2.4:8890/", "http://[2001:db8::1]:8890/")),
        ):
            with self.subTest(host=host, addresses=addresses):
                self.assertEqual(admin_access_urls(host, addresses, authorities), expected)

    def test_wildcard_loopback_fallback_uses_bound_family_and_port(self) -> None:
        authorities = ("127.0.0.1:8787", "[::]:8787", "[::1]:8787")
        self.assertEqual(
            admin_access_urls("::", (("::", 8787, 0, 0),), authorities),
            ("http://[::1]:8787/",),
        )
        self.assertEqual(
            admin_access_urls("0.0.0.0", (("0.0.0.0", 8787),), authorities),
            ("http://127.0.0.1:8787/",),
        )
        self.assertEqual(admin_access_urls("::", (), authorities), ())
        self.assertEqual(
            admin_access_urls("::", (("::", 8787, 0, 0),), ("127.0.0.1:8787",)), (),
        )

    def test_discovered_hostnames_do_not_hide_bound_family_loopback_fallback(self) -> None:
        for host, bound, discovered, expected in (
            ("::", ("::", 8890, 0, 0),
             (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.4", 0)),
             "http://[::1]:8890/"),
            ("0.0.0.0", ("0.0.0.0", 8890),
             (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 0, 0, 0)),
             "http://127.0.0.1:8890/"),
        ):
            with (
                self.subTest(host=host),
                patch("netizen.admin.web.socket.gethostname", return_value="server"),
                patch("netizen.admin.web.socket.getfqdn", return_value="server.example.test"),
                patch("netizen.admin.web.socket.getaddrinfo", return_value=[discovered]),
            ):
                authorities = accepted_authorities(host, 8890, (bound,))
                self.assertIn("server.example.test:8890", authorities)
                self.assertEqual(admin_access_urls(host, (bound,), authorities), (expected,))

    def test_explicit_access_hostname_is_preserved_for_single_family_listener(self) -> None:
        authorities = ("[::1]:8890", "192.0.2.4:8890", "admin.example.test:8890")
        self.assertEqual(
            admin_access_urls("::", (("::", 8890, 0, 0),), authorities, "admin.example.test"),
            ("http://admin.example.test:8890/",),
        )

    def test_wildcard_candidates_match_each_family_port_and_stay_bounded(self) -> None:
        authorities = (
            "192.0.2.4:8787", "192.0.2.4:8788", "[2001:db8::1]:8787", "[2001:db8::1]:8788",
            "192.0.2.5:8787", "192.0.2.6:8787", "192.0.2.7:8787", "192.0.2.8:8787",
        )
        self.assertEqual(
            admin_access_urls("::", (("0.0.0.0", 8787), ("::", 8788, 0, 0)), authorities),
            ("http://192.0.2.4:8787/", "http://[2001:db8::1]:8788/",
             "http://192.0.2.5:8787/", "http://192.0.2.6:8787/"),
        )


def _request(method, path, authority="127.0.0.1:8787", *, cookies=None, origin=None, form=None):
    headers = [(b"Host", authority.encode())]
    if cookies:
        headers.append((b"Cookie", "; ".join(f"{key}={value}" for key, value in cookies.items()).encode()))
    if origin:
        headers.append((b"Origin", origin.encode()))
    body = b""
    if form:
        headers.append((b"Content-Type", b"application/x-www-form-urlencoded"))
        body = urlencode(form).encode()
    return Request(method.encode(), path.encode(), b"1.1", tuple(headers), body, ("127.0.0.1", 1234), ("127.0.0.1", 8787), "test")


def _response_cookies(response):
    parsed = SimpleCookie()
    for key, value in response.headers:
        if key.lower() == b"set-cookie":
            parsed.load(value.decode())
    return {name: morsel.value for name, morsel in parsed.items()}
