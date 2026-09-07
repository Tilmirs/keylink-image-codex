from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
CLIENT = WORKSPACE_ROOT / "skills" / "keylink-image" / "scripts" / "keylink_image.py"
sys.path.insert(0, str(CLIENT.parent))
import keylink_image as client_module
try:
    from PIL import Image
except ImportError:
    Image = None
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")


class TestServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), RequestHandler)
        self.routes: dict[tuple[str, str], Any] = {}
        self.requests: list[dict[str, Any]] = []


class RequestHandler(BaseHTTPRequestHandler):
    server: TestServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        self.server.requests.append(
            {
                "method": method,
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        )
        route = self.server.routes.get((method, self.path))
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        if callable(route):
            status, content_type, payload = route(self.server.requests[-1])
        else:
            status, content_type, payload = route
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextlib.contextmanager
def running_server() -> Iterator[TestServer]:
    server = TestServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def json_response(payload: Any, status: int = 200) -> tuple[int, str, Any]:
    return status, "application/json", payload


class KeylinkImageTests(unittest.TestCase):
    def run_client(
        self,
        workspace: Path,
        server: TestServer,
        *args: str,
        thread_id: str = "test-thread",
        auto_select: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["CODEX_THREAD_ID"] = thread_id
        env.pop("KEYLINK_API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
        env.pop("KEYLINK_IMAGE_STATE_DIR", None)
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        effective_args = list(args)
        if effective_args and effective_args[0] == "run" and auto_select:
            model_index = effective_args.index("--model") + 1
            selected_model = effective_args[model_index]
            server.routes[("GET", "/v1/models")] = json_response(
                {"data": [{"id": selected_model}]}
            )
            discovery = subprocess.run(
                [
                    sys.executable,
                    str(CLIENT),
                    "models",
                    "--base-url",
                    base_url,
                ],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(
                discovery.returncode, 0, discovery.stdout + discovery.stderr
            )
            selection = json.loads(discovery.stdout)
            effective_args.extend(
                ["--selection-token", selection["selection_token"]]
            )
            server.requests.clear()
        completed = subprocess.run(
            [
                sys.executable,
                str(CLIENT),
                *effective_args,
                *([] if args[0] == "last" else ["--base-url", base_url]),
            ],
            cwd=workspace,
            env=env,
            capture_output=True,
                text=True,
            timeout=15,
            check=False,
        )
        payload = json.loads(completed.stdout)
        return completed, payload

    def test_gpt_image_falls_back_from_images_no_image_to_chat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary)
            server.routes[("POST", "/v1/images/generations")] = json_response(
                {"data": []}
            )
            server.routes[("POST", "/v1/chat/completions")] = json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:image/png;base64,{PNG_B64}"
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

            completed, payload = self.run_client(
                workspace,
                server,
                "run",
                "--prompt",
                "a lighthouse",
                "--model",
                "gpt-image-2",
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(payload["endpoint"], "chat")
            self.assertEqual(
                [request["path"] for request in server.requests],
                ["/v1/images/generations", "/v1/chat/completions"],
            )
            for request in server.requests:
                body = json.loads(request["body"])
                self.assertEqual(body["model"], "gpt-image-2")
                self.assertEqual(body["size"], "1024x1024")
            self.assertTrue(Path(payload["images"][0]["path"]).is_file())

    def test_last_recovers_all_saved_images_without_api_or_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary)
            # Different image bytes prevent response deduplication.
            second_b64 = base64.b64encode(PNG_BYTES + b"\n").decode("ascii")
            server.routes[("POST", "/v1/images/generations")] = json_response(
                {"data": [{"b64_json": PNG_B64}, {"b64_json": second_b64}]}
            )
            first, generated = self.run_client(
                workspace, server, "run", "--prompt", "a landscape", "--model", "gpt-image-2"
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            server.requests.clear()
            last, recovered = self.run_client(workspace, server, "last")
            self.assertEqual(last.returncode, 0, last.stdout + last.stderr)
            self.assertTrue(recovered["recovered"])
            self.assertEqual(recovered["images"], generated["images"])
            self.assertEqual(len(recovered["images"]), 2)
            self.assertEqual(server.requests, [])
            for item in recovered["images"]:
                self.assertNotIn("\\", item["display_markdown"])
                self.assertIn(Path(item["path"]).as_posix(), item["original_markdown"])
            other, error = self.run_client(workspace, server, "last", thread_id="other-task")
            self.assertEqual(other.returncode, 1)
            self.assertIn("No successful image", error["error"])

    @unittest.skipIf(Image is None, "Pillow display previews are optional")
    def test_last_recovers_legacy_4k_result_and_preserves_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary)
            original = workspace / "original 4k.png"
            Image.new("RGB", (3840, 2160), "orange").save(original)
            before = original.read_bytes()
            state_dir = workspace / ".keylink-image" / "threads" / "test-thread"
            state_dir.mkdir(parents=True)
            state_path = state_dir / "last.json"
            state_path.write_text(json.dumps({
                "path": str(original), "model": "gpt-image-2", "endpoint": "images-edits",
                "requested_size": "3840x2160", "saved_at": "2026-09-07T08:14:27Z",
            }), encoding="utf-8")
            state_before = state_path.read_bytes()
            completed, result = self.run_client(
                workspace, server, "last", "--preview-dir", str(workspace / "previews")
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            item = result["images"][0]
            self.assertEqual((item["width"], item["height"]), (3840, 2160))
            with Image.open(item["preview_path"]) as preview:
                preview.load()
                self.assertEqual(preview.size, (1600, 900))
            self.assertLess(Path(item["preview_path"]).stat().st_size, 1024 * 1024)
            self.assertEqual(original.read_bytes(), before)
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(client_module.load_last_image(state_dir), original)
            self.assertEqual(server.requests, [])
            original.unlink()
            failed, error = self.run_client(workspace, server, "last")
            self.assertEqual(failed.returncode, 1)
            self.assertIn("no longer exists", error["error"])

    @unittest.skipIf(Image is None, "Pillow display previews are optional")
    def test_generation_saves_preview_but_next_edit_uses_4k_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary)
            raw = io.BytesIO()
            Image.new("RGB", (3840, 2160), "red").save(raw, "PNG")
            server.routes[("POST", "/v1/images/generations")] = json_response(
                {"data": [{"b64_json": base64.b64encode(raw.getvalue()).decode("ascii")}]}
            )
            completed, result = self.run_client(
                workspace, server, "run", "--prompt", "a landscape", "--model", "gpt-image-2",
                "--size", "3840x2160", "--confirm-high-res",
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            original = result["images"][0]["path"]
            self.assertNotEqual(original, result["images"][0]["display_path"])
            server.routes[("POST", "/v1/images/edits")] = json_response(
                {"data": [{"b64_json": PNG_B64}]}
            )
            edited, edit_result = self.run_client(
                workspace, server, "run", "--prompt", "change only the sky", "--model", "gpt-image-2",
            )
            self.assertEqual(edited.returncode, 0, edited.stdout + edited.stderr)
            self.assertEqual(edit_result["references"], [original])
            self.assertIn(raw.getvalue(), server.requests[0]["body"])

    def test_missing_pillow_keeps_original_deliverable(self) -> None:
        saved = [{"path": str(Path("original.png").resolve()), "bytes": 12000000,
                  "width": 3840, "height": 2160}]
        with mock.patch.dict(sys.modules, {"PIL": None}):
            warnings = client_module.prepare_display(saved)
        self.assertTrue(warnings)
        self.assertEqual(saved[0]["display_path"], saved[0]["path"])
        self.assertNotIn("preview_path", saved[0])

    @unittest.skipIf(Image is None, "Pillow display previews are optional")
    def test_preview_write_failure_keeps_original_deliverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            original = Path(temporary) / "original.png"
            Image.new("RGB", (3840, 2160), "red").save(original)
            before = original.read_bytes()
            saved = [{"path": str(original), "bytes": len(before), "width": 3840, "height": 2160}]
            with mock.patch.object(Image.Image, "save", side_effect=PermissionError("read-only")):
                warnings = client_module.prepare_display(saved)
            self.assertTrue(warnings)
            self.assertEqual(saved[0]["display_path"], str(original))
            self.assertEqual(original.read_bytes(), before)

    def test_gemini_edit_sends_both_chat_reference_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary)
            reference = workspace / "reference.png"
            reference.write_bytes(PNG_BYTES)
            server.routes[("POST", "/v1/chat/completions")] = json_response(
                {"choices": [{"message": {"images": [{"b64_json": PNG_B64}]}}]}
            )

            completed, payload = self.run_client(
                workspace,
                server,
                "run",
                "--prompt",
                "change the background only",
                "--model",
                "gemini-3.1-flash-image",
                "--image",
                str(reference),
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(payload["endpoint"], "chat")
            self.assertEqual(payload["reference_source"], "uploaded")
            self.assertEqual(
                [request["path"] for request in server.requests],
                ["/v1/images/edits", "/v1/chat/completions"],
            )
            request_payload = json.loads(server.requests[1]["body"])
            self.assertTrue(request_payload["images"][0]["image_url"].startswith("data:image/png;base64,"))
            content = request_payload["messages"][0]["content"]
            image_content = [item for item in content if item["type"] == "image_url"]
            self.assertEqual(len(image_content), 1)
            self.assertTrue(image_content[0]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_gemini_generation_uses_images_before_chat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary)
            server.routes[("POST", "/v1/images/generations")] = json_response(
                {"data": [{"b64_json": PNG_B64}]}
            )
            server.routes[("POST", "/v1/chat/completions")] = json_response(
                {"data": [{"b64_json": PNG_B64}]}
            )

            completed, payload = self.run_client(
                workspace,
                server,
                "run",
                "--prompt",
                "a red planet",
                "--model",
                "gemini-3-pro-image",
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(payload["endpoint"], "images-generations")
            self.assertEqual(
                [request["path"] for request in server.requests],
                ["/v1/images/generations"],
            )

    def test_images_edit_uses_multipart_image_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary)
            reference = workspace / "reference.png"
            reference.write_bytes(PNG_BYTES)
            server.routes[("POST", "/v1/images/edits")] = json_response(
                {"data": [{"b64_json": PNG_B64}]}
            )

            completed, payload = self.run_client(
                workspace,
                server,
                "run",
                "--prompt",
                "remove the sign",
                "--model",
                "gpt-image-2",
                "--image",
                str(reference),
                "--endpoint",
                "images",
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(payload["endpoint"], "images-edits")
            request = server.requests[0]
            self.assertIn("multipart/form-data", request["headers"]["Content-Type"])
            self.assertIn(b'name="image"; filename="reference.png"', request["body"])
            self.assertIn(b'name="prompt"\r\n\r\nremove the sign\r\n', request["body"])

    def test_explicit_images_endpoint_does_not_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary)
            server.routes[("POST", "/v1/images/generations")] = json_response(
                {"error": {"message": "unsupported"}}, status=400
            )
            server.routes[("POST", "/v1/chat/completions")] = json_response(
                {"data": [{"b64_json": PNG_B64}]}
            )

            completed, payload = self.run_client(
                workspace,
                server,
                "run",
                "--prompt",
                "a tree",
                "--model",
                "gpt-image-2",
                "--endpoint",
                "images",
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual([request["path"] for request in server.requests], ["/v1/images/generations"])
            self.assertFalse(payload["ask_user_to_switch_model"])
            self.assertEqual(len(payload["attempts"]), 1)

    def test_non_conservative_size_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary)
            completed, payload = self.run_client(
                workspace,
                server,
                "run",
                "--prompt",
                "a wide scene",
                "--model",
                "gpt-image-2",
                "--size",
                "3840x2160",
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("requires user confirmation", payload["error"])
            self.assertEqual(server.requests, [])

    def test_high_resolution_prompt_cannot_silently_send_default_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary)
            completed, payload = self.run_client(
                workspace,
                server,
                "run",
                "--prompt",
                "generate this in 4K",
                "--model",
                "gpt-image-2",
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("high-resolution intent", payload["error"])
            self.assertEqual(server.requests, [])

    def test_models_reports_published_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary)
            server.routes[("GET", "/v1/models")] = json_response(
                {
                    "data": [
                        {
                            "id": "gpt-image-2",
                            "capabilities": {
                                "sizes": ["1024x1024", "1536x1024", "2048x2048"]
                            },
                        },
                        {"id": "text-model"},
                    ]
                }
            )

            completed, payload = self.run_client(workspace, server, "models")

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            image_model = next(item for item in payload["models"] if item["id"] == "gpt-image-2")
            self.assertEqual(
                image_model["published_sizes"],
                ["1024x1024", "1536x1024", "2048x2048"],
            )
            self.assertEqual(payload["image_models"], [image_model])
            self.assertTrue(payload["selection_token"])
            self.assertTrue(payload["selection_required"])
            self.assertIn("wait patiently", payload["four_k_notice"])

    def test_run_requires_model_discovery_selection_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            completed, payload = self.run_client(
                Path(temporary),
                server,
                "run",
                "--prompt",
                "a lighthouse",
                "--model",
                "gpt-image-2",
                auto_select=False,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("--selection-token is required", payload["error"])
            self.assertEqual(server.requests, [])

    def test_selection_requires_a_model_from_latest_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary)
            server.routes[("GET", "/v1/models")] = json_response(
                {"data": [{"id": "gpt-image-2"}]}
            )
            discovery, selection = self.run_client(workspace, server, "models")
            self.assertEqual(discovery.returncode, 0, discovery.stdout + discovery.stderr)
            server.requests.clear()

            completed, payload = self.run_client(
                workspace,
                server,
                "run",
                "--prompt",
                "a lighthouse",
                "--model",
                "gemini-3.1-flash-image",
                "--selection-token",
                selection["selection_token"],
                auto_select=False,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("was not present", payload["error"])
            self.assertEqual(server.requests, [])

    def test_selection_token_is_one_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary)
            server.routes[("GET", "/v1/models")] = json_response(
                {"data": [{"id": "gpt-image-2"}]}
            )
            server.routes[("POST", "/v1/images/generations")] = json_response(
                {"data": [{"b64_json": PNG_B64}]}
            )
            discovery, selection = self.run_client(workspace, server, "models")
            self.assertEqual(discovery.returncode, 0, discovery.stdout + discovery.stderr)
            token = selection["selection_token"]
            server.requests.clear()

            first, _ = self.run_client(
                workspace,
                server,
                "run",
                "--prompt",
                "first image",
                "--model",
                "gpt-image-2",
                "--selection-token",
                token,
                auto_select=False,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            server.requests.clear()

            second, payload = self.run_client(
                workspace,
                server,
                "run",
                "--prompt",
                "second image",
                "--model",
                "gpt-image-2",
                "--selection-token",
                token,
                auto_select=False,
            )
            self.assertEqual(second.returncode, 1)
            self.assertIn("already been used", payload["error"])
            self.assertEqual(server.requests, [])

    def test_direct_keylink_ignores_codex_ccswitch_and_generic_api_urls(self) -> None:
        ccswitch = client_module.CCSwitchState(
            proxy_base_url="http://127.0.0.1:15721",
            provider_base_urls=["https://other-provider.example"],
        )
        with mock.patch.dict(os.environ, {
            "OPENAI_BASE_URL": "http://127.0.0.1:15721/v1",
            "OPENAI_API_BASE": "https://other-provider.example/v1",
        }, clear=True), mock.patch.object(
            client_module, "discover_ccswitch_state", return_value=ccswitch
        ), mock.patch.object(client_module, "discover_codex_base_url") as read_codex:
            base_url, source, _ = client_module.discover_base_url(None)
        self.assertEqual(base_url, "https://keylinkclub.com")
        self.assertEqual(source, "default")
        read_codex.assert_not_called()

    def test_explicit_keylink_address_overrides_remain_fixed(self) -> None:
        with mock.patch.dict(os.environ, {
            "KEYLINK_BASE_URL": "https://keylink-override.example/v1",
        }, clear=True), mock.patch.object(
            client_module, "discover_ccswitch_state", return_value=client_module.CCSwitchState()
        ):
            base_url, source, _ = client_module.discover_base_url(None)
            self.assertEqual((base_url, source), (
                "https://keylink-override.example", "environment:KEYLINK_BASE_URL"
            ))
            base_url, source, _ = client_module.discover_base_url("http://localhost:4321/v1")
            self.assertEqual((base_url, source), ("http://localhost:4321", "argument"))

    def test_keylink_credentials_are_not_reused_for_an_unrelated_host(self) -> None:
        ccswitch = client_module.CCSwitchState(
            provider_base_urls=["https://keylinkclub.com"], api_key="test-key-only"
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(client_module.discover_api_key(
                "https://keylinkclub.com", "default", ccswitch
            ), ("test-key-only", "ccswitch-current-provider"))
            self.assertEqual(client_module.discover_api_key(
                "https://unrelated.example", "argument", ccswitch
            ), (None, None))

    def test_models_failure_is_reported_without_switching_servers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            server.routes[("GET", "/v1/models")] = json_response(
                {"error": "catalog unavailable"}, status=503
            )
            completed, payload = self.run_client(Path(temporary), server, "models")
            self.assertEqual(completed.returncode, 1)
            self.assertIn("HTTP 503", payload["error"])
            self.assertEqual(len(server.requests), 1)

    def test_blank_prompt_is_rejected_before_any_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            completed, payload = self.run_client(
                Path(temporary), server, "run", "--prompt", "  ", "--model", "gpt-image-2"
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("non-empty --prompt", payload["error"])
            self.assertEqual(server.requests, [])

    def test_proxy_download_connection_failure_recovers_same_image_directly(self) -> None:
        with running_server() as server:
            server.routes[("GET", "/generated/moon.png")] = (200, "image/png", PNG_BYTES)
            url = f"http://127.0.0.1:{server.server_address[1]}/generated/moon.png"
            client = client_module.HttpClient("https://keylinkclub.com", "test-key", 5)
            broken_proxy = mock.Mock()
            broken_proxy.open.side_effect = urllib.error.URLError(ConnectionResetError("proxy reset"))
            client.opener = broken_proxy
            with mock.patch.object(client_module.urllib.request, "getproxies", return_value={
                "https": "http://localhost:9567"
            }):
                data, mime = client.download(url)
            self.assertEqual((data, mime), (PNG_BYTES, "image/png"))
            self.assertEqual(broken_proxy.open.call_count, 1)
            self.assertEqual([r["path"] for r in server.requests], ["/generated/moon.png"])
            self.assertNotIn("Authorization", server.requests[0]["headers"])

    def test_download_http_error_does_not_trigger_direct_retry(self) -> None:
        client = client_module.HttpClient("https://keylinkclub.com", None, 5)
        request_url = "https://images.example/missing.png"
        client.opener = mock.Mock()
        client.opener.open.side_effect = urllib.error.HTTPError(
            request_url, 404, "Not Found", {}, None
        )
        with mock.patch.object(client_module.urllib.request, "build_opener") as build:
            with self.assertRaisesRegex(client_module.ClientError, "HTTP 404"):
                client.download(request_url)
            build.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows launcher integration")
    def test_powershell_launcher_preserves_chinese_prompt_in_request_body(self) -> None:
        powershell = Path(sys.executable).parents[1] / "native" / "powershell" / "pwsh.exe"
        if not powershell.is_file():
            self.skipTest("Bundled PowerShell is unavailable")
        prompt = "完整月球悬浮在深黑太空中，月海与陨石坑清晰可见，无文字。"
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            server.routes[("GET", "/v1/models")] = json_response(
                {"data": [{"id": "gpt-image-2"}]}
            )
            server.routes[("POST", "/v1/images/generations")] = json_response(
                {"data": [{"b64_json": PNG_B64}]}
            )
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env.pop("KEYLINK_API_KEY", None)
            env.pop("OPENAI_API_KEY", None)
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            discovery = subprocess.run([
                sys.executable, str(CLIENT), "models", "--base-url", base_url,
            ], cwd=temporary, env=env, capture_output=True, text=True,
                encoding="utf-8", timeout=15, check=False)
            self.assertEqual(discovery.returncode, 0, discovery.stdout + discovery.stderr)
            token = json.loads(discovery.stdout)["selection_token"]
            server.requests.clear()
            completed = subprocess.run([
                str(powershell), "-NoProfile", "-File", str(CLIENT.with_name("keylink-image.ps1")),
                "run", "--prompt", prompt, "--model", "gpt-image-2", "--endpoint", "images",
                "--selection-token", token, "--base-url", base_url,
            ], cwd=temporary, env=env, capture_output=True, text=True,
                encoding="utf-8", timeout=15, check=False)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            body = json.loads(server.requests[0]["body"])
            self.assertEqual(body["prompt"], prompt)
            self.assertEqual(body["size"], "1024x1024")

    def test_correction_intent_reuses_thread_scoped_successful_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary)
            server.routes[("POST", "/v1/images/generations")] = json_response(
                {"data": [{"b64_json": PNG_B64}]}
            )
            first, first_payload = self.run_client(
                workspace,
                server,
                "run",
                "--prompt",
                "first image",
                "--model",
                "gpt-image-2",
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertTrue(Path(first_payload["images"][0]["path"]).is_file())

            server.requests.clear()
            server.routes[("POST", "/v1/chat/completions")] = json_response(
                {"data": [{"b64_json": PNG_B64}]}
            )
            second, second_payload = self.run_client(
                workspace,
                server,
                "run",
                "--prompt",
                "change only the color",
                "--model",
                "gemini-3-pro-image",
            )

            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(second_payload["reference_source"], "auto-correction-last-image")
            self.assertEqual(
                [request["path"] for request in server.requests],
                ["/v1/images/edits", "/v1/chat/completions"],
            )
            request_payload = json.loads(server.requests[-1]["body"])
            self.assertIn("images", request_payload)


if __name__ == "__main__":
    unittest.main()
