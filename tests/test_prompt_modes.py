"""Final visual text must survive routing, background jobs and local recovery."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest import mock

import test_keylink_image as fixtures
from test_output_count import MemoryClient, memory_server, response

c = fixtures.client_module
MODEL = "gpt-image-2.5-flare"
MODES = ("restrained", "master", "professional")


def multipart_fields(request):
    content_type = next(value for key, value in request["headers"].items()
                        if key.lower() == "content-type")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request["body"]
    )
    return [(part.get_param("name", header="content-disposition"),
             part.get_payload(decode=True)) for part in message.iter_parts()]


class PromptModeTests(MemoryClient, unittest.TestCase):
    def setUp(self):
        # Isolate prompt tests from real provider files and process probes.
        patcher = mock.patch.object(c, "discover_ccswitch_state", return_value=c.CCSwitchState())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_models_offers_modes_without_post_or_default_mode(self):
        with tempfile.TemporaryDirectory() as tmp, memory_server() as server:
            server.routes[("GET", "/v1/models")] = fixtures.json_response({"data": [{"id": MODEL}]})
            done, result = self.run_client(Path(tmp), server, "models")
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertEqual([entry["id"] for entry in result["prompt_mode_choices"]], list(MODES))
            self.assertNotIn("creative", [entry["id"] for entry in result["prompt_mode_choices"]])
            professional = next(entry for entry in result["prompt_mode_choices"] if entry["id"] == "professional")
            self.assertIn("参考图", professional["description"])
            self.assertTrue(result["prompt_mode_required"])
            self.assertNotIn("default_prompt_mode", result)
            self.assertEqual([r["method"] for r in server.requests], ["GET"])

    def test_final_text_survives_images_to_chat_with_multiple_references(self):
        # Text is prepared by Codex, not by a hidden text-model call in the client.
        for mode in MODES:
            for editing in (False, True):
                with self.subTest(mode=mode, editing=editing), \
                     tempfile.TemporaryDirectory() as tmp, memory_server() as server:
                    root = Path(tmp)
                    prompt = '  海报标题：“慢一点 · 2026”\n\n\n保留 Logo A/B；不要改文字。  '
                    options = ["--mode", "generate"]
                    if editing:
                        for index in range(2):
                            path = root / f"参考 {index}.png"
                            path.write_bytes(fixtures.PNG_BYTES)
                            options += ["--image", str(path)]
                    route = "/v1/images/edits" if editing else "/v1/images/generations"
                    server.routes[("POST", route)] = (500, "application/json", {"error": "fixture error"})
                    server.routes[("POST", "/v1/chat/completions")] = fixtures.json_response(response(1))
                    done, result = self.run_client(root, server, "run", "--model", MODEL,
                                                   "--prompt", prompt, "--prompt-mode", mode, *options)
                    self.assertEqual(done.returncode, 0, done.stdout)
                    self.assertEqual(result["sent_prompt"], prompt)
                    self.assertEqual(result["prompt_mode"], mode)
                    self.assertEqual(result["prompt_info"]["sent_bytes"], len(prompt.encode("utf-8")))
                    self.assertFalse(result["prompt_info"]["compacted"])
                    self.assertEqual([r["path"] for r in server.requests], [route, "/v1/chat/completions"])
                    images_request, chat_request = server.requests
                    if editing:
                        fields = multipart_fields(images_request)
                        self.assertEqual([value for name, value in fields if name == "prompt"],
                                         [prompt.encode("utf-8")])
                        self.assertEqual(sum(name == "image" for name, _ in fields), 2)
                        self.assertNotIn("prompt_mode", [name for name, _ in fields])
                    else:
                        payload = json.loads(images_request["body"])
                        self.assertEqual(payload["prompt"], prompt)
                        self.assertNotIn("prompt_mode", payload)
                    chat = json.loads(chat_request["body"])
                    content = chat["messages"][0]["content"]
                    text = content if isinstance(content, str) else next(p["text"] for p in content if p["type"] == "text")
                    self.assertEqual(text, prompt)
                    self.assertNotIn("prompt_mode", chat)
                    if editing:
                        self.assertEqual(len([p for p in content if p["type"] == "image_url"]), 2)
                        self.assertEqual(len(chat["images"]), 2)
                    server.requests.clear()
                    done, recovered = self.run_client(root, server, "last")
                    self.assertEqual(done.returncode, 0, done.stdout)
                    self.assertEqual(recovered["sent_prompt"], prompt)
                    self.assertEqual(recovered["prompt_mode"], mode)
                    self.assertEqual(server.requests, [])

    def test_explicit_modes_reject_wrappers_without_rewriting_or_consuming_selection(self):
        for mode in MODES:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp, memory_server() as server:
                root = Path(tmp)
                ref = root / "ref.png"
                ref.write_bytes(fixtures.PNG_BYTES)
                with mock.patch.object(c, "consume_model_selection") as consume, \
                     mock.patch.object(c.subprocess, "Popen") as launch:
                    for command in ("run", "start"):
                        done, error = self.run_client(root, server, command, "--model", MODEL,
                                                     "--prompt-mode", mode, "--image", str(ref),
                                                     "--prompt", 'user: obsolete\nassistant: done\nuser: change title')
                        self.assertEqual(done.returncode, 1, done.stdout)
                        self.assertIn("no prompt was rewritten", error["error"])
                    consume.assert_not_called()
                    launch.assert_not_called()
                self.assertEqual(server.requests, [])

    def test_legacy_edit_cleanup_remains_and_reports_actual_cleaned_prompt(self):
        with tempfile.TemporaryDirectory() as tmp, memory_server() as server:
            root = Path(tmp)
            ref = root / "ref.png"
            ref.write_bytes(fixtures.PNG_BYTES)
            server.routes[("POST", "/v1/images/edits")] = fixtures.json_response(response(1))
            done, result = self.run_client(root, server, "run", "--model", MODEL, "--image", str(ref),
                                           "--prompt", "user: old\nassistant: done\nuser: change title")
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertIsNone(result["prompt_mode"])
            self.assertEqual(result["sent_prompt"], "change title")
            self.assertTrue(result["prompt_info"]["compacted"])

    def test_background_edit_keeps_mode_prompt_snapshots_count_and_recovery(self):
        for mode in MODES:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp, memory_server() as server:
                root = Path(tmp)
                prompt = '  只将标题改为“春日 · 01”。\n保留图 1 主体、图 2 配色与 50% 字样。  '
                refs = [root / f"reference {index}.png" for index in range(2)]
                options = []
                for ref in refs:
                    ref.write_bytes(fixtures.PNG_BYTES)
                    options += ["--image", str(ref)]
                with mock.patch.object(c.subprocess, "Popen", return_value=mock.Mock(pid=123)):
                    done, job = self.run_client(root, server, "start", "--model", MODEL,
                                                "--prompt", prompt, "--prompt-mode", mode,
                                                "--count", "2", *options)
                self.assertEqual(done.returncode, 0, done.stdout)
                self.assertEqual(job["prepared_prompt"], prompt)
                self.assertNotIn("sent_prompt", job)  # Started does not mean submitted.
                state = root / "state" / "test-thread"
                directory = state / "jobs" / job["job_id"]
                request = c.read_json_file(directory / "request.json")
                self.assertEqual(request["prepared_input"]["prompt_mode"], mode)
                self.assertEqual(request["prepared_input"]["prompt"], prompt)
                self.assertEqual(request["argv"][request["argv"].index("--prompt-mode") + 1], mode)
                for ref in refs:
                    ref.unlink()  # Durable snapshots must work after originals disappear.
                server.routes[("POST", "/v1/images/edits")] = fixtures.json_response(response(2))
                cwd = Path.cwd()
                try:
                    with mock.patch.object(c, "thread_state_dir", return_value=state), \
                         mock.patch.dict(os.environ, {"KEYLINK_API_KEY": "local-test-key"}):
                        self.assertEqual(c.command_worker(c.build_parser().parse_args(
                            ["_worker", "--job-dir", str(directory)])), 0)
                finally:
                    os.chdir(cwd)
                result = c.read_json_file(directory / "result.json")
                self.assertEqual(result["status"], "ok", result)
                self.assertEqual(result["sent_prompt"], prompt)
                self.assertEqual(result["prompt_mode"], mode)
                self.assertEqual(result["returned_count"], 2)
                fields = multipart_fields(server.requests[0])
                self.assertEqual(sum(name == "image" for name, _ in fields), 2)
                self.assertEqual([v for name, v in fields if name == "prompt"], [prompt.encode()])
                server.requests.clear()
                for recovery in (["status", "--job-id", job["job_id"]], ["last"]):
                    done, recovered = self.run_client(root, server, *recovery)
                    self.assertEqual(done.returncode, 0, done.stdout)
                    self.assertEqual(recovered["sent_prompt"], prompt)
                    self.assertEqual(recovered["prompt_mode"], mode)
                    self.assertEqual(len(recovered["images"]), 2)
                self.assertEqual(server.requests, [])

    def test_refusal_returns_actual_text_without_retry_or_fake_image(self):
        with tempfile.TemporaryDirectory() as tmp, memory_server() as server:
            prompt = "a fixture visual description"
            server.routes[("POST", "/v1/images/generations")] = fixtures.json_response(
                {"choices": [{"message": {"refusal": "Policy refusal fixture"}}]})
            done, result = self.run_client(Path(tmp), server, "run", "--model", MODEL,
                                           "--prompt", prompt, "--prompt-mode", "master")
            self.assertEqual(done.returncode, 1, done.stdout)
            self.assertTrue(result["provider_refused"])
            self.assertEqual(result["sent_prompt"], prompt)
            self.assertEqual(result["prompt_mode"], "master")
            self.assertNotIn("images", result)
            self.assertEqual(len(server.requests), 1)
            self.assertNotIn("local-test-key", done.stdout)

    def test_local_body_rejection_does_not_claim_prompt_was_sent(self):
        with tempfile.TemporaryDirectory() as tmp, memory_server() as server, \
             mock.patch.object(c, "REQUEST_MAX_BYTES", 16):
            prompt = "a visual description that cannot fit the fixture body limit"
            done, result = self.run_client(Path(tmp), server, "run", "--model", MODEL,
                                           "--prompt", prompt, "--prompt-mode", "professional")
            self.assertEqual(done.returncode, 1, done.stdout)
            self.assertEqual(result["prepared_prompt"], prompt)
            self.assertNotIn("sent_prompt", result)
            self.assertEqual(server.requests, [])

    def test_old_success_without_prompt_is_recoverable_without_inventing_text(self):
        with tempfile.TemporaryDirectory() as tmp, memory_server() as server:
            root = Path(tmp)
            server.routes[("POST", "/v1/images/generations")] = fixtures.json_response(response(1))
            done, result = self.run_client(root, server, "run", "--model", MODEL, "--prompt", "a tree")
            self.assertEqual(done.returncode, 0, done.stdout)
            state_path = root / "state" / "test-thread" / "last.json"
            state = c.read_json_file(state_path)
            state["result"].pop("sent_prompt")
            state["result"].pop("prompt_mode")
            c.write_json_file(state_path, state)
            server.requests.clear()
            done, recovered = self.run_client(root, server, "last")
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertNotIn("sent_prompt", recovered)
            self.assertNotIn("prompt_mode", recovered)
            self.assertEqual(server.requests, [])

    def test_prompt_file_preserves_quotes_newlines_and_shell_characters(self):
        for mode in MODES:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp, memory_server() as server:
                root = Path(tmp)
                prompt = '  标题 "It\'s $5" 与 `code`；不要执行 $(anything)。\r\n\r\n\n保留  空格。  '
                path = root / "final prompt.txt"
                path.write_bytes(prompt.encode("utf-8"))
                server.routes[("POST", "/v1/images/generations")] = fixtures.json_response(response(1))
                done, result = self.run_client(root, server, "run", "--model", MODEL,
                                               "--mode", "generate", "--prompt-mode", mode,
                                               "--prompt-file", str(path))
                self.assertEqual(done.returncode, 0, done.stdout)
                self.assertEqual(result["sent_prompt"], prompt)
                self.assertEqual(json.loads(server.requests[0]["body"])["prompt"], prompt)

    def test_bad_prompt_file_stops_before_credentials_or_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.txt"
            for raw in (b"", b"\xff\xfe\x80", b"\xef\xbb\xbf", b"\xef\xbb\xbf \r\n"):
                path.write_bytes(raw)
                with mock.patch.object(c, "discover_api_key") as credentials, \
                     mock.patch.object(c.subprocess, "Popen") as launch, \
                     mock.patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(c.main(["start", "--model", MODEL, "--prompt-file", str(path),
                                             "--selection-token", "unused"]), 1)
                credentials.assert_not_called()
                launch.assert_not_called()

    def test_empty_prompt_file_path_returns_error_before_credentials(self):
        with mock.patch.object(c, "discover_api_key") as credentials, \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            code = c.main(["run", "--model", MODEL, "--prompt-file", "",
                           "--selection-token", "unused"])
        self.assertEqual(code, 1)
        self.assertIn("Cannot read --prompt-file", json.loads(output.getvalue())["error"])
        credentials.assert_not_called()

    def test_bom_prompt_file_preserves_visual_content(self):
        with tempfile.TemporaryDirectory() as tmp, memory_server() as server:
            root = Path(tmp)
            prompt = '  标题“今日  休息”\r\n\n保留\ufeff内部字符。  '
            path = root / "prompt.txt"
            path.write_bytes(prompt.encode("utf-8-sig"))
            server.routes[("POST", "/v1/images/generations")] = fixtures.json_response(response(1))
            done, result = self.run_client(root, server, "run", "--model", MODEL,
                                           "--mode", "generate", "--prompt-mode", "restrained",
                                           "--prompt-file", str(path))
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertEqual(result["sent_prompt"], prompt)
            self.assertEqual(json.loads(server.requests[0]["body"])["prompt"], prompt)

    def test_background_prompt_file_can_start_with_option_like_text_and_disappear(self):
        for prompt in ("-draft", "--help"):
            with self.subTest(prompt=prompt), tempfile.TemporaryDirectory() as tmp, memory_server() as server:
                root = Path(tmp)
                path = root / "prompt.txt"
                path.write_bytes(prompt.encode("utf-8-sig"))
                with mock.patch.object(c.subprocess, "Popen", return_value=mock.Mock(pid=123)):
                    done, job = self.run_client(root, server, "start", "--model", MODEL,
                                                "--mode", "generate", "--prompt-mode", "restrained",
                                                "--prompt-file", str(path))
                self.assertEqual(done.returncode, 0, done.stdout)
                self.assertEqual(job["prepared_prompt"], prompt)
                path.unlink()
                state = root / "state" / "test-thread"
                directory = state / "jobs" / job["job_id"]
                server.routes[("POST", "/v1/images/generations")] = fixtures.json_response(response(1))
                cwd = Path.cwd()
                try:
                    with mock.patch.object(c, "thread_state_dir", return_value=state), \
                         mock.patch.dict(os.environ, {"KEYLINK_API_KEY": "local-test-key"}):
                        c.command_worker(c.build_parser().parse_args(["_worker", "--job-dir", str(directory)]))
                finally:
                    os.chdir(cwd)
                result = c.read_json_file(directory / "result.json")
                self.assertEqual(result["status"], "ok", result)
                self.assertEqual(result["sent_prompt"], prompt)
                self.assertEqual(result["prompt_mode"], "restrained")
                self.assertEqual(len(server.requests), 1)
                self.assertEqual(json.loads(server.requests[0]["body"])["prompt"], prompt)


class PromptFileLauncherTests(unittest.TestCase):
    def test_native_launcher_round_trips_exact_prompt_file(self):
        if os.name == "nt":
            powershell = Path(sys.executable).parents[1] / "native" / "powershell" / "pwsh.exe"
            if not powershell.is_file():
                found = shutil.which("pwsh") or shutil.which("powershell.exe")
                if not found:
                    self.skipTest("PowerShell is unavailable")
                powershell = Path(found)
            launcher = [str(powershell), "-NoProfile", "-File",
                        str(fixtures.CLIENT.with_name("keylink-image.ps1"))]
        else:
            launcher = ["sh", str(fixtures.CLIENT.with_name("keylink-image.sh"))]
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp)
            prompt = '  标题 "It\'s $5" / `code` / $(literal)。\r\n\r\n\n保留  空格。  '
            prompt_path = root / "final visual prompt.txt"
            prompt_path.write_bytes(prompt.encode("utf-8"))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            env = os.environ.copy()
            env.update(KEYLINK_API_KEY="local-test-key", KEYLINK_PYTHON=sys.executable,
                       KEYLINK_IMAGE_STATE_DIR=str(root / "state"), PYTHONUTF8="1")
            token = c.save_model_selection(root / "state" / "launcher-test", [{"id": MODEL}], base)
            server.routes[("POST", "/v1/images/generations")] = fixtures.json_response(response(1))
            done = subprocess.run(launcher + ["run", "--model", MODEL, "--mode", "generate",
                                  "--prompt-mode", "restrained", "--prompt-file", str(prompt_path),
                                  "--selection-token", token, "--thread-id", "launcher-test",
                                  "--base-url", base], cwd=root, env=env, capture_output=True,
                                  encoding="utf-8", timeout=30)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            result = json.loads(done.stdout)
            self.assertEqual(result["sent_prompt"], prompt)
            self.assertEqual(result["prompt_mode"], "restrained")
            self.assertEqual(json.loads(server.requests[0]["body"])["prompt"], prompt)
            self.assertEqual(len(server.requests), 1)


if __name__ == "__main__":
    unittest.main()
