"""Bound output counts and retain all current-request files without resubmission."""
import base64
import contextlib
import subprocess
import urllib.parse
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_keylink_image as fixtures

c = fixtures.client_module
MODEL = "gpt-image-2.5-sunburst"
IMAGES = [base64.b64encode(fixtures.png_image(1024 + i, 1024)).decode("ascii") for i in range(5)]


def response(count):
    return {"data": [{"b64_json": value} for value in IMAGES[:count]]}



class MemoryServer:
    """Exercise real request serialization and parsing without network access."""
    server_address = ("127.0.0.1", 43210)

    def __init__(self):
        self.routes = {}
        self.requests = []

    def open(self, request, **kwargs):
        item = {"method": request.get_method(), "path": urllib.parse.urlsplit(request.full_url).path,
                "body": request.data or b"", "headers": dict(request.header_items())}
        self.requests.append(item)
        route = self.routes.get((item["method"], item["path"]))
        if route is None:
            raise c.APIError("HTTP 404: missing fixture route", 404)
        status, content_type, payload = route(item) if callable(route) else route
        raw = json.dumps(payload).encode() if isinstance(payload, (dict, list)) else payload
        if isinstance(raw, str):
            raw = raw.encode()
        if status >= 400:
            try:
                c.check_provider_refusal(json.loads(raw))
            except ValueError:
                pass
            raise c.APIError(f"HTTP {status}: {raw.decode()}", status)
        return raw, content_type


@contextlib.contextmanager
def memory_server():
    server = MemoryServer()
    with mock.patch.object(c.HttpClient, "_open", side_effect=server.open):
        yield server


class MemoryClient:
    def run_client(self, workspace, server, *args, thread_id="test-thread"):
        base = f"http://127.0.0.1:{server.server_address[1]}"
        state = workspace / "state" / thread_id
        effective = list(args)
        with mock.patch.object(c, "thread_state_dir", return_value=state), \
             mock.patch.dict(os.environ, {"KEYLINK_API_KEY": "local-test-key"}):
            if args[0] in {"run", "start"}:
                model = effective[effective.index("--model") + 1]
                effective += ["--selection-token", c.save_model_selection(state, [{"id": model}], base)]
            if args[0] in {"models", "run", "start"}:
                effective += ["--base-url", base]
            with mock.patch("sys.stdout", io.StringIO()) as output:
                code = c.main(effective)
        text = output.getvalue()
        return subprocess.CompletedProcess(effective, code, text, ""), json.loads(text)


