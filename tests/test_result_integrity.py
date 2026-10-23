"""Prevent stale/refused image results and bound duplicated Chat references."""
import base64
import contextlib
import errno
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import test_keylink_image as fixtures

c = fixtures.client_module
Image = fixtures.Image


class ResultIntegrityTests(unittest.TestCase):
    run_client = fixtures.KeylinkImageTests.run_client

    def test_host_network_permission_failure_releases_selection_for_exact_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            base = "http://127.0.0.1:4321"
            token = c.save_model_selection(state, [{"id": "gpt-image-2"}], base)
            args = c.build_parser().parse_args([
                "run", "--prompt", "a tree", "--model", "gpt-image-2",
                "--selection-token", token, "--base-url", base,
            ])
            denied = urllib.error.URLError(PermissionError(errno.EACCES, "access denied"))

            def raise_denied(*_args, **_kwargs):
                try:
                    raise denied
                except urllib.error.URLError as cause:
                    raise c.ClientError("Request failed for fixture: [WinError 10013] access denied") from cause

            first_output = io.StringIO()
            with mock.patch.object(c, "thread_state_dir", return_value=state), \
                 mock.patch.object(c, "discover_base_url", return_value=(base, "argument", c.CCSwitchState())), \
                 mock.patch.object(c, "discover_api_key", return_value=("fixture-key", "test")), \
                 mock.patch.object(c.HttpClient, "_open", side_effect=raise_denied), \
                 contextlib.redirect_stdout(first_output):
                self.assertEqual(c.command_run(args), 1)
            blocked = json.loads(first_output.getvalue())
            self.assertTrue(blocked["retryable_host_network_error"])
            self.assertTrue(blocked["selection_token_released"])
            self.assertIn("WinError 10013/EACCES", blocked["error"])
            self.assertIn("prepared_prompt", blocked)
            self.assertNotIn("sent_prompt", blocked)
            self.assertFalse(blocked["ask_user_to_switch_model"])
            c.validate_model_selection(state, token, "gpt-image-2", base)

            response = (json.dumps({"b64_json": fixtures.PNG_B64}).encode("utf-8"), "application/json")
            second_output = io.StringIO()
            with mock.patch.object(c, "thread_state_dir", return_value=state), \
                 mock.patch.object(c, "discover_base_url", return_value=(base, "argument", c.CCSwitchState())), \
                 mock.patch.object(c, "discover_api_key", return_value=("fixture-key", "test")), \
                 mock.patch.object(c.HttpClient, "_open", return_value=response), \
                 contextlib.redirect_stdout(second_output):
                self.assertEqual(c.command_run(args), 0)
            succeeded = json.loads(second_output.getvalue())
            self.assertEqual(succeeded["returned_count"], 1)
            self.assertEqual(succeeded["sent_prompt"], "a tree")

    def test_status_does_not_present_earlier_job_as_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            c.write_json_file(state / "latest-job.json", {"job_id": "old-job"})
            c.write_json_file(state / "jobs" / "old-job" / "job.json", {
                "job_id": "old-job", "status": "succeeded", "started_at": "2026-10-19T02:00:00Z",
            })
            c.write_json_file(state / "jobs" / "old-job" / "result.json", {
                "status": "ok", "request_id": "old", "images": [{"path": "old.png"}],
            })
            c.write_json_file(state / "latest-request.json", {
                "request_id": "new", "status": "failed", "started_at": "2026-10-19T03:00:00+00:00",
            })
            for options in ([], ["--job-id", "old-job"]):
                with self.subTest(options=options):
                    output = io.StringIO()
                    with mock.patch.object(c, "thread_state_dir", return_value=state), mock.patch("sys.stdout", output):
                        code = c.command_status(c.build_parser().parse_args(["status", *options]))
                    result = json.loads(output.getvalue())
                    self.assertFalse(result["current_request_succeeded"])
                    if not options:
                        self.assertEqual(code, 1)
                        self.assertEqual(result["images"], [])
                    else:
                        self.assertEqual(code, 0)
                        self.assertTrue(result["historical"])

    def test_earlier_completion_cannot_replace_newer_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            old = state / "old.png"
            new = state / "new.png"
            old.write_bytes(fixtures.PNG_BYTES)
            new.write_bytes(fixtures.PNG_BYTES)
            c.record_image_request(state, "old-token", "gpt-image-2")
            c.record_image_request(state, "new-token", "gpt-image-2")
            self.assertTrue(c.save_last_state(state, new, "gpt-image-2", "images-edits", "1024x1024",
                                             {"request_id": c.image_request_id("new-token")}))
            c.finish_image_request(state, "new-token", "succeeded")
            before = (state / "last.json").read_bytes()
            self.assertFalse(c.save_last_state(state, old, "gpt-image-2", "images-edits", "1024x1024",
                                              {"request_id": c.image_request_id("old-token")}))
            c.finish_image_request(state, "old-token", "succeeded")
            self.assertEqual((state / "last.json").read_bytes(), before)
            self.assertEqual(c.load_last_image(state), new.resolve())
            self.assertEqual(c.latest_image_request(state)["request_id"], c.image_request_id("new-token"))

    def test_legacy_timestamp_offsets_compare_as_instants(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            c.write_json_file(state / "latest-request.json", {"request_id": "old", "started_at": "2026-10-19T10:00:00+08:00"})
            c.write_json_file(state / "model-selection.json", {"token": "new", "used_at": "2026-10-19T03:00:00Z"})
            latest = c.latest_image_request(state)
            self.assertEqual(latest["request_id"], c.image_request_id("new"))
            self.assertFalse(c.matches_latest_request(latest, timestamp="2026-10-19T10:30:00+08:00"))
            self.assertTrue(c.matches_latest_request(latest, timestamp="2026-10-19T11:30:00+08:00"))

    def test_saved_image_state_error_does_not_trigger_another_generation(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp)
            base = f"http://127.0.0.1:{server.server_address[1]}"
            token = c.save_model_selection(root, [{"id": "gpt-image-2"}], base)
            server.routes[("POST", "/v1/images/generations")] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
            args = c.build_parser().parse_args(["run", "--prompt", "a tree", "--model", "gpt-image-2", "--selection-token", token, "--base-url", base])
            output = io.StringIO()
            with mock.patch.object(c, "thread_state_dir", return_value=root), \
                 mock.patch.object(c, "discover_base_url", return_value=(base, "argument", c.CCSwitchState())), \
                 mock.patch.object(c, "discover_api_key", return_value=("local-test-key", "fixture")), \
                 mock.patch.object(c, "save_last_state", side_effect=c.ClientError("state busy")), \
                 mock.patch("sys.stdout", output):
                self.assertEqual(c.command_run(args), 0)
            result = json.loads(output.getvalue())
            self.assertFalse(result["state_recorded"])
            self.assertTrue(Path(result["images"][0]["path"]).is_file())
            self.assertEqual([r["path"] for r in server.requests], ["/v1/images/generations"])

    def test_failed_request_cannot_recover_previous_image_as_current(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp)
            server.routes[("POST", "/v1/images/generations")] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
            done, original = self.run_client(root, server, "run", "--prompt", "a tree", "--model", "gpt-image-2")
            self.assertEqual(done.returncode, 0, done.stdout)
            state = root / ".keylink-image" / "threads" / "test-thread"
            before = (state / "last.json").read_bytes()
            for path in ("/v1/images/edits", "/v1/chat/completions"):
                server.routes[("POST", path)] = fixtures.json_response({"status_code": 413, "message": "Request body is too large."})
            failed, error = self.run_client(root, server, "run", "--prompt", "change only the sky",
                                            "--model", "gpt-image-2", "--use-last")
            self.assertEqual(failed.returncode, 1, failed.stdout)
            self.assertFalse(error["current_request_succeeded"])
            self.assertNotEqual(error["request_id"], original["request_id"])
            self.assertEqual([r["path"] for r in server.requests], ["/v1/images/edits", "/v1/chat/completions"])
            self.assertEqual((state / "last.json").read_bytes(), before)
            server.requests.clear()
            last, blocked = self.run_client(root, server, "last")
            self.assertEqual(last.returncode, 1)
            self.assertEqual(blocked["images"], [])
            self.assertEqual(blocked["latest_request"]["status"], "failed")
            self.assertTrue(blocked["previous_result_available"])
            old, historical = self.run_client(root, server, "last", "--allow-previous")
            self.assertEqual(old.returncode, 0, old.stdout)
            self.assertTrue(historical["historical"])
            self.assertFalse(historical["current_request_succeeded"])
            self.assertEqual(historical["images"], original["images"])
            self.assertEqual(server.requests, [])

    def test_running_request_blocks_last_until_its_result_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp)
            server.routes[("POST", "/v1/images/generations")] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
            done, _ = self.run_client(root, server, "run", "--prompt", "a tree", "--model", "gpt-image-2")
            self.assertEqual(done.returncode, 0, done.stdout)
            observed = []
            def respond(request):
                last, payload = self.run_client(root, server, "last")
                observed.append((last.returncode, payload))
                return fixtures.json_response({"b64_json": fixtures.PNG_B64})
            server.routes[("POST", "/v1/images/generations")] = respond
            done, generated = self.run_client(root, server, "run", "--mode", "generate", "--prompt", "a new tree", "--model", "gpt-image-2")
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertEqual(observed[0][0], 1)
            self.assertEqual(observed[0][1]["images"], [])
            self.assertEqual(observed[0][1]["latest_request"]["status"], "running")
            last, recovered = self.run_client(root, server, "last")
            self.assertEqual(last.returncode, 0, last.stdout)
            self.assertEqual(recovered["request_id"], generated["request_id"])
            self.assertFalse(recovered["historical"])

    def test_legacy_consumed_selection_rejects_older_saved_image(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp)
            original = root / "original.png"
            original.write_bytes(fixtures.PNG_BYTES)
            state = root / ".keylink-image" / "threads" / "test-thread"
            c.save_last_state(state, original, "gpt-image-2", "images-edits", "1024x1024")
            old = c.read_json_file(state / "last.json")
            old["saved_at"] = "2026-10-19T02:48:17+00:00"
            c.write_json_file(state / "last.json", old)
            c.write_json_file(state / "model-selection.json", {"token": "legacy", "used_at": "2026-10-19T03:01:28+00:00"})
            last, error = self.run_client(root, server, "last")
            self.assertEqual(last.returncode, 1)
            self.assertEqual(error["images"], [])
            self.assertEqual(error["latest_request"]["status"], "unknown")
            self.assertEqual(server.requests, [])

    def test_background_start_blocks_previous_image_before_worker_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            ref = Path(tmp) / "ref.png"
            ref.write_bytes(fixtures.PNG_BYTES)
            c.save_last_state(state, ref, "gpt-image-2", "images-generations", "1024x1024")
            base = "http://127.0.0.1:4321"
            token = c.save_model_selection(state, [{"id": "gpt-image-2"}], base)
            args = c.build_parser().parse_args(["start", "--prompt", "change sky", "--image", str(ref),
                                               "--model", "gpt-image-2", "--selection-token", token, "--base-url", base])
            output = io.StringIO()
            with mock.patch.object(c, "thread_state_dir", return_value=state), \
                 mock.patch.object(c, "discover_base_url", return_value=(base, "argument", c.CCSwitchState())), \
                 mock.patch.object(c.subprocess, "Popen", return_value=mock.Mock(pid=123)), \
                 mock.patch("sys.stdout", output):
                self.assertEqual(c.command_start(args), 0)
                started = json.loads(output.getvalue())
                output.seek(0)
                output.truncate()
                self.assertEqual(c.command_last(c.build_parser().parse_args(["last"])), 1)
                self.assertEqual(json.loads(output.getvalue())["latest_request"]["status"], "running")
                job_path = state / "jobs" / started["job_id"] / "job.json"
                job = c.read_json_file(job_path)
                job["status"] = "failed"
                c.write_json_file(job_path, job)
                output.seek(0)
                output.truncate()
                self.assertEqual(c.command_last(c.build_parser().parse_args(["last"])), 1)
            self.assertEqual(json.loads(output.getvalue())["images"], [])
            self.assertEqual(json.loads(output.getvalue())["latest_request"]["status"], "failed")

    def test_worker_launch_failure_releases_selection_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            base = "http://127.0.0.1:4321"
            token = c.save_model_selection(state, [{"id": "gpt-image-2"}], base)
            argv = ["start", "--prompt", "a tree", "--model", "gpt-image-2", "--selection-token", token, "--base-url", base]
            with mock.patch.object(c, "thread_state_dir", return_value=state), \
                 mock.patch.object(c, "discover_base_url", return_value=(base, "argument", c.CCSwitchState())), \
                 mock.patch.object(c.subprocess, "Popen", side_effect=PermissionError("process unavailable")), \
                 mock.patch.object(c, "create_windows_worker_task", side_effect=OSError("scheduler unavailable")):
                with self.assertRaises(c.ClientError):
                    c.command_start(c.build_parser().parse_args(argv))
            self.assertEqual(c.latest_image_request(state)["status"], "failed")
            self.assertFalse(list((state / "jobs").glob("*.claim")))
            self.assertFalse(c.read_json_file(state / "model-selection.json").get("used_at"))
            with mock.patch.object(c, "thread_state_dir", return_value=state), \
                 mock.patch.object(c, "discover_base_url", return_value=(base, "argument", c.CCSwitchState())), \
                 mock.patch.object(c.subprocess, "Popen", return_value=mock.Mock(pid=123)), \
                 mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(c.command_start(c.build_parser().parse_args(argv)), 0)
            self.assertEqual(c.latest_image_request(state)["status"], "running")
            self.assertNotIn("finished_at", c.latest_image_request(state))

    def test_structured_refusals_never_become_success_or_trigger_fallback(self):
        responses = [
            ({"choices": [{"finish_reason": "content_filter", "message": {"content": f"![original](data:image/png;base64,{fixtures.PNG_B64})"}}]}, 200),
            ({"choices": [{"message": {"refusal": "Cannot comply", "images": [{"b64_json": fixtures.PNG_B64}]}}]}, 200),
            ({"promptFeedback": {"blockReason": "SAFETY"}, "data": [{"b64_json": fixtures.PNG_B64}]}, 200),
            ({"error": {"code": "content_policy_violation", "message": "Request rejected"}}, 400),
            ({"error": {"message": "Request rejected " + "x" * 1500, "code": "content_policy_violation"}}, 400),
            ({"choices": [{"message": {"content": json.dumps({"refusal": "blocked", "data": [{"b64_json": fixtures.PNG_B64}]})}}]}, 200),
            ({"choices": [{"message": {"content": "```json\n" + json.dumps({"refusal": "blocked", "data": [{"b64_json": fixtures.PNG_B64}]}) + "\n```"}}]}, 200),
        ]
        for response, status in responses:
            with self.subTest(status=status, keys=list(response)), tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
                root = Path(tmp)
                server.routes[("POST", "/v1/images/generations")] = fixtures.json_response(response, status)
                server.routes[("POST", "/v1/chat/completions")] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
                done, error = self.run_client(root, server, "run", "--prompt", "a test prompt", "--model", "gpt-image-2")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertTrue(error["provider_refused"])
                self.assertFalse(error["ask_user_to_switch_model"])
                self.assertNotIn("images", error)
                self.assertEqual([r["path"] for r in server.requests], ["/v1/images/generations"])
                self.assertFalse((root / ".keylink-image" / "threads" / "test-thread" / "last.json").exists())

    def test_chat_nested_api_error_is_not_a_generated_image(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            response = {"choices": [{"message": {"content": json.dumps({
                "status_code": 413, "message": "Request body is too large.",
                "data": [{"b64_json": fixtures.PNG_B64}],
            })}}]}
            server.routes[("POST", "/v1/chat/completions")] = fixtures.json_response(response)
            done, error = self.run_client(Path(tmp), server, "run", "--prompt", "a tree", "--model", "gpt-image-2", "--endpoint", "chat")
            self.assertEqual(done.returncode, 1, done.stdout)
            self.assertIn("413", error["attempts"][0]["error"])
            self.assertFalse(error["provider_refused"])
            self.assertNotIn("images", error)

    def test_echoed_user_reference_is_not_an_output_image(self):
        data = f"data:image/png;base64,{fixtures.PNG_B64}"
        response = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data}}]}]}
        self.assertEqual(c.extract_image_candidates(response), [])
        response["messages"].append({"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": data}}]})
        self.assertEqual(len(c.extract_image_candidates(response)), 1)

    @unittest.skipIf(Image is None, "Pillow required")
    def test_megabyte_reference_compresses_before_both_endpoint_requests(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp)
            ref = root / "photo.png"
            Image.frombytes("RGB", (1024, 1536), os.urandom(1024 * 1536 * 3)).save(ref)
            before = ref.read_bytes()
            self.assertGreater(len(before), 1024 * 1024)
            server.routes[("POST", "/v1/images/edits")] = fixtures.json_response({"error": "unsupported"}, 400)
            server.routes[("POST", "/v1/chat/completions")] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
            done, result = self.run_client(root, server, "run", "--prompt", "change only the sky", "--model", "gpt-image-2.5-flare",
                                           "--image", str(ref), "--size", "3840x2160", "--confirm-high-res")
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertEqual(ref.read_bytes(), before)
            self.assertEqual(result["requested_size"], "3840x2160")
            self.assertLessEqual(result["reference_images"][0]["prepared_bytes"], 1024 * 1024)
            self.assertTrue(result["reference_images"][0]["normalized"])
            self.assertLess(len(server.requests[0]["body"]), 1100000)
            self.assertLess(len(server.requests[1]["body"]), 2900000)
            chat = json.loads(server.requests[1]["body"])
            self.assertEqual(chat["images"][0]["image_url"], chat["messages"][0]["content"][1]["image_url"]["url"])
            self.assertEqual(base64.b64decode(chat["images"][0]["image_url"].split(",")[1]),
                             Path(result["reference_images"][0]["request_path"]).read_bytes())


if __name__ == "__main__":
    unittest.main()
