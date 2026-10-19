"""Keep requested output pixels separate from upload compression dimensions."""
import base64
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


def image_response(width, height):
    return fixtures.json_response({"b64_json": base64.b64encode(fixtures.png_image(width, height)).decode("ascii")})


class EditDimensionTests(unittest.TestCase):
    run_client = fixtures.KeylinkImageTests.run_client

    @unittest.skipIf(fixtures.Image is None, "Pillow required for compression")
    def test_one_oversized_reference_preserves_smaller_uploads_and_sends_all(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp).resolve()
            refs = [root / "small.png", root / "large.png", root / "tiny.png"]
            for path, noise_height in zip(refs, (180, 1536, 30)):
                picture = fixtures.Image.new("RGB", (1024, 1536), "white")
                noise = fixtures.Image.frombytes("RGB", (1024, noise_height), os.urandom(1024 * noise_height * 3))
                picture.paste(noise)
                picture.save(path)
            originals = [path.read_bytes() for path in refs]
            self.assertLess(len(originals[0]), c.REFERENCE_MAX_IMAGE_BYTES)
            self.assertGreater(len(originals[0]), c.REFERENCE_MAX_IMAGE_BYTES // 3)
            self.assertGreater(len(originals[1]), c.REFERENCE_MAX_IMAGE_BYTES)
            self.assertLess(len(originals[2]), c.REFERENCE_MAX_IMAGE_BYTES)
            server.routes[("POST", "/v1/images/edits")] = fixtures.json_response({"error": "unsupported"}, 400)
            server.routes[("POST", "/v1/chat/completions")] = image_response(1024, 1536)
            options = [part for path in refs for part in ("--image", str(path))]
            done, result = self.run_client(root, server, "run", "--model", MODEL,
                                           "--prompt", "combine these references", *options)
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertEqual(result["requested_size"], "1024x1536")
            metadata = result["reference_images"]
            uploads = [Path(item["request_path"]).read_bytes() for item in metadata]
            self.assertEqual([item["normalized"] for item in metadata], [False, True, False])
            self.assertEqual(uploads[0], originals[0])
            self.assertEqual(uploads[2], originals[2])
            self.assertLessEqual(sum(map(len, uploads)), c.REFERENCE_MAX_TOTAL_BYTES)
            self.assertTrue(all(len(raw) <= c.REFERENCE_MAX_IMAGE_BYTES for raw in uploads))
            self.assertEqual(len(uploads), 3)
            self.assertEqual([r["path"] for r in server.requests], ["/v1/images/edits", "/v1/chat/completions"])
            self.assertEqual(server.requests[0]["body"].count(b'name="image";'), 3)
            for raw in uploads:
                self.assertIn(raw, server.requests[0]["body"])
            chat = json.loads(server.requests[1]["body"])
            self.assertEqual([base64.b64decode(item["image_url"].split(",", 1)[1]) for item in chat["images"]], uploads)
            self.assertEqual([item["image_url"]["url"] for item in chat["messages"][0]["content"] if item["type"] == "image_url"],
                             [item["image_url"] for item in chat["images"]])
            self.assertEqual([path.read_bytes() for path in refs], originals)

    @unittest.skipIf(fixtures.Image is None, "Pillow required for compression")
    def test_small_references_only_change_when_aggregate_budget_requires_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            refs = [root / "first.png", root / "second.png"]
            picture = fixtures.Image.new("RGB", (1024, 1536), "white")
            picture.paste(fixtures.Image.frombytes("RGB", (1024, 200), os.urandom(1024 * 200 * 3)))
            picture.save(refs[0])
            refs[1].write_bytes(refs[0].read_bytes())
            originals = [path.read_bytes() for path in refs]
            self.assertTrue(all(len(raw) < c.REFERENCE_MAX_IMAGE_BYTES for raw in originals))
            self.assertGreater(sum(map(len, originals)), c.REFERENCE_MAX_IMAGE_BYTES)
            default = c.prepare_reference_images(refs, root / "default", snapshot=True)
            self.assertEqual([item["normalized"] for item in default.items], [False, False])
            self.assertEqual([path.read_bytes() for path in default.paths], originals)
            limits = {"max_total_bytes": 1024 * 1024}
            limited = c.prepare_reference_images(refs, root / "limited", limits, snapshot=True)
            self.assertLessEqual(limited.prepared_bytes, limits["max_total_bytes"])
            self.assertEqual([item["normalized"] for item in limited.items], [True, False])
            self.assertEqual(limited.paths[1].read_bytes(), originals[1])
            # Below the aggregate limit, even a file larger than half the budget stays intact.
            refs[1].write_bytes(fixtures.png_image(1024, 1536))
            unmodified = c.prepare_reference_images(refs, root / "unchanged", limits, snapshot=True)
            self.assertEqual([item["normalized"] for item in unmodified.items], [False, False])
            self.assertEqual(unmodified.paths[0].read_bytes(), originals[0])
            self.assertEqual(refs[0].read_bytes(), originals[0])

    @unittest.skipIf(fixtures.Image is None, "Pillow required for upload fixtures")
    def test_fifteen_near_megabyte_images_reach_both_endpoints_without_recompression(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp).resolve()
            picture = fixtures.Image.new("RGB", (1024, 1536), "white")
            picture.paste(fixtures.Image.frombytes("RGB", (1024, 300), os.urandom(1024 * 300 * 3)))
            buffer = io.BytesIO()
            picture.save(buffer, "PNG")
            raw = buffer.getvalue()
            self.assertGreater(len(raw), 900000)
            self.assertLessEqual(len(raw), 1024 * 1024)
            refs = [root / f"ref-{i}.png" for i in range(15)]
            for ref in refs:
                ref.write_bytes(raw)
            server.routes[("POST", "/v1/images/edits")] = fixtures.json_response({"error": "unsupported"}, 400)
            server.routes[("POST", "/v1/chat/completions")] = image_response(1024, 1536)
            options = [part for ref in refs for part in ("--image", str(ref))]
            done, result = self.run_client(root, server, "run", "--model", MODEL,
                                           "--prompt", "combine these references", *options)
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertEqual(len(result["reference_images"]), 15)
            self.assertTrue(all(not item["normalized"] for item in result["reference_images"]))
            self.assertEqual(result["references"], [str(ref) for ref in refs])
            self.assertEqual([r["path"] for r in server.requests], ["/v1/images/edits", "/v1/chat/completions"])
            multipart, chat_request = server.requests
            self.assertEqual(multipart["body"].count(b'name="image";'), 15)
            self.assertGreater(len(multipart["body"]), 9 * 1024 * 1024)
            self.assertGreater(len(chat_request["body"]), 30 * 1024 * 1024)
            self.assertLess(len(chat_request["body"]), c.REQUEST_MAX_BYTES)
            chat = json.loads(chat_request["body"])
            self.assertEqual(len(chat["images"]), 15)
            self.assertTrue(all(base64.b64decode(item["image_url"].split(",", 1)[1]) == raw for item in chat["images"]))
            self.assertEqual([item["image_url"]["url"] for item in chat["messages"][0]["content"] if item["type"] == "image_url"],
                             [item["image_url"] for item in chat["images"]])
            self.assertTrue(all(ref.read_bytes() == raw for ref in refs))

    def test_sixteen_references_stop_before_submission_without_discarding(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp)
            ref = root / "reference.png"
            ref.write_bytes(fixtures.PNG_BYTES)
            options = [part for _ in range(16) for part in ("--image", str(ref))]
            done, error = self.run_client(root, server, "run", "--model", MODEL,
                                          "--prompt", "combine references", *options)
            self.assertEqual(done.returncode, 1, done.stdout)
            self.assertIn("current limit is 15", error["error"])
            self.assertEqual(server.requests, [])

    @unittest.skipIf(fixtures.Image is None, "Pillow required for WebP fixture")
    def test_webp_last_recovery_preserves_actual_dimensions_and_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp)
            buffer = io.BytesIO()
            fixtures.Image.new("RGB", (1024, 1536), "blue").save(buffer, "WEBP")
            server.routes[("POST", "/v1/images/generations")] = fixtures.json_response(
                {"b64_json": base64.b64encode(buffer.getvalue()).decode("ascii")})
            done, generated = self.run_client(root, server, "run", "--model", MODEL, "--prompt", "a tree")
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertFalse(generated["images"][0]["matches_requested_size"])
            server.requests.clear()
            recovered, result = self.run_client(root, server, "last")
            self.assertEqual(recovered.returncode, 0, recovered.stdout)
            self.assertEqual(result["images"], generated["images"])
            self.assertEqual(result["warnings"], generated["warnings"])
            self.assertEqual(server.requests, [])

    @unittest.skipIf(fixtures.Image is None, "Pillow required for compression")
    def test_compressed_portrait_upload_and_continued_edits_keep_original_pixels(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp).resolve()
            ref = root / "portrait.png"
            fixtures.Image.frombytes("RGB", (1024, 1536), os.urandom(1024 * 1536 * 3)).save(ref)
            original = ref.read_bytes()
            self.assertGreater(len(original), c.REFERENCE_MAX_IMAGE_BYTES)
            server.routes[("POST", "/v1/images/edits")] = fixtures.json_response({"error": "unsupported"}, 400)
            server.routes[("POST", "/v1/chat/completions")] = image_response(1024, 1536)
            for options in (["--image", str(ref)], ["--use-last"], []):
                with self.subTest(options=options):
                    done, result = self.run_client(root, server, "run", "--model", MODEL,
                                                   "--prompt", "change only the sky", *options)
                    self.assertEqual(done.returncode, 0, done.stdout)
                    self.assertEqual(result["requested_size"], "1024x1536")
                    self.assertEqual(result["size_source"], "original-reference")
                    self.assertIn(b'name="size"\r\n\r\n1024x1536', server.requests[0]["body"])
                    self.assertEqual(json.loads(server.requests[1]["body"])["size"], "1024x1536")
                    self.assertEqual([r["path"] for r in server.requests], ["/v1/images/edits", "/v1/chat/completions"])
                    reference = result["reference_images"][0]
                    self.assertEqual(reference["original_dimensions"], [1024, 1536])
                    self.assertEqual(reference["prepared_dimensions"], [1024, 1536])
                    self.assertLessEqual(reference["prepared_bytes"], c.REFERENCE_MAX_IMAGE_BYTES)
                    self.assertTrue(result["images"][0]["matches_requested_size"])
            self.assertEqual(ref.read_bytes(), original)

    def test_explicit_size_or_aspect_overrides_original(self):
        for options, expected, source in ((["--size", "1024x1024"], "1024x1024", "explicit-size"),
                                          (["--aspect", "square"], "1024x1024", "explicit-aspect"),
                                          (["--aspect", "landscape"], "1536x1024", "explicit-aspect")):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
                root = Path(tmp)
                ref = root / "portrait.png"
                ref.write_bytes(fixtures.png_image(1024, 1536))
                width, height = map(int, expected.split("x"))
                server.routes[("POST", "/v1/images/edits")] = image_response(width, height)
                done, result = self.run_client(root, server, "run", "--model", MODEL, "--prompt", "change sky",
                                               "--image", str(ref), *options)
                self.assertEqual(done.returncode, 0, done.stdout)
                self.assertEqual(result["requested_size"], expected)
                self.assertEqual(result["size_source"], source)
                self.assertTrue(result["images"][0]["matches_requested_size"])

    def test_nonstandard_original_stops_before_submission_and_keeps_selection(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp).resolve()
            ref = root / "custom.png"
            ref.write_bytes(fixtures.png_image(800, 1200))
            server.routes[("GET", "/v1/models")] = fixtures.json_response({"data": [{"id": MODEL}]})
            _, selection = self.run_client(root, server, "models")
            server.requests.clear()
            argv = ["run", "--model", MODEL, "--prompt", "change sky", "--image", str(ref),
                    "--selection-token", selection["selection_token"]]
            failed, error = self.run_client(root, server, *argv, auto_select=False)
            self.assertEqual(failed.returncode, 1, failed.stdout)
            self.assertIn("800x1200", error["error"])
            self.assertIn("confirmation", error["error"])
            self.assertEqual(server.requests, [])
            state = root / ".keylink-image" / "threads" / "test-thread"
            self.assertFalse(c.read_json_file(state / "model-selection.json").get("used_at"))
            server.routes[("POST", "/v1/images/edits")] = image_response(800, 1200)
            done, result = self.run_client(root, server, *argv, "--confirm-high-res", auto_select=False)
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertEqual(result["requested_size"], "800x1200")

    def test_high_resolution_edit_cannot_inherit_a_small_canvas_after_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp)
            ref = root / "reference.png"
            ref.write_bytes(fixtures.png_image(1024, 1536))
            server.routes[("POST", "/v1/images/edits")] = image_response(1024, 1536)
            failed, error = self.run_client(root, server, "run", "--model", MODEL,
                                            "--prompt", "make this 4K", "--image", str(ref),
                                            "--confirm-high-res")
            self.assertEqual(failed.returncode, 1, failed.stdout)
            self.assertIn("explicit --size", error["error"])
            self.assertEqual(server.requests, [])

    def test_mixed_reference_dimensions_require_output_choice(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp)
            refs = [root / "portrait.png", root / "square.png"]
            refs[0].write_bytes(fixtures.png_image(1024, 1536))
            refs[1].write_bytes(fixtures.PNG_BYTES)
            options = ["--image", str(refs[0]), "--image", str(refs[1])]
            failed, error = self.run_client(root, server, "run", "--model", MODEL, "--prompt", "combine references", *options)
            self.assertEqual(failed.returncode, 1)
            self.assertIn("different original dimensions", error["error"])
            self.assertEqual(server.requests, [])
            server.routes[("POST", "/v1/images/edits")] = image_response(1536, 1024)
            done, result = self.run_client(root, server, "run", "--model", MODEL, "--prompt", "combine references",
                                           *options, "--aspect", "landscape")
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertEqual(result["requested_size"], "1536x1024")

    def test_provider_dimension_mismatch_warns_without_resubmission(self):
        for endpoint in ("auto", "chat"):
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
                root = Path(tmp)
                ref = root / "portrait.png"
                ref.write_bytes(fixtures.png_image(1024, 1536))
                path = "/v1/images/edits" if endpoint == "auto" else "/v1/chat/completions"
                server.routes[("POST", path)] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
                done, result = self.run_client(root, server, "run", "--model", MODEL, "--prompt", "change sky",
                                               "--image", str(ref), "--endpoint", endpoint)
                self.assertEqual(done.returncode, 0, done.stdout)
                self.assertFalse(result["images"][0]["matches_requested_size"])
                self.assertTrue(any("Provider returned 1024x1024, but 1024x1536 was requested" in w for w in result["warnings"]))
                self.assertEqual([r["path"] for r in server.requests], [path])
                self.assertEqual(Path(result["images"][0]["path"]).read_bytes(), fixtures.PNG_BYTES)
                server.requests.clear()
                recovered, history = self.run_client(root, server, "last")
                self.assertEqual(recovered.returncode, 0, recovered.stdout)
                self.assertEqual(history["images"], result["images"])
                self.assertEqual(history["warnings"], result["warnings"])
                self.assertEqual(server.requests, [])

    @unittest.skipIf(fixtures.Image is None, "Pillow required for reference resizing")
    def test_background_edit_keeps_original_4k_pixels_after_reference_shrinks(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp).resolve()
            ref = root / "original.png"
            ref.write_bytes(fixtures.png_image(3840, 2160))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            state = root / "state"
            token = c.save_model_selection(state, [{"id": MODEL}], base)
            args = c.build_parser().parse_args(["start", "--model", MODEL, "--prompt", "change sky",
                                               "--selection-token", token, "--image", str(ref), "--base-url", base,
                                               "--confirm-high-res"])
            output = io.StringIO()
            with mock.patch.object(c, "thread_state_dir", return_value=state), \
                 mock.patch.object(c, "discover_base_url", return_value=(base, "argument", c.CCSwitchState())), \
                 mock.patch.object(c.subprocess, "Popen", return_value=mock.Mock(pid=123)), \
                 mock.patch("sys.stdout", output):
                self.assertEqual(c.command_start(args), 0)
            job = json.loads(output.getvalue())
            self.assertEqual(job["requested_size"], "3840x2160")
            directory = state / "jobs" / job["job_id"]
            request = c.read_json_file(directory / "request.json")
            metadata = request["prepared_input"]["reference_images"][0]
            self.assertEqual(metadata["original_dimensions"], [3840, 2160])
            self.assertLessEqual(max(metadata["prepared_dimensions"]), 2048)
            ref.unlink()
            server.routes[("POST", "/v1/images/edits")] = fixtures.json_response({"data": []})
            server.routes[("POST", "/v1/chat/completions")] = image_response(3840, 2160)
            cwd = Path.cwd()
            try:
                with mock.patch.object(c, "thread_state_dir", return_value=state), \
                     mock.patch.dict(os.environ, {"KEYLINK_API_KEY": "local-test-key"}):
                    self.assertEqual(c.command_worker(c.build_parser().parse_args(["_worker", "--job-dir", str(directory)])), 0)
            finally:
                os.chdir(cwd)
            result = c.read_json_file(directory / "result.json")
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["requested_size"], "3840x2160")
            self.assertIn(b'name="size"\r\n\r\n3840x2160', server.requests[0]["body"])
            self.assertEqual(json.loads(server.requests[1]["body"])["size"], "3840x2160")
            self.assertTrue(result["images"][0]["matches_requested_size"])

    @unittest.skipIf(fixtures.Image is None, "Pillow required for EXIF orientation")
    def test_default_pixels_use_orientation_before_reference_normalization(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp)
            ref = root / "rotated.jpg"
            exif = fixtures.Image.Exif()
            exif[274] = 6
            fixtures.Image.new("RGB", (1024, 1536), "blue").save(ref, exif=exif)
            server.routes[("POST", "/v1/images/edits")] = image_response(1536, 1024)
            done, result = self.run_client(root, server, "run", "--model", MODEL,
                                           "--prompt", "change sky", "--image", str(ref))
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertEqual(result["requested_size"], "1536x1024")
            self.assertEqual(result["reference_images"][0]["original_dimensions"], [1024, 1536])
            self.assertEqual(result["reference_images"][0]["original_oriented_dimensions"], [1536, 1024])

    def test_generation_without_references_still_defaults_to_square(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            server.routes[("POST", "/v1/images/generations")] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
            done, result = self.run_client(Path(tmp), server, "run", "--model", MODEL, "--prompt", "a blue sky")
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertEqual(result["requested_size"], "1024x1024")
            self.assertEqual(result["size_source"], "default-square")


if __name__ == "__main__":
    unittest.main()
