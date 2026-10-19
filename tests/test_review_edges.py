"""Failure injection for interrupted jobs and malformed provider responses."""
import base64
import concurrent.futures
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import test_keylink_image as fixtures

c = fixtures.client_module


class ReviewEdgeTests(unittest.TestCase):
    run_client = fixtures.KeylinkImageTests.run_client

    def test_abandoned_selection_lock_does_not_block_unused_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            base = "http://127.0.0.1:4321"
            token = c.save_model_selection(state, [{"id": "gpt-image-2"}], base)
            (state / ".model-selection.lock").touch()
            # A killed process can leave the file, but cannot retain an OS lock.
            c.consume_model_selection(state, token, "gpt-image-2", base)
            with self.assertRaisesRegex(c.ClientError, "already been used"):
                c.consume_model_selection(state, token, "gpt-image-2", base)

    def test_concurrent_token_consumption_only_allows_one_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            base = "http://127.0.0.1:4321"
            token = c.save_model_selection(state, [{"id": "gpt-image-2"}], base)
            barrier = threading.Barrier(2)

            def consume():
                barrier.wait(timeout=5)
                try:
                    c.consume_model_selection(state, token, "gpt-image-2", base)
                    return "consumed"
                except c.ClientError as error:
                    return str(error)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: consume(), range(2)))
            self.assertEqual(results.count("consumed"), 1, results)
            self.assertTrue(any("already been used" in result for result in results), results)
            self.assertEqual(c.latest_image_request(state)["request_id"], c.image_request_id(token))

    def test_start_metadata_failure_releases_unsubmitted_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            base = "http://127.0.0.1:4321"
            token = c.save_model_selection(state, [{"id": "gpt-image-2"}], base)
            args = c.build_parser().parse_args([
                "start", "--prompt", "a tree", "--model", "gpt-image-2",
                "--selection-token", token, "--base-url", base,
            ])
            write = c.write_json_file

            def fail_request_metadata(path, payload):
                if path.name == "request.json":
                    raise OSError("metadata storage unavailable")
                return write(path, payload)

            with mock.patch.object(c, "thread_state_dir", return_value=state), \
                 mock.patch.object(c, "discover_base_url", return_value=(base, "argument", c.CCSwitchState())), \
                 mock.patch.object(c, "write_json_file", side_effect=fail_request_metadata), \
                 mock.patch.object(c.subprocess, "Popen") as launch:
                with self.assertRaises((c.ClientError, OSError)):
                    c.command_start(args)
                launch.assert_not_called()
            self.assertFalse(list((state / "jobs").glob("*.claim")))
            c.validate_model_selection(state, token, "gpt-image-2", base)
            with mock.patch.object(c, "thread_state_dir", return_value=state), \
                 mock.patch.object(c, "discover_base_url", return_value=(base, "argument", c.CCSwitchState())), \
                 mock.patch.object(c.subprocess, "Popen", return_value=mock.Mock(pid=123)), \
                 mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(c.command_start(args), 0)

    def test_scheduler_cleanup_failure_preserves_completed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            c.write_json_file(directory / "request.json", {"argv": [], "cwd": str(directory)})
            c.write_json_file(directory / "job.json", {"job_id": "fixture", "task_name": "fixture-task"})
            image = directory / "generated.png"
            image.write_bytes(fixtures.PNG_BYTES)
            payload = {"status": "ok", "images": [{"path": str(image)}]}

            def generate(*args, **kwargs):
                c.emit(payload)
                return 0

            cwd = Path.cwd()
            try:
                with mock.patch.object(c, "main", side_effect=generate) as main, \
                     mock.patch.object(c, "delete_windows_task", side_effect=OSError("scheduler unavailable")):
                    self.assertEqual(c.command_worker(c.build_parser().parse_args([
                        "_worker", "--job-dir", str(directory),
                    ])), 0)
                    main.assert_called_once()
            finally:
                os.chdir(cwd)
            self.assertEqual(c.read_json_file(directory / "result.json")["images"], payload["images"])
            self.assertEqual(c.read_json_file(directory / "job.json")["status"], "succeeded")

    def test_nested_error_envelopes_cannot_publish_returned_reference(self):
        error = {"status_code": 413, "message": "Request body is too large.",
                 "data": [{"b64_json": fixtures.PNG_B64}]}
        responses = [
            {"data": error},
            {"data": [error]},
            {"choices": [{"message": {"role": "assistant", "content": error}}]},
        ]
        for response in responses:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
                server.routes[("POST", "/v1/images/generations")] = fixtures.json_response(response)
                server.routes[("POST", "/v1/chat/completions")] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
                done, result = self.run_client(Path(tmp), server, "run", "--prompt", "a tree", "--model", "gpt-image-2")
                self.assertEqual(done.returncode, 0, done.stdout)
                self.assertEqual(result["endpoint"], "chat")
                self.assertIn("413", result["failed_attempts"][0]["error"])

    @unittest.skipIf(fixtures.Image is None, "Pillow required")
    def test_truncated_image_falls_back_instead_of_reporting_success(self):
        # Includes PNG signature and valid dimensions but no pixel data.
        damaged = base64.b64encode(fixtures.PNG_BYTES[:33]).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            server.routes[("POST", "/v1/images/generations")] = fixtures.json_response({"b64_json": damaged})
            server.routes[("POST", "/v1/chat/completions")] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
            done, result = self.run_client(Path(tmp), server, "run", "--prompt", "a tree", "--model", "gpt-image-2")
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertEqual(result["endpoint"], "chat")
            self.assertEqual(result["failed_attempts"][0]["phase"], "image_download_or_decode")
            self.assertEqual(Path(result["images"][0]["path"]).read_bytes(), fixtures.PNG_BYTES)
            self.assertFalse(list(Path(tmp).rglob("*-images-generations-*.png")))

    def test_missing_pillow_still_rejects_truncated_image(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(fixtures.sys.modules, {"PIL": None}):
            root = Path(tmp)
            api = c.HttpClient("http://127.0.0.1:4321", "fixture", 1)
            damaged = base64.b64encode(fixtures.PNG_BYTES[:33]).decode("ascii")
            with self.assertRaisesRegex(c.ClientError, "truncated"):
                c.save_candidates([c.ImageCandidate("base64", damaged)], api, root, "gpt-image-2", "images-generations")
            saved, errors = c.save_candidates([c.ImageCandidate("base64", fixtures.PNG_B64)], api, root, "gpt-image-2", "images-generations")
            self.assertFalse(errors)
            self.assertEqual(Path(saved[0]["path"]).read_bytes(), fixtures.PNG_BYTES)


if __name__ == "__main__":
    unittest.main()
