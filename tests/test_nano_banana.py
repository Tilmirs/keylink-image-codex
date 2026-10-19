"""Nano Banana discovery and requests use the existing Gemini workflow."""
import json
import tempfile
import unittest
from pathlib import Path

import test_keylink_image as fixtures

MODEL = "gemini-nano-banana-2.1"


class NanoBananaTests(unittest.TestCase):
    run_client = fixtures.KeylinkImageTests.run_client

    def test_catalog_recognizes_bare_id_without_inventing_availability_or_sizes(self):
        with tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
            root = Path(tmp)
            for listed in (False, True):
                with self.subTest(listed=listed):
                    entries = [{"id": "text-only-model"},
                               {"id": "gemini-3-pro-image", "supported_sizes": ["2048x2048"]}]
                    if listed:
                        entries.append({"id": MODEL})
                    server.routes[("GET", "/v1/models")] = fixtures.json_response({"data": entries})
                    done, result = self.run_client(root, server, "models")
                    self.assertEqual(done.returncode, 0, done.stdout)
                    images = {entry["id"]: entry for entry in result["image_models"]}
                    self.assertEqual(MODEL in images, listed)
                    self.assertNotIn("text-only-model", images)
                    if listed:
                        self.assertEqual(images[MODEL]["published_sizes"], [])
                    self.assertEqual(result["preferred_sizes"], ["1024x1024", "1536x1024", "1024x1536"])
            self.assertTrue(all(request["method"] == "GET" for request in server.requests))

    def test_generation_upload_and_continued_edit_keep_model_and_endpoint_order(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
                root = Path(tmp)
                upload = root / "upload.png"
                upload.write_bytes(fixtures.PNG_BYTES)
                response = {"data": []} if fallback else {"b64_json": fixtures.PNG_B64}
                for endpoint in ("generations", "edits"):
                    server.routes[("POST", "/v1/images/" + endpoint)] = fixtures.json_response(response)
                server.routes[("POST", "/v1/chat/completions")] = fixtures.json_response({"b64_json": fixtures.PNG_B64})
                previous = None
                for reference_args, source in (([], "none"), (["--image", str(upload)], "uploaded"),
                                               (["--use-last"], "last-successful-image")):
                    done, result = self.run_client(
                        root, server, "run", "--prompt", "make the sky blue", "--model", MODEL,
                        "--aspect", "landscape", *reference_args,
                    )
                    self.assertEqual(done.returncode, 0, done.stdout)
                    self.assertEqual(result["model"], MODEL)
                    self.assertEqual(result["requested_size"], "1536x1024")
                    self.assertEqual(result["reference_source"], source)
                    if previous and reference_args == ["--use-last"]:
                        self.assertEqual(result["references"], [previous])
                    expected_endpoint = "edits" if reference_args else "generations"
                    expected_paths = ["/v1/images/" + expected_endpoint]
                    if fallback:
                        expected_paths.append("/v1/chat/completions")
                    self.assertEqual([r["path"] for r in server.requests], expected_paths)
                    for request in server.requests:
                        if request["path"] == "/v1/images/edits":
                            body = request["body"]
                            self.assertIn(b'name="model"\r\n\r\n' + MODEL.encode(), body)
                            self.assertIn(b'name="size"\r\n\r\n1536x1024', body)
                            self.assertIn(b'name="image";', body)
                            self.assertIn(fixtures.PNG_BYTES, body)
                        else:
                            body = json.loads(request["body"])
                            self.assertEqual(body["model"], MODEL)
                            self.assertEqual(body["size"], "1536x1024")
                            if reference_args:
                                self.assertEqual(body["images"][0]["image_url"],
                                                 body["messages"][0]["content"][1]["image_url"]["url"])
                    previous = result["images"][0]["path"]
                    self.assertEqual(Path(previous).read_bytes(), fixtures.PNG_BYTES)
                self.assertEqual(upload.read_bytes(), fixtures.PNG_BYTES)

    def test_explicit_chat_succeeds_or_fails_without_images_request(self):
        for status in (200, 400):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp, fixtures.running_server() as server:
                root = Path(tmp)
                upload = root / "upload.png"
                upload.write_bytes(fixtures.PNG_BYTES)
                body = {"b64_json": fixtures.PNG_B64} if status == 200 else {"error": "model unavailable"}
                server.routes[("POST", "/v1/chat/completions")] = fixtures.json_response(body, status)
                done, result = self.run_client(
                    root, server, "run", "--prompt", "change the sky", "--model", MODEL,
                    "--image", str(upload), "--endpoint", "chat",
                )
                self.assertEqual(done.returncode, 0 if status == 200 else 1, done.stdout)
                self.assertEqual([r["path"] for r in server.requests], ["/v1/chat/completions"])
                self.assertEqual(json.loads(server.requests[0]["body"])["model"], MODEL)
                if status == 200:
                    self.assertEqual(result["endpoint"], "chat")
                    self.assertTrue(Path(result["images"][0]["path"]).is_file())
                else:
                    self.assertFalse(result["ask_user_to_switch_model"])


if __name__ == "__main__":
    unittest.main()
