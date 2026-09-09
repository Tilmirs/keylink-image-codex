from __future__ import annotations

import base64
import contextlib
import http.client
import io
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import xml.etree.ElementTree as ET
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
GPT_IMAGE_25_MODELS = (
    "gpt-image-2.5", "gpt-image-2.5-sunburst", "gpt-image-2.5-flare",
)


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
        force_scheduled_task: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["CODEX_THREAD_ID"] = thread_id
        env.pop("KEYLINK_API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
        env.pop("KEYLINK_IMAGE_STATE_DIR", None)
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        effective_args = list(args)
        if effective_args and effective_args[0] in {"run", "start"} and auto_select:
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
        entrypoint = [str(CLIENT)]
        if force_scheduled_task:
            entrypoint = ["-c", "\n".join([
                "import runpy, subprocess, sys",
                "popen = subprocess.Popen",
                "def deny_breakaway(*args, **kwargs):",
                "    if kwargs.get('creationflags', 0) & 0x01000000:",
                "        raise PermissionError('test: breakaway denied')",
                "    return popen(*args, **kwargs)",
                "subprocess.Popen = deny_breakaway",
                "sys.argv = sys.argv[1:]",
                "runpy.run_path(sys.argv[0], run_name='__main__')",
            ]), str(CLIENT)]
        completed = subprocess.run(
            [
                sys.executable,
                *entrypoint,
                *effective_args,
                *(["--base-url", base_url] if args[0] in {"models", "run", "start"} else []),
            ],
            cwd=workspace,
            env=env,
            capture_output=True,
                text=True,
            timeout=15,
            check=False,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            self.fail(f"Client exited {completed.returncode} without JSON: "
                      f"{completed.stdout}\n{completed.stderr}")
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
            self.assertEqual(client_module.load_last_image(state_dir), original.resolve())
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
            self.assertEqual(saved[0]["display_path"], str(original.resolve()))
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

    def test_gpt_image_25_catalog_keeps_distinct_variants_and_their_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            server.routes[("GET", "/v1/models")] = json_response({"data": [
                {"id": "gpt-image-2.5", "capabilities": {"sizes": ["1024x1024", "3840x2160"]}},
                {"id": "gpt-image-2.5-sunburst", "capabilities": {"sizes": ["1536x1024"]}},
                {"id": "gpt-image-2.5-flare"},
                {"id": "gpt-image-future-variant"},
                {"id": "text-model"},
            ]})
            completed, result = self.run_client(Path(temporary), server, "models")
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            found = {entry["id"]: entry["published_sizes"] for entry in result["image_models"]}
            self.assertEqual(found, {
                "gpt-image-2.5": ["1024x1024", "3840x2160"],
                "gpt-image-2.5-sunburst": ["1536x1024"],
                "gpt-image-2.5-flare": [],
                "gpt-image-future-variant": [],
            })
            self.assertEqual(result["preferred_sizes"], ["1024x1024", "1536x1024", "1024x1536"])
            self.assertEqual([r["path"] for r in server.requests], ["/v1/models"])

    def test_gpt_image_25_generation_and_continued_edit_preserve_variant(self) -> None:
        for model in GPT_IMAGE_25_MODELS:
            with self.subTest(model=model), tempfile.TemporaryDirectory() as temporary, running_server() as server:
                workspace = Path(temporary)
                server.routes[("POST", "/v1/images/generations")] = json_response(
                    {"data": [{"b64_json": PNG_B64}]}
                )
                generated, first = self.run_client(
                    workspace, server, "run", "--prompt", "a lighthouse", "--model", model,
                    "--aspect", "landscape",
                )
                self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
                self.assertEqual([r["path"] for r in server.requests], ["/v1/images/generations"])
                generation_body = json.loads(server.requests[0]["body"])
                self.assertEqual(generation_body["model"], model)
                self.assertEqual(generation_body["size"], "1536x1024")
                self.assertEqual(first["model"], model)
                original = Path(first["images"][0]["path"])
                self.assertEqual(original.read_bytes(), PNG_BYTES)

                server.routes[("POST", "/v1/images/edits")] = json_response(
                    {"error": "Images edit unavailable"}, status=400
                )
                server.routes[("POST", "/v1/chat/completions")] = json_response(
                    {"choices": [{"message": {"images": [{"b64_json": PNG_B64}]}}]}
                )
                prompt = "change only the sky to sunset"
                completed, edited = self.run_client(
                    workspace, server, "run", "--prompt", prompt, "--model", model,
                    "--use-last", "--size", "3840x2160", "--confirm-high-res",
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertEqual([r["path"] for r in server.requests],
                                 ["/v1/images/edits", "/v1/chat/completions"])
                multipart = server.requests[0]["body"]
                self.assertIn(f'name="model"\r\n\r\n{model}\r\n'.encode(), multipart)
                self.assertIn(b'name="size"\r\n\r\n3840x2160\r\n', multipart)
                self.assertIn(b'name="image"; filename=', multipart)
                self.assertIn(PNG_BYTES, multipart)
                chat = json.loads(server.requests[1]["body"])
                self.assertEqual(chat["model"], model)
                self.assertEqual(chat["size"], "3840x2160")
                content = chat["messages"][0]["content"]
                self.assertEqual([item["text"] for item in content if item["type"] == "text"], [prompt])
                data_url = f"data:image/png;base64,{PNG_B64}"
                self.assertEqual([item["image_url"]["url"] for item in content if item["type"] == "image_url"], [data_url])
                self.assertEqual(chat["images"], [{"image_url": data_url}])
                self.assertEqual(edited["model"], model)
                self.assertEqual(edited["references"], [str(original)])
                self.assertEqual(edited["endpoint"], "chat")
                self.assertEqual(edited["requested_size"], "3840x2160")
                self.assertTrue(Path(edited["images"][0]["path"]).is_file())
                self.assertTrue(any("not guaranteed" in warning for warning in edited["warnings"]))
                state = json.loads((workspace / ".keylink-image" / "threads" / "test-thread" / "last.json").read_text(encoding="utf-8"))
                self.assertEqual(state["model"], model)

    def test_gpt_image_25_explicit_chat_failure_does_not_switch_to_images(self) -> None:
        for model in GPT_IMAGE_25_MODELS:
            with self.subTest(model=model), tempfile.TemporaryDirectory() as temporary, running_server() as server:
                server.routes[("POST", "/v1/chat/completions")] = json_response({"choices": []})
                server.routes[("POST", "/v1/images/generations")] = json_response(
                    {"data": [{"b64_json": PNG_B64}]}
                )
                completed, result = self.run_client(
                    Path(temporary), server, "run", "--prompt", "a lighthouse", "--model", model,
                    "--endpoint", "chat",
                )
                self.assertEqual(completed.returncode, 1)
                self.assertEqual([r["path"] for r in server.requests], ["/v1/chat/completions"])
                self.assertEqual(json.loads(server.requests[0]["body"])["model"], model)
                self.assertEqual(result["model"], model)
                self.assertFalse(result["ask_user_to_switch_model"])

    def test_background_job_saves_once_after_launcher_exits(self) -> None:
        self.check_background_job_saves_once()

    @unittest.skipUnless(os.name == "nt", "Windows scheduled-task fallback")
    def test_scheduled_job_saves_once_after_launcher_exits(self) -> None:
        self.check_background_job_saves_once(force_scheduled_task=True)

    def check_background_job_saves_once(self, force_scheduled_task: bool = False) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary) / "image workspace with spaces & \u56fe\u7247"
            workspace.mkdir()
            request_received = threading.Event()
            release_response = threading.Event()

            def delayed_image(_request: dict[str, Any]) -> tuple[int, str, Any]:
                request_received.set()
                if not release_response.wait(10):
                    return json_response({"error": "test timeout"}, status=504)
                return json_response({"data": [{"b64_json": PNG_B64}]})

            server.routes[("POST", "/v1/images/generations")] = delayed_image
            started, job = self.run_client(
                workspace, server, "start", "--prompt", "a 4K black hole",
                "--model", "gpt-image-2.5", "--size", "3840x2160",
                "--confirm-high-res",
                force_scheduled_task=force_scheduled_task,
            )
            self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
            self.assertEqual(job["status"], "started")
            self.assertTrue(job["background"])
            if force_scheduled_task:
                self.assertEqual(job["launch_mode"], "scheduled-task")
            self.assertTrue(request_received.wait(5), "background request did not start")

            running, running_status = self.run_client(
                workspace, server, "status", "--job-id", job["job_id"],
                auto_select=False,
            )
            self.assertEqual(running.returncode, 0, running.stdout + running.stderr)
            self.assertEqual(running_status["status"], "running")
            self.assertIn("do not resubmit", running_status["message"])

            release_response.set()
            deadline = time.monotonic() + 10
            while True:
                completed, result = self.run_client(
                    workspace, server, "status", "--job-id", job["job_id"],
                    auto_select=False,
                )
                if result["status"] != "running":
                    break
                if time.monotonic() >= deadline:
                    self.fail("background image job did not finish")
                time.sleep(0.1)
            job_dir = (workspace / ".keylink-image" / "threads" / "test-thread"
                       / "jobs" / job["job_id"])
            diagnostic = "\n".join(
                f"{path.name}: {path.read_text(encoding='utf-8', errors='replace')}"
                for path in job_dir.glob("*") if path.is_file()
            )
            self.assertEqual(
                completed.returncode, 0,
                completed.stdout + completed.stderr + "\n" + diagnostic,
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["job_status"], "succeeded")
            self.assertEqual(result["model"], "gpt-image-2.5")
            self.assertEqual(result["requested_size"], "3840x2160")
            self.assertEqual([request["path"] for request in server.requests],
                             ["/v1/images/generations"])
            output = Path(result["images"][0]["path"])
            self.assertTrue(output.is_file())
            self.assertEqual(output.read_bytes(), PNG_BYTES)

            recovered, recovered_result = self.run_client(
                workspace, server, "last", auto_select=False,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertEqual(Path(recovered_result["images"][0]["path"]), output)
            self.assertEqual([request["path"] for request in server.requests],
                             ["/v1/images/generations"])

    def test_process_probe_does_not_stop_worker(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            self.assertTrue(client_module.process_is_running(process.pid))
            self.assertIsNone(process.poll())
            process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertFalse(client_module.process_is_running(process.pid))
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)

    def test_http_error_with_broken_body_remains_endpoint_failure(self) -> None:
        for read_error in (ConnectionResetError("connection reset"), http.client.IncompleteRead(b"")):
            with self.subTest(error=type(read_error).__name__):
                request = client_module.urllib.request.Request("http://127.0.0.1/v1/images/edits")
                error = urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO())
                client = client_module.HttpClient("http://127.0.0.1", None, 5)
                client.opener = mock.Mock()
                client.opener.open.side_effect = error
                with mock.patch.object(error, "read", side_effect=read_error):
                    with self.assertRaisesRegex(client_module.ClientError, "HTTP 404.*could not read error body"):
                        client._open(request)

    @unittest.skipUnless(os.name == "nt", "Windows scheduled-task fallback")
    def test_scheduled_task_supports_long_arguments_without_timer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            command = [sys.executable, "-X", "utf8", str(CLIENT), "_worker", "--job-dir",
                       str(Path(temporary) / ("long image path & \u56fe\u7247 " * 12))]
            self.assertGreater(len(subprocess.list2cmdline(command)), 261)
            definition_paths: list[Path] = []

            def register(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                if Path(argv[0]).name == "whoami.exe":
                    return subprocess.CompletedProcess(argv, 0, '"DOMAIN\\user","S-1-5-21-123-1001"', "")
                self.assertIn("/XML", argv)
                self.assertNotIn("/TR", argv)
                definition = Path(argv[argv.index("/XML") + 1])
                definition_paths.append(definition)
                root = ET.parse(definition).getroot()
                ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
                self.assertIsNone(root.find("t:Triggers", ns))
                self.assertEqual(root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns),
                                 subprocess.list2cmdline(command[1:]))
                self.assertEqual(root.findtext("t:Actions/t:Exec/t:WorkingDirectory", namespaces=ns),
                                 temporary)
                self.assertEqual(root.findtext("t:Principals/t:Principal/t:UserId", namespaces=ns),
                                 "S-1-5-21-123-1001")
                return subprocess.CompletedProcess(argv, 0, "created", "")

            with mock.patch.object(client_module.subprocess, "run", side_effect=register):
                task = client_module.create_windows_worker_task(command, temporary, "test-long-path")
            self.assertEqual(task, r"\KeylinkImage-test-long-path")
            self.assertTrue(definition_paths)
            self.assertFalse(definition_paths[0].exists())

    def test_background_start_rejects_high_resolution_before_spawning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            completed, result = self.run_client(
                Path(temporary), server, "start", "--prompt", "a 4K black hole",
                "--model", "gpt-image-2.5", "--size", "3840x2160",
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("high-resolution intent", result["error"])
            self.assertEqual(server.requests, [])

    def test_background_status_without_job_does_not_make_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            completed, result = self.run_client(
                Path(temporary), server, "status", auto_select=False,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("No background image job", result["error"])
            self.assertEqual(server.requests, [])

    @unittest.skipUnless(os.name == "nt", "Windows scheduled-task fallback")
    def test_windows_fallback_records_task_before_starting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            state_dir = workspace / ".keylink-image" / "threads" / "test-thread"
            token = client_module.save_model_selection(
                state_dir,
                [{"id": "gpt-image-2.5", "image_candidate": True,
                  "published_sizes": []}],
                "http://127.0.0.1:4321",
            )
            args = client_module.build_parser().parse_args([
                "start", "--prompt", "a 4K black hole", "--model", "gpt-image-2.5",
                "--selection-token", token, "--size", "3840x2160",
                "--confirm-high-res", "--base-url", "http://127.0.0.1:4321",
                "--thread-id", "test-thread",
            ])
            observed: dict[str, Any] = {}

            def start_scheduled(task_name: str, cwd: str) -> None:
                latest = json.loads((state_dir / "latest-job.json").read_text(encoding="utf-8"))
                job_path = state_dir / "jobs" / latest["job_id"] / "job.json"
                recorded = json.loads(job_path.read_text(encoding="utf-8"))
                observed.update(recorded)
                self.assertEqual(recorded["task_name"], task_name)
                self.assertEqual(recorded["launch_mode"], "scheduled-task")
                self.assertEqual(Path(cwd), workspace.resolve())

            output = io.StringIO()
            with mock.patch.object(client_module.Path, "cwd", return_value=workspace), \
                    mock.patch.object(client_module.subprocess, "Popen",
                                      side_effect=PermissionError("breakaway denied")), \
                    mock.patch.object(client_module, "create_windows_worker_task",
                                      return_value=r"\KeylinkImage-test"), \
                    mock.patch.object(client_module, "run_windows_worker_task",
                                      side_effect=start_scheduled) as run_task, \
                    contextlib.redirect_stdout(output):
                exit_code = client_module.command_start(args)
            self.assertEqual(exit_code, 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["launch_mode"], "scheduled-task")
            self.assertEqual(result["task_name"], r"\KeylinkImage-test")
            self.assertIsNone(result["pid"])
            self.assertTrue(observed)
            run_task.assert_called_once()

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

    def test_ccswitch_credentials_from_home_with_spaces_and_uri_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            user_home = Path(temporary) / "Mac User #1 %20"
            db_path = user_home / ".cc-switch" / "cc-switch.db"
            db_path.parent.mkdir(parents=True)
            connection = sqlite3.connect(db_path)
            try:
                connection.executescript("""
                    CREATE TABLE proxy_config (app_type TEXT, proxy_enabled INTEGER,
                        listen_address TEXT, listen_port INTEGER, enabled INTEGER);
                    CREATE TABLE providers (id TEXT, app_type TEXT, is_current INTEGER,
                        sort_index INTEGER, settings_config TEXT, website_url TEXT);
                    CREATE TABLE provider_endpoints (id INTEGER, app_type TEXT,
                        provider_id TEXT, url TEXT);
                """)
                settings = json.dumps({"auth": {"OPENAI_API_KEY": "fixture-key-only"}})
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    ("keylink", "codex", 1, 0, settings, "https://keylinkclub.com"),
                )
                connection.commit()
            finally:
                connection.close()
            original = db_path.read_bytes()
            with mock.patch.object(client_module.Path, "home", return_value=user_home):
                state = client_module.discover_ccswitch_state()
            self.assertEqual(state.api_key, "fixture-key-only")
            self.assertEqual(state.provider_base_urls, ["https://keylinkclub.com"])
            self.assertEqual(db_path.read_bytes(), original)

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
            installed = shutil.which("pwsh")
            if not installed:
                self.skipTest("PowerShell is unavailable")
            powershell = Path(installed)
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

    @unittest.skipIf(os.name == "nt", "POSIX launcher integration")
    def test_workspace_fallback_id_is_stable_through_directory_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "real-workspace"
            workspace.mkdir()
            alias = Path(temporary) / "workspace-alias"
            alias.symlink_to(workspace, target_is_directory=True)
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(client_module.Path, "cwd", return_value=workspace):
                    original_id = client_module.resolve_thread_id(None)
                with mock.patch.object(client_module.Path, "cwd", return_value=alias):
                    alias_id = client_module.resolve_thread_id(None)
            self.assertEqual(original_id, alias_id)

    @unittest.skipIf(os.name == "nt", "POSIX launcher integration")
    def test_shell_launcher_preserves_prompt_and_paths_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as server:
            workspace = Path(temporary)
            scripts = workspace / "Skill folder #1" / "scripts"
            scripts.mkdir(parents=True)
            for name in ("keylink_image.py", "keylink-image.sh"):
                shutil.copy2(CLIENT.with_name(name), scripts / name)
            env = os.environ.copy()
            env.update(KEYLINK_PYTHON=sys.executable, CODEX_THREAD_ID="shell-test",
                       NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
            for name in ("KEYLINK_API_KEY", "OPENAI_API_KEY", "KEYLINK_IMAGE_STATE_DIR"):
                env.pop(name, None)
            server.routes[("GET", "/v1/models")] = json_response(
                {"data": [{"id": "gpt-image-2"}]}
            )
            server.routes[("POST", "/v1/images/generations")] = json_response(
                {"data": [{"b64_json": PNG_B64}]}
            )
            launcher = ["sh", str(scripts / "keylink-image.sh")]
            base = ["--base-url", f"http://127.0.0.1:{server.server_address[1]}"]
            discovered = subprocess.run(launcher + ["models"] + base, cwd=workspace,
                                        env=env, capture_output=True, text=True,
                                        encoding="utf-8", timeout=15)
            self.assertEqual(discovered.returncode, 0, discovered.stdout + discovered.stderr)
            token = json.loads(discovered.stdout)["selection_token"]
            prompt = '海边的灯塔，保留 "蓝色" 天空与 $符号。'
            completed = subprocess.run(
                launcher + ["run", "--prompt", prompt, "--model", "gpt-image-2",
                            "--selection-token", token] + base,
                cwd=workspace, env=env, capture_output=True, text=True,
                encoding="utf-8", timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(json.loads(server.requests[-1]["body"])["prompt"], prompt)
            result = json.loads(completed.stdout)
            self.assertTrue(Path(result["images"][0]["path"]).is_file())

    @unittest.skipIf(os.name == "nt", "POSIX launcher integration")
    def test_shell_launcher_discovers_skill_virtual_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill_dir = Path(temporary) / "Skill with spaces"
            shutil.copytree(CLIENT.parent, skill_dir / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
            interpreter = skill_dir / ".venv" / "bin" / "python3"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text(
                "#!/bin/sh\nprintf '%s\\n' used-skill-venv >&2\nexec "
                + shlex.quote(sys.executable) + ' "$@"\n', encoding="utf-8",
            )
            interpreter.chmod(0o755)
            env = os.environ.copy()
            env.pop("KEYLINK_PYTHON", None)
            completed = subprocess.run(
                ["sh", str(skill_dir / "scripts" / "keylink-image.sh"), "--help"],
                cwd=temporary, env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("models", completed.stdout)
            self.assertIn("used-skill-venv", completed.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX launcher integration")
    def test_shell_launcher_rejects_invalid_explicit_python(self) -> None:
        env = dict(os.environ, KEYLINK_PYTHON="/nonexistent/keylink-python")
        completed = subprocess.run(
            ["sh", str(CLIENT.with_name("keylink-image.sh")), "--help"],
            env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("KEYLINK_PYTHON", completed.stderr)
        self.assertIn("3.11", completed.stderr)

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
