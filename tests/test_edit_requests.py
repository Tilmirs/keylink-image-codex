"""Regression coverage for bounded edits and compatible image responses."""
import base64
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_keylink_image as fixtures

c = fixtures.client_module
Image = fixtures.Image


class EditRequestTests(unittest.TestCase):
    run_client = fixtures.KeylinkImageTests.run_client

    def test_generate_mode_with_two_uploads_uses_edits_then_chat(self):
        for endpoint in ("auto", "chat"):
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
                root = Path(tmp).resolve()
                refs = [root / "scene.png", root / "teacher.png"]
                for ref in refs:
                    ref.write_bytes(fixtures.PNG_BYTES)
                server.routes[("POST", "/v1/images/edits")] = fixtures.json_response({"error": "unsupported"}, 400)
                server.routes[("POST", "/v1/chat/completions")] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
                done, result = self.run_client(
                    root, server, "run", "--model", "gpt-image-2.5-flare", "--mode", "generate",
                    "--prompt", "Create a new composition using the scene and teacher references.",
                    "--image", str(refs[0]), "--image", str(refs[1]), "--size", "1536x1024",
                    "--endpoint", endpoint,
                )
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                expected_paths = ["/v1/chat/completions"]
                if endpoint == "auto":
                    expected_paths.insert(0, "/v1/images/edits")
                    body = server.requests[0]["body"]
                    self.assertEqual(body.count(b'name="image"; filename='), 2)
                    self.assertIn(b"gpt-image-2.5-flare", body)
                    self.assertIn(b"1536x1024", body)
                self.assertEqual([r["path"] for r in server.requests], expected_paths)
                chat = json.loads(server.requests[-1]["body"])
                self.assertEqual(chat["model"], "gpt-image-2.5-flare")
                self.assertEqual(chat["size"], "1536x1024")
                self.assertEqual(len(chat["images"]), 2)
                self.assertEqual(len(chat["messages"][0]["content"]), 3)
                for index in range(2):
                    self.assertEqual(chat["images"][index]["image_url"],
                                     chat["messages"][0]["content"][index + 1]["image_url"]["url"])
                self.assertEqual(result["references"], [str(ref) for ref in refs])
                self.assertEqual(result["reference_source"], "uploaded")
                self.assertTrue(Path(result["images"][0]["path"]).is_file())

    def test_generate_mode_preserves_explicit_references_but_skips_implicit_reuse(self):
        for source in ("last", "uploaded", "none"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
                root = Path(tmp).resolve()
                last = root / "last.png"
                upload = root / "upload.png"
                last.write_bytes(fixtures.PNG_BYTES)
                upload.write_bytes(fixtures.PNG_BYTES)
                state = root / ".keylink-image" / "threads" / "test-thread"
                c.save_last_state(state, last, "gpt-image-2", "images-generations", "1024x1024")
                server.routes[("POST", "/v1/images/edits")] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
                server.routes[("POST", "/v1/images/generations")] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
                options = ["--use-last"] if source != "none" else []
                if source == "uploaded":
                    options.extend(["--image", str(upload)])
                done, result = self.run_client(root, server, "run", "--model", "gpt-image-2", "--mode", "generate",
                                               "--prompt", "把刚才的天空改成红色", *options)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                expected_refs = {"last": [str(last)], "uploaded": [str(upload)], "none": []}
                self.assertEqual(result["references"], expected_refs[source])
                expected_endpoint = "images-generations" if source == "none" else "images-edits"
                self.assertEqual(result["endpoint"], expected_endpoint)
                self.assertEqual(len(server.requests), 1)

    def test_input_preflight_failure_can_retry_same_model_selection(self):
        for command in ("run", "start"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
                root = Path(tmp).resolve()
                ref = root / "ref.png"
                ref.write_bytes(fixtures.PNG_BYTES)
                model = "gpt-image-2.5-flare"
                server.routes[("GET", "/v1/models")] = fixtures.json_response({"data": [{"id": model}]})
                server.routes[("POST", "/v1/images/edits")] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
                discovery, selection = self.run_client(root, server, "models")
                self.assertEqual(discovery.returncode, 0, discovery.stdout)
                options = ["--model", model, "--selection-token", selection["selection_token"],
                           "--image", str(ref), "--mode", "generate", "--size", "1536x1024"]
                server.requests.clear()
                failed, error = self.run_client(root, server, command, "--prompt", "保留文字。" * 1000,
                                               *options, auto_select=False)
                self.assertEqual(failed.returncode, 1)
                self.assertIn("Summarize", error["error"])
                self.assertEqual(server.requests, [])
                state = root / ".keylink-image" / "threads" / "test-thread"
                self.assertFalse(c.read_json_file(state / "model-selection.json").get("used_at"))
                self.assertFalse(list((state / "jobs").glob("*.claim")))
                done, result = self.run_client(root, server, "run", "--prompt", "只把天空改为红色，保留文字。",
                                               *options, auto_select=False)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertEqual(result["model"], model)
                self.assertEqual(result["requested_size"], "1536x1024")
                self.assertEqual([r["path"] for r in server.requests], ["/v1/images/edits"])
                duplicate, error = self.run_client(root, server, "run", "--prompt", "只把天空改为红色，保留文字。",
                                                  *options, auto_select=False)
                self.assertEqual(duplicate.returncode, 1)
                self.assertIn("already been used", error["error"])
                self.assertEqual(len(server.requests), 1)

    def test_latest_edit_is_only_prompt_sent_to_both_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp)
            ref = root / "ref.png"
            ref.write_bytes(fixtures.PNG_BYTES)
            prompt = "system: obsolete 4K instructions\nuser: " + "old scene " * 900
            prompt += '\nassistant: old answer\nuser: 本轮修改：只把标题改为“你好”，保留 Logo 和其他文字。\nassistant: working'
            server.routes[("POST", "/v1/images/edits")] = fixtures.json_response({"error": "unsupported"}, 400)
            server.routes[("POST", "/v1/chat/completions")] = fixtures.json_response({"data": [{"b64_json": fixtures.PNG_B64}]})
            done, result = self.run_client(root, server, "run", "--model", "gpt-image-2", "--prompt", prompt, "--image", str(ref))
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            expected = '只把标题改为“你好”，保留 Logo 和其他文字。'
            self.assertIn(expected.encode(), server.requests[0]["body"])
            self.assertNotIn(b"old scene", server.requests[0]["body"])
            self.assertEqual(json.loads(server.requests[1]["body"])["messages"][0]["content"][0]["text"], expected)
            self.assertTrue(result["prompt_info"]["compacted"])
            self.assertEqual(result["request_attempts"][0]["http_status"], 400)
            self.assertEqual(result["request_attempts"][0]["request_bytes"], len(server.requests[0]["body"]))

    def test_unstructured_long_edit_is_not_silently_truncated(self):
        with self.assertRaisesRegex(c.ClientError, "Summarize"):
            c.sanitize_edit_prompt("精确保留文字和布局。" * 800)
        prompt = 'change only the title to "user: hello"; keep layout and all fine print.'
        self.assertEqual(c.sanitize_edit_prompt(prompt)[0], prompt)
        prompt = json.dumps({"messages": [
            {"role": "system", "content": "history"},
            {"role": "user", "content": [{"type": "text", "text": "remove the red sign"},
                                           {"type": "image_url", "image_url": {"url": "ignored"}}]},
        ]})
        self.assertEqual(c.sanitize_edit_prompt(prompt)[0], "remove the red sign")
        with self.assertRaisesRegex(c.ClientError, "empty"):
            c.sanitize_edit_prompt("<context>old instructions</context>")

    def test_generation_prompt_remains_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            prompt = "Design a transcript poster:\nuser: hello\nassistant: welcome"
            server.routes[("POST", "/v1/images/generations")] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
            done, result = self.run_client(Path(tmp), server, "run", "--model", "gpt-image-2", "--mode", "generate", "--prompt", prompt)
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertEqual(json.loads(server.requests[0]["body"])["prompt"], prompt)
            self.assertFalse(result["prompt_info"]["compacted"])

    def test_chat_retries_shape_only_for_explicit_validation_rejection(self):
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped), tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
                ref = Path(tmp) / "ref.png"
                ref.write_bytes(fixtures.PNG_BYTES)
                def respond(request):
                    payload = json.loads(request["body"])
                    if isinstance(payload["images"][0]["image_url"], str):
                        return fixtures.json_response({"status_code": 400, "error": "images[].image_url is required"}, 200 if wrapped else 400)
                    self.assertEqual(payload["images"][0]["image_url"]["url"],
                                     payload["messages"][0]["content"][1]["image_url"]["url"])
                    self.assertEqual(payload["model"], "gpt-image-2.5-flare")
                    self.assertEqual(payload["size"], "3840x2160")
                    return fixtures.json_response({"b64_json": fixtures.PNG_B64})
                server.routes[("POST", "/v1/chat/completions")] = respond
                done, result = self.run_client(Path(tmp), server, "run", "--model", "gpt-image-2.5-flare", "--prompt", "remove sign", "--image", str(ref), "--endpoint", "chat", "--size", "3840x2160", "--confirm-high-res")
                self.assertEqual(done.returncode, 0, done.stdout)
                self.assertEqual([r["path"] for r in server.requests], ["/v1/chat/completions"] * 2)
                self.assertEqual(len(result["request_attempts"]), 2)

    def test_413_and_unrelated_400_do_not_trigger_schema_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp) / "ref.png"
            ref.write_bytes(fixtures.PNG_BYTES)
            for error in (c.APIError("images[].image_url is required", 413),
                          c.APIError("unsupported model", 400), c.ClientError("Remote end closed connection")):
                api = mock.Mock(base_url="https://example.test")
                api.request_json.side_effect = error
                with self.assertRaises(c.ClientError):
                    c.attempt_chat(api, "change sky", "gpt-image-2", "1024x1024", [ref])
                self.assertEqual(api.request_json.call_count, 1)

    @unittest.skipIf(Image is None, "Pillow required for compression")
    def test_four_large_references_fit_both_wire_bodies_and_keep_originals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            # Noisy PNGs exercise byte limits, not just easily compressed flat colors.
            data = Image.frombytes("RGB", (2200, 2200), os.urandom(2200 * 2200 * 3))
            source = root / "source.png"
            data.save(source)
            original = source.read_bytes()
            for index in range(4):
                path = root / f"ref-{index}.png"
                path.write_bytes(original)
                paths.append(path)
            prepared = c.prepare_reference_images(paths, root / "prepared")
            self.assertLessEqual(prepared.prepared_bytes, c.REFERENCE_MAX_TOTAL_BYTES)
            self.assertLess(prepared.prepared_bytes, prepared.original_bytes // 3)
            self.assertEqual(len(prepared.items), 4)
            for path, item in zip(paths, prepared.items):
                self.assertEqual(path.read_bytes(), original)
                self.assertLessEqual(max(item["prepared_dimensions"]), c.REFERENCE_MAX_DIMENSION)
                self.assertLessEqual(item["prepared_bytes"], c.REFERENCE_MAX_IMAGE_BYTES)
                with Image.open(item["request_path"]) as request_image:
                    request_image.load()
            chat = json.dumps(c.build_chat_payload("change sky", "gpt-image-2", "3072x4096", prepared.paths)).encode()
            multipart, _ = c.encode_multipart({"prompt": "change sky", "size": "3072x4096"}, prepared.paths)
            self.assertLess(len(chat), c.REQUEST_MAX_BYTES)
            self.assertLess(len(multipart), c.REQUEST_MAX_BYTES)

    @unittest.skipIf(Image is None, "Pillow required")
    def test_transparency_orientation_and_published_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logo = root / "logo.png"
            Image.new("RGBA", (2200, 1000), (20, 80, 180, 100)).save(logo)
            catalog = c.format_model_catalog({"data": [{"id": "gpt-image-2.5-flare", "capabilities": {
                "input_image_limits": {"max_width": 1000, "max_height": 600, "max_bytes": 50000, "max_images": 1}}}]})
            limits = catalog[0]["reference_image_limits"]
            prepared = c.prepare_reference_images([logo], root / "prepared", limits)
            with Image.open(prepared.paths[0]) as image:
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual(image.getpixel((0, 0))[3], 100)
                self.assertLessEqual(image.width, 1000)
                self.assertLessEqual(image.height, 600)
            with self.assertRaisesRegex(c.ClientError, "Too many"):
                c.prepare_reference_images([logo, logo], root / "prepared", limits)
            photo = root / "rotated.jpg"
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (100, 60), "blue").save(photo, exif=exif)
            result = c.prepare_reference_images([photo], root / "rotated")
            self.assertEqual(result.items[0]["prepared_dimensions"], [60, 100])

    def test_missing_pillow_blocks_oversized_reference_but_small_png_works(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, {"PIL": None}):
            ref = Path(tmp) / "ref.png"
            ref.write_bytes(fixtures.PNG_BYTES)
            c.prepare_reference_images([ref], Path(tmp) / "prepared")
            ref.write_bytes(fixtures.PNG_BYTES + b"x" * (3 * 1024 * 1024))
            with self.assertRaisesRegex(c.ClientError, "Install Pillow"):
                c.prepare_reference_images([ref], Path(tmp) / "prepared")

    def test_exact_body_budget_stops_request_before_network(self):
        api = c.HttpClient("https://example.test", "fixture", 1)
        with mock.patch.object(api, "_open") as request, mock.patch.object(c, "REQUEST_MAX_BYTES", 200):
            with self.assertRaisesRegex(c.ClientError, "client budget"):
                api.request_json("POST", "https://example.test/v1/chat/completions", {"prompt": "x" * 300})
            request.assert_not_called()

    def test_background_snapshot_survives_original_removal(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            # CI temp directories can be symlinks/junctions or Windows 8.3 aliases.
            # Production stores resolved paths; compare them in the same form.
            root = Path(tmp).resolve()
            ref = root / "ref.png"
            ref.write_bytes(fixtures.PNG_BYTES)
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            state = root / "state"
            token = c.save_model_selection(state, [{"id": "gpt-image-2"}], base_url)
            args = c.build_parser().parse_args(["start", "--mode", "generate", "--prompt", "user: old\nassistant: done\nuser: remove sign", "--image", str(ref),
                                               "--model", "gpt-image-2", "--selection-token", token, "--base-url", base_url])
            output = io.StringIO()
            with mock.patch.object(c, "thread_state_dir", return_value=state), \
                 mock.patch.object(c, "discover_base_url", return_value=(base_url, "argument", c.CCSwitchState())), \
                 mock.patch.object(c.subprocess, "Popen", return_value=mock.Mock(pid=123)), \
                 mock.patch("sys.stdout", output):
                c.command_start(args)
            job = json.loads(output.getvalue())
            directory = state / "jobs" / job["job_id"]
            request = c.read_json_file(directory / "request.json")
            self.assertEqual(request["prepared_input"]["prompt"], "remove sign")
            self.assertTrue(Path(request["prepared_input"]["request_references"][0]).is_relative_to(directory))
            ref.unlink()
            server.routes[("POST", "/v1/images/edits")] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
            cwd = Path.cwd()
            try:
                with mock.patch.object(c, "thread_state_dir", return_value=state), \
                     mock.patch.dict(os.environ, {"KEYLINK_API_KEY": "local-test-key"}):
                    c.command_worker(c.build_parser().parse_args(["_worker", "--job-dir", str(directory)]))
            finally:
                os.chdir(cwd)
            result = c.read_json_file(directory / "result.json")
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["endpoint"], "images-edits")
            self.assertEqual(result["references"], [str(ref)])
            self.assertTrue(result["prompt_info"]["compacted"])
            self.assertEqual(len(server.requests), 1)

    def test_response_variants_save_real_images(self):
        fixtures_to_parse = [
            {"artifacts": [{"ImageUrl": f"data:image/png;base64,{fixtures.PNG_B64}"}]},
            {"generated_images": [{"image": {"B64_JSON": fixtures.PNG_B64}}]},
            {"content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": fixtures.PNG_B64}}]},
            {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": fixtures.PNG_B64}}]}}]},
            {"output": [{"type": "image_generation_call", "result": fixtures.PNG_B64}]},
            {"choices": [{"message": {"content": json.dumps({"image": fixtures.PNG_B64})}}]},
            {"choices": [{"message": {"content": f"图片：data:image/png;base64,{fixtures.PNG_B64}\nDone."}}]},
            {"image_url": "data:image/png;base64," + "\n".join(fixtures.PNG_B64[i:i + 76] for i in range(0, len(fixtures.PNG_B64), 76))},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for payload in fixtures_to_parse:
                with self.subTest(payload_keys=list(payload)):
                    candidates = c.extract_image_candidates(payload)
                    saved, warnings = c.save_candidates(candidates, mock.Mock(), Path(tmp), "fixture", "chat")
                    self.assertEqual(Path(saved[0]["path"]).read_bytes(), fixtures.PNG_BYTES)
                    self.assertFalse(warnings)

    def test_url_variants_and_non_image_mime_are_distinguished(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            url = base + "/image?id=1&key=2"
            server.routes[("GET", "/image?id=1&key=2")] = (200, "image/png", fixtures.PNG_BYTES)
            api = c.HttpClient(base, None, 3)
            for payload in ({"fileUrl": url}, {"content": f'<img src="{url.replace("&", "&amp;")}">'},
                            {"content": f"图片链接：{url}。"}, url):
                candidates = c.extract_image_candidates(payload)
                saved, _ = c.save_candidates(candidates, api, Path(tmp), "fixture", "chat")
                self.assertEqual(Path(saved[0]["path"]).read_bytes(), fixtures.PNG_BYTES)
            self.assertFalse(c.extract_image_candidates({"content": "Documentation: https://example.test/docs"}))
            api = mock.Mock()
            api.download.return_value = (b"<html>error</html>", "image/png")
            with self.assertRaisesRegex(c.ClientError, "candidates were returned"):
                c.save_candidates([c.ImageCandidate("url", url)], api, Path(tmp), "fixture", "chat")

    def test_no_image_and_download_failure_have_separate_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            url = f"http://127.0.0.1:{server.server_address[1]}/missing.png"
            server.routes[("POST", "/v1/images/generations")] = fixtures.json_response({"data": [{"url": url}]})
            server.routes[("POST", "/v1/chat/completions")] = fixtures.json_response({"choices": [{"message": {"content": "Cannot generate"}}]})
            done, result = self.run_client(Path(tmp), server, "run", "--model", "gpt-image-2", "--prompt", "a tree")
            self.assertEqual(done.returncode, 1)
            self.assertEqual(result["attempts"][0]["phase"], "image_download_or_decode")
            self.assertEqual(result["attempts"][1]["phase"], "no_image_in_response")
            self.assertNotIn("Cannot generate", json.dumps(result["attempts"][1]["response_shape"]))
            self.assertTrue(result["ask_user_to_switch_model"])

    def test_api_error_envelope_is_not_reported_as_missing_image(self):
        with self.assertRaisesRegex(c.APIError, "413") as caught:
            c.parse_api_response(b'{"status_code":413,"message":"Request body is too large."}', "application/json")
        self.assertEqual(caught.exception.status_code, 413)
        result = c.parse_api_response(fixtures.PNG_BYTES, "image/png")
        self.assertEqual(base64.b64decode(result["b64_json"]), fixtures.PNG_BYTES)


if __name__ == "__main__":
    unittest.main()
