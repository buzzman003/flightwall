"""Verify the GUI HTTP client and official MCP SDK against the real backend."""
import asyncio
from pathlib import Path
import socket
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
import re

from flightwall.backend import Runtime
from flightwall.config import ConfigStore
from flightwall.display import BackendConnection

try:
    import httpx
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata
    from mcp.client.streamable_http import streamable_http_client
except ImportError:
    ClientSession = None


class BackendRemoteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "data"
        self.install_dir = Path(self.temp.name) / "install"
        # Configuration rejects port zero; reserve a free ephemeral test port.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        store = ConfigStore(self.data_dir)
        store.update({"control_port": port, "public_url": "https://wall.example.com"})
        self.runtime = Runtime(self.data_dir, self.install_dir).start()
        self.connection = BackendConnection(self.data_dir, store.get())

    def tearDown(self):
        self.runtime.stop()
        self.temp.cleanup()

    def test_gui_maintenance_roundtrip_and_saved_settings(self):
        status = self.connection.status()
        self.assertTrue(status["snapshot"]["demo"])
        self.assertGreater(len(status["snapshot"]["flights"]), 0)
        self.assertNotIn("control_port", status["config"])
        self.assertNotIn("calendar_id", status["config"])
        result = self.connection.settings({"brightness": 0.45, "rotation_seconds": 12})
        self.assertTrue(result["updated"])
        self.assertEqual(result["config"]["brightness"], 0.45)
        self.assertEqual(self.connection.status()["config"]["rotation_seconds"], 12)
        self.assertEqual(ConfigStore(self.data_dir).get()["brightness"], 0.45)
        for invalid in ({"brightness": 2}, {"control_port": 9876}, {"demo": False}, {"update_public_key": "untrusted"}):
            with self.assertRaises(HTTPError) as caught:
                self.connection.settings(invalid)
            self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.connection.request("calendar/refresh", {})["queued"], True)
        self.assertEqual(self.connection.request("updates/check", {})["status"], "disabled")

    @unittest.skipIf(ClientSession is None, "Optional official mcp SDK not installed")
    def test_rejected_post_advertises_close_before_pooled_oauth_discovery(self):
        # The server rejects this body before reading it. HTTP/1.1 clients must
        # be told to discard the socket instead of racing its eventual EOF.
        url = f"http://127.0.0.1:{self.runtime.remote.address[1]}"
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "Synthetic pooled client", "version": "1"}}}
        token = (self.data_dir / "control-token.txt").read_text().strip()
        with httpx.Client(trust_env=False, timeout=5,
                          limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)) as client:
            denied = client.post(url + "/mcp", json=initialize)
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(denied.headers.get("connection"), "close")
            self.assertIn("resource_metadata=", denied.headers["www-authenticate"])
            discovery = client.get(url + "/.well-known/oauth-protected-resource/mcp")
            self.assertEqual(discovery.status_code, 200)
            self.assertEqual(discovery.json()["resource"], "https://wall.example.com/mcp")
            initialized = client.post(url + "/mcp", json=initialize,
                                      headers={"Authorization": "Bearer " + token})
            self.assertEqual(initialized.status_code, 200)
            self.assertEqual(initialized.json()["id"], 1)
            self.assertEqual(initialized.json()["result"]["serverInfo"]["name"], "flightwall")

    @unittest.skipIf(ClientSession is None, "Optional official mcp SDK not installed")
    def test_official_mcp_sdk_streamable_http_against_runtime(self):
        async def exercise():
            token = (self.data_dir / "control-token.txt").read_text().strip()

            url = f"http://127.0.0.1:{self.runtime.remote.address[1]}/mcp"
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as http_client:
                async with streamable_http_client(url, http_client=http_client) as (reader, writer, session_id):
                    async with ClientSession(reader, writer) as session:
                        initialized = await session.initialize()
                        self.assertIn(initialized.protocolVersion, ("2025-11-25", "2025-06-18", "2025-03-26"))
                        self.assertEqual(initialized.serverInfo.name, "flightwall")
                        self.assertIsNone(session_id())
                        tools = await session.list_tools()
                        self.assertEqual({tool.name for tool in tools.tools},
                                         {"get_status", "update_settings", "refresh_calendar", "check_updates"})
                        status = await session.call_tool("get_status", {})
                        self.assertFalse(status.isError)
                        self.assertTrue(status.structuredContent["snapshot"]["demo"])
                        result = await session.call_tool("update_settings", {"settings": {"brightness": 0.4}})
                        self.assertFalse(result.isError)
                        self.assertTrue(result.structuredContent["updated"])
                        self.assertEqual(self.connection.status()["config"]["brightness"], 0.4)
                        invalid = await session.call_tool("update_settings", {"settings": {"brightness": 20}})
                        self.assertTrue(invalid.isError)
                        refreshed = await session.call_tool("refresh_calendar", {})
                        self.assertTrue(refreshed.structuredContent["queued"])
                        update = await session.call_tool("check_updates", {})
                        self.assertEqual(update.structuredContent["status"], "disabled")

        asyncio.run(exercise())

    @unittest.skipIf(ClientSession is None, "Optional official mcp SDK not installed")
    def test_official_mcp_sdk_oauth_discovery_registration_pkce(self):
        port = self.runtime.remote.address[1]
        owner_secret = (self.data_dir / "control-token.txt").read_text().strip()

        class LoopbackTransport(httpx.AsyncBaseTransport):
            """Test only: preserve public URLs to the SDK, route sockets locally.

            This verifies OAuth wire semantics, not TLS or proxy deployment.
            """
            def __init__(self):
                self.inner = httpx.AsyncHTTPTransport()

            async def handle_async_request(self, request):
                if request.url.host != "wall.example.com":
                    raise AssertionError("Unexpected test request host")
                url = request.url.copy_with(scheme="http", host="127.0.0.1", port=port)
                proxied = httpx.Request(request.method, url, headers=request.headers,
                                       content=await request.aread(), extensions=request.extensions)
                return await self.inner.handle_async_request(proxied)

            async def aclose(self):
                await self.inner.aclose()

        class MemoryStorage:
            tokens, client = None, None

            async def get_tokens(self):
                return self.tokens

            async def set_tokens(self, tokens):
                self.tokens = tokens

            async def get_client_info(self):
                return self.client

            async def set_client_info(self, client):
                self.client = client

        async def exercise():
            callback = {}

            async def redirect_handler(url):
                async with httpx.AsyncClient(transport=LoopbackTransport(), trust_env=False) as browser:
                    page = await browser.get(url)
                    self.assertEqual(page.status_code, 200)
                    pending = re.search('name="pending" value="([^"]+)"', page.text).group(1)
                    csrf = re.search('name="csrf" value="([^"]+)"', page.text).group(1)
                    response = await browser.post("https://wall.example.com/oauth/authorize", data={
                        "pending": pending, "csrf": csrf, "admin_secret": owner_secret, "approve": "yes"})
                    self.assertEqual(response.status_code, 302)
                    values = parse_qs(urlsplit(response.headers["Location"]).query)
                    callback.update(code=values["code"][0], state=values["state"][0])

            async def callback_handler():
                return callback["code"], callback["state"]

            storage = MemoryStorage()
            provider = OAuthClientProvider(server_url="https://wall.example.com/mcp",
                client_metadata=OAuthClientMetadata(client_name="Official SDK integration test",
                    redirect_uris=["https://client.example/callback"], token_endpoint_auth_method="none",
                    grant_types=["authorization_code", "refresh_token"], response_types=["code"]),
                storage=storage, redirect_handler=redirect_handler, callback_handler=callback_handler)
            async with httpx.AsyncClient(transport=LoopbackTransport(), auth=provider, trust_env=False) as client:
                async with streamable_http_client("https://wall.example.com/mcp", http_client=client) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        initialized = await session.initialize()
                        self.assertEqual(initialized.serverInfo.name, "flightwall")
                        status = await session.call_tool("get_status", {})
                        self.assertFalse(status.isError)
                        self.assertTrue(status.structuredContent["snapshot"]["demo"])
                        self.assertIsNotNone(storage.client)
                        self.assertIsNotNone(storage.tokens.refresh_token)
                        self.assertEqual(storage.tokens.scope, "flightwall.control")

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