class OutputCountTests(MemoryClient, unittest.TestCase):

    def test_models_exposes_count_choices_without_post(self):
        with tempfile.TemporaryDirectory() as tmp, memory_server() as server:
            server.routes[("GET", "/v1/models")] = fixtures.json_response({"data": [{"id": MODEL}]})
            done, result = self.run_client(Path(tmp), server, "models")
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertEqual(result["output_count_choices"], [1, 2, 3, 4])
            self.assertEqual(result["default_output_count"], 1)
            self.assertEqual(result["max_output_count"], 4)
            self.assertEqual([r["method"] for r in server.requests], ["GET"])

    def test_invalid_counts_stop_before_credentials_token_use_or_job_launch(self):
        for command in ("run", "start"):
            for count in ("0", "-1", "5", "999"):
                with self.subTest(command=command, count=count):
                    with mock.patch.object(c, "discover_api_key") as credentials, \
                         mock.patch.object(c, "consume_model_selection") as consume, \
                         mock.patch.object(c.subprocess, "Popen") as launch, \
                         mock.patch("sys.stdout", io.StringIO()) as output:
                        code = c.main([command, "--prompt", "a tree", "--model", MODEL,
                                       "--selection-token", "fixture", "--count", count])
                    self.assertEqual(code, 1)
                    self.assertIn("between 1 and 4", json.loads(output.getvalue())["error"])
                    credentials.assert_not_called()
                    consume.assert_not_called()
                    launch.assert_not_called()

    def test_images_counts_generation_edit_and_local_recovery(self):
        for editing in (False, True):
            for count in (1, 2, 3, 4):
                with self.subTest(editing=editing, count=count), tempfile.TemporaryDirectory() as tmp, memory_server() as server:
                    root = Path(tmp)
                    ref = root / "reference.png"
                    ref.write_bytes(fixtures.PNG_BYTES)
                    route = "/v1/images/edits" if editing else "/v1/images/generations"
                    server.routes[("POST", route)] = fixtures.json_response(response(count))
                    options = ["--image", str(ref)] if editing else []
                    # Omitted --count must keep old command lines at one output.
                    if count != 1:
                        options += ["--count", str(count)]
                    done, result = self.run_client(root, server, "run", "--prompt", "a blue sky",
                                                   "--model", MODEL, *options)
                    self.assertEqual(done.returncode, 0, done.stdout)
                    self.assertEqual(result["requested_count"], count)
                    self.assertEqual(result["returned_count"], count)
                    self.assertTrue(result["count_complete"])
                    self.assertEqual(len(result["images"]), count)
                    self.assertEqual([r["path"] for r in server.requests], [route])
                    body = server.requests[0]["body"]
                    if editing:
                        self.assertIn(b'name="n"\r\n\r\n' + str(count).encode(), body)
                        self.assertEqual(body.count(b'name="image";'), 1)
                    else:
                        self.assertEqual(json.loads(body)["n"], count)
                    paths = [image["path"] for image in result["images"]]
                    self.assertEqual(len(set(paths)), count)
                    self.assertEqual([Path(p).read_bytes() for p in paths],
                                     [base64.b64decode(value) for value in IMAGES[:count]])
                    server.requests.clear()
                    done, recovered = self.run_client(root, server, "last")
                    self.assertEqual(done.returncode, 0, done.stdout)
                    self.assertEqual(recovered["images"], result["images"])
                    self.assertEqual(recovered["requested_count"], count)
                    self.assertTrue(recovered["count_complete"])
                    self.assertEqual(server.requests, [])

    def test_chat_fallback_and_fixed_chat_make_bounded_calls_with_same_inputs(self):
        for endpoint in ("auto", "chat", "custom"):
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp, memory_server() as server:
                root = Path(tmp)
                ref = root / "reference.png"
                ref.write_bytes(fixtures.PNG_BYTES)
                server.routes[("POST", "/v1/images/edits")] = fixtures.json_response({"data": []})
                replies = iter(IMAGES[:4])
                def chat(_):
                    return fixtures.json_response({"b64_json": next(replies)})
                route = "/custom-chat" if endpoint == "custom" else "/v1/chat/completions"
                server.routes[("POST", route)] = chat
                options = ["--endpoint", endpoint]
                if endpoint == "custom":
                    options += ["--custom-kind", "chat", "--custom-url",
                                f"http://127.0.0.1:{server.server_address[1]}{route}"]
                with mock.patch.object(c.dt, "datetime", wraps=c.dt.datetime) as clock:
                    clock.now.return_value = c.dt.datetime(2026, 1, 1)
                    done, result = self.run_client(root, server, "run", "--prompt", "change sky to blue",
                                                   "--model", MODEL, "--count", "4", "--image", str(ref), *options)
                self.assertEqual(done.returncode, 0, done.stdout)
                self.assertEqual(result["returned_count"], 4)
                self.assertTrue(result["count_complete"])
                self.assertEqual([r["path"] for r in server.requests],
                                 (["/v1/images/edits"] if endpoint == "auto" else []) + [route] * 4)
                bodies = [json.loads(r["body"]) for r in server.requests if r["path"] == route]
                self.assertTrue(all(body == bodies[0] for body in bodies))
                self.assertNotIn("n", bodies[0])
                self.assertEqual(bodies[0]["model"], MODEL)
                self.assertEqual(bodies[0]["size"], "1024x1024")
                self.assertEqual(bodies[0]["messages"][0]["content"][0]["text"], "change sky to blue")
                self.assertEqual(base64.b64decode(bodies[0]["images"][0]["image_url"].split(",", 1)[1]), fixtures.PNG_BYTES)
                self.assertEqual(len({image["path"] for image in result["images"]}), 4)
                self.assertEqual([Path(image["path"]).read_bytes() for image in result["images"]],
                                 [base64.b64decode(value) for value in IMAGES[:4]])

    def test_short_images_response_is_partial_without_top_up_or_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, memory_server() as server:
            server.routes[("POST", "/v1/images/generations")] = fixtures.json_response(response(2))
            done, result = self.run_client(Path(tmp), server, "run", "--prompt", "a tree",
                                           "--model", MODEL, "--count", "4")
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertEqual(result["requested_count"], 4)
            self.assertEqual(result["returned_count"], 2)
            self.assertFalse(result["count_complete"])
            self.assertTrue(any("only 2" in warning for warning in result["warnings"]))
            self.assertEqual([r["path"] for r in server.requests], ["/v1/images/generations"])

    def test_later_chat_error_or_refusal_keeps_saved_images_and_stops(self):
        for refusal in (False, True):
            with self.subTest(refusal=refusal), tempfile.TemporaryDirectory() as tmp, memory_server() as server:
                replies = iter([fixtures.json_response({"b64_json": IMAGES[0]}),
                                fixtures.json_response({"refusal": "blocked"} if refusal else {"error": "unavailable"},
                                                       200 if refusal else 500)])
                server.routes[("POST", "/v1/chat/completions")] = lambda _: next(replies)
                done, result = self.run_client(Path(tmp), server, "run", "--prompt", "a tree",
                                               "--model", MODEL, "--count", "4", "--endpoint", "chat")
                self.assertEqual(done.returncode, 0, done.stdout)
                self.assertEqual(result["returned_count"], 1)
                self.assertFalse(result["count_complete"])
                self.assertEqual(result["provider_refused"], refusal)
                self.assertEqual(len(server.requests), 2)
                self.assertTrue(Path(result["images"][0]["path"]).is_file())
                server.requests.clear()
                _, recovered = self.run_client(Path(tmp), server, "last")
                self.assertEqual(recovered["images"], result["images"])
                self.assertFalse(recovered["count_complete"])
                self.assertEqual(server.requests, [])

    def test_partial_decode_keeps_valid_files_and_excess_results_are_bounded(self):
        for extra in (False, True):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp, memory_server() as server:
                body = response(5) if extra else {"data": [{"b64_json": IMAGES[0]}, {"b64_json": "invalid"}]}
                server.routes[("POST", "/v1/images/generations")] = fixtures.json_response(body)
                done, result = self.run_client(Path(tmp), server, "run", "--prompt", "a tree",
                                               "--model", MODEL, "--count", "4")
                self.assertEqual(done.returncode, 0, done.stdout)
                self.assertEqual(result["returned_count"], 4 if extra else 1)
                self.assertEqual(result["count_complete"], extra)
                self.assertEqual(len(server.requests), 1)
                self.assertEqual(len(list(Path(tmp).rglob("*-images-generations-*.png"))), 4 if extra else 1)

    def test_zero_saved_still_uses_fallback_and_refusal_stops_immediately(self):
        for refusal in (False, True):
            with self.subTest(refusal=refusal), tempfile.TemporaryDirectory() as tmp, memory_server() as server:
                server.routes[("POST", "/v1/images/generations")] = fixtures.json_response(
                    {"refusal": "blocked"} if refusal else {"data": []})
                server.routes[("POST", "/v1/chat/completions")] = fixtures.json_response({"error": "unavailable"}, 500)
                done, result = self.run_client(Path(tmp), server, "run", "--prompt", "a tree",
                                               "--model", MODEL, "--count", "4")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertEqual(result["requested_count"], 4)
                self.assertEqual(result["returned_count"], 0)
                self.assertFalse(result["count_complete"])
                self.assertNotIn("images", result)
                self.assertEqual(len(server.requests), 1 if refusal else 2)
                self.assertEqual(result["ask_user_to_switch_model"], not refusal)

    def test_background_worker_keeps_count_and_status_recovers_without_posts(self):
        with tempfile.TemporaryDirectory() as tmp, memory_server() as server:
            root = Path(tmp).resolve()
            state = root / "state"
            base = f"http://127.0.0.1:{server.server_address[1]}"
            token = c.save_model_selection(state, [{"id": MODEL}], base)
            args = c.build_parser().parse_args(["start", "--model", MODEL, "--prompt", "a tree",
                                               "--count", "4", "--selection-token", token, "--base-url", base])
            with mock.patch.object(c, "thread_state_dir", return_value=state), \
                 mock.patch.object(c, "discover_base_url", return_value=(base, "argument", c.CCSwitchState())), \
                 mock.patch.object(c.subprocess, "Popen", return_value=mock.Mock(pid=123)), \
                 mock.patch("sys.stdout", io.StringIO()) as output:
                self.assertEqual(c.command_start(args), 0)
            job = json.loads(output.getvalue())
            self.assertEqual(job["requested_count"], 4)
            directory = state / "jobs" / job["job_id"]
            request = c.read_json_file(directory / "request.json")
            self.assertEqual(request["argv"][request["argv"].index("--count") + 1], "4")
            server.routes[("POST", "/v1/images/generations")] = fixtures.json_response(response(4))
            cwd = Path.cwd()
            try:
                with mock.patch.object(c, "thread_state_dir", return_value=state), \
                     mock.patch.dict(os.environ, {"KEYLINK_API_KEY": "local-test-key"}):
                    self.assertEqual(c.command_worker(c.build_parser().parse_args(["_worker", "--job-dir", str(directory)])), 0)
            finally:
                os.chdir(cwd)
            result = c.read_json_file(directory / "result.json")
            self.assertEqual(result["returned_count"], 4)
            self.assertTrue(result["count_complete"])
            self.assertEqual(json.loads(server.requests[0]["body"])["n"], 4)
            server.requests.clear()
            with mock.patch.object(c, "thread_state_dir", return_value=state), \
                 mock.patch("sys.stdout", io.StringIO()) as output:
                self.assertEqual(c.command_status(c.build_parser().parse_args(["status", "--job-id", job["job_id"]])), 0)
            recovered = json.loads(output.getvalue())
            self.assertEqual(recovered["images"], result["images"])
            self.assertEqual(recovered["requested_count"], 4)
            self.assertEqual(server.requests, [])
            with mock.patch.object(c, "thread_state_dir", return_value=state), \
                 mock.patch.dict(os.environ, {"KEYLINK_API_KEY": "local-test-key"}), \
                 mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(c.main(request["argv"]), 1)
            self.assertEqual(server.requests, [])


if __name__ == "__main__":
    unittest.main()
