"""Credential routing fixtures contain no real accounts or network requests."""

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/keylink-image/scripts"))
import keylink_image as client


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "User #1 %20"
        self.root.mkdir()
        self.cc = self.root / ".cc-switch"
        self.cc.mkdir()
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {
            "KEYLINK_IMAGE_STATE_DIR": str(self.root / "state"), "CODEX_THREAD_ID": "credential-fixture",
        }, clear=True).start()
        mock.patch.object(client, "user_home", return_value=self.root).start()
        mock.patch.object(client, "ccswitch_process_is_running", return_value=True).start()
        mock.patch.object(client, "local_endpoint_is_running", return_value=False).start()

    def write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8-sig")

    def database(self, providers, *, optional_tables=True):
        path = self.cc / "cc-switch.db"
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE providers (id TEXT, app_type TEXT, is_current INTEGER, settings_config TEXT)")
            if optional_tables:
                # Older schemas may not have enabled, website_url or sort_index.
                connection.execute("CREATE TABLE proxy_config (app_type TEXT, proxy_enabled INTEGER)")
                connection.execute("INSERT INTO proxy_config VALUES ('codex', 1)")
                connection.execute("CREATE TABLE provider_endpoints (app_type TEXT, provider_id TEXT, url TEXT)")
            for identifier, current, key, url in providers:
                settings = {"auth": {"OPENAI_API_KEY": key}, "config": {
                    "model_provider": "fixture", "model_providers": {"fixture": {"base_url": url}},
                }}
                connection.execute("INSERT INTO providers VALUES (?, 'codex', ?, ?)",
                                   (identifier, current, json.dumps(settings)))
        return path

    def aggregate(self, members, route="official", *, mode="proxy", enabled=True):
        self.write_json(self.cc / "live-state.json", {"version": 1, "apps": {"codex": {
            "mode": mode, "proxy_route": route, "stack": {
                "enabled": enabled, "members": members,
                # Old registered keys are not active members.
                "keys": {"retired-keylink": "retired"},
            },
        }}})

    def account(self, **extra):
        self.write_json(self.root / ".codex/auth.json", {
            "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": {"access_token": "private-login-token", "refresh_token": "private-refresh-token"},
            **extra,
        })

    def resolve(self, *, confirm=False):
        return client.discover_api_key(client.DEFAULT_BASE_URL, "default", client.discover_ccswitch_state(),
                                       confirm_codex_auth=confirm)

    def test_legacy_schema_optional_tables_and_columns_do_not_block_key(self):
        for optional in (False, True):
            with self.subTest(optional_tables=optional):
                path = self.cc / "cc-switch.db"
                path.unlink(missing_ok=True)
                self.database([("keylink", 1, "legacy-key", "https://keylinkclub.com/v1")], optional_tables=optional)
                before = path.read_bytes()
                self.assertEqual(self.resolve(), ("legacy-key", "ccswitch-current-provider"))
                self.assertEqual(path.read_bytes(), before)

    def test_device_selection_precedes_stale_database_selection(self):
        self.database([("old", 1, "stale-key", client.DEFAULT_BASE_URL),
                       ("new", 0, "device-key", client.DEFAULT_BASE_URL)])
        self.write_json(self.cc / "settings.json", {"currentProviderCodex": "new"})
        self.assertEqual(self.resolve()[0], "device-key")

    def test_proxy_route_precedes_device_and_database_selection(self):
        self.database([("old", 1, "stale-key", client.DEFAULT_BASE_URL),
                       ("route", 0, "route-key", client.DEFAULT_BASE_URL)])
        self.write_json(self.cc / "settings.json", {"currentProviderCodex": "old"})
        self.aggregate([], route="route", enabled=False)
        self.assertEqual(self.resolve(), ("route-key", "ccswitch-current-provider"))

    def test_account_login_with_aggregation_reads_keylink_member(self):
        self.account()
        self.database([("official", 1, None, "https://api.openai.com"),
                       ("other", 0, "unrelated-key", "https://other.example"),
                       ("keylink", 0, "member-key", client.DEFAULT_BASE_URL)])
        self.aggregate(["official", "other", "keylink"])
        with mock.patch.object(client, "discover_codex_credentials", side_effect=AssertionError("No auth fallback")):
            self.assertEqual(self.resolve(), ("member-key", "ccswitch-aggregation-provider"))

    def test_nonmember_or_disabled_stack_is_not_used(self):
        self.database([("official", 1, None, "https://api.openai.com"),
                       ("retired", 0, "retired-key", client.DEFAULT_BASE_URL)])
        for mode, enabled, members in (("proxy", True, []), ("direct", True, ["retired"]),
                                        ("proxy", False, ["retired"])):
            with self.subTest(mode=mode, enabled=enabled):
                self.aggregate(members, mode=mode, enabled=enabled)
                self.assertEqual(self.resolve(), (None, None))

    def test_multiple_keylink_accounts_require_selection(self):
        self.database([("official", 1, None, "https://api.openai.com"),
                       ("one", 0, "first-key", client.DEFAULT_BASE_URL),
                       ("two", 0, "second-key", client.DEFAULT_BASE_URL)])
        self.aggregate(["one", "two"])
        with self.assertRaises(client.ClientError) as error:
            self.resolve()
        self.assertIn("Multiple", str(error.exception))
        self.assertNotIn("first-key", str(error.exception))
        self.aggregate(["one", "two"], route="two")
        self.assertEqual(self.resolve()[0], "second-key")

    def test_same_key_in_multiple_members_is_unambiguous(self):
        self.database([("one", 0, "shared-key", client.DEFAULT_BASE_URL),
                       ("two", 0, "shared-key", client.DEFAULT_BASE_URL)])
        self.aggregate(["one", "two"])
        self.assertEqual(self.resolve()[0], "shared-key")

    def test_unrelated_host_key_is_not_sent_even_with_keylink_website(self):
        self.database([("wrong", 1, "wrong-key", "https://keylinkclub.com.unrelated.example")])
        with contextlib.closing(sqlite3.connect(self.cc / "cc-switch.db")) as connection, connection:
            connection.execute("ALTER TABLE providers ADD COLUMN website_url TEXT")
            connection.execute("UPDATE providers SET website_url = 'https://keylinkclub.com'")
        self.assertEqual(self.resolve(), (None, None))

    def test_custom_api_host_uses_only_matching_member(self):
        self.database([("keylink", 1, "keylink-key", client.DEFAULT_BASE_URL),
                       ("custom", 0, "custom-key", "https://custom.example/v1")])
        self.aggregate(["keylink", "custom"], route="keylink")
        base, source, state = client.discover_base_url("https://custom.example/v1")
        self.assertEqual(client.discover_api_key(base, source, state)[0], "custom-key")

    def test_account_login_blocks_auth_and_generic_env_with_or_without_ccswitch(self):
        self.account(OPENAI_API_KEY="stale-auth-key")
        os.environ["OPENAI_API_KEY"] = "generic-env-key"
        for running in (False, True):
            for confirm in (False, True):
                with self.subTest(running=running, confirm=confirm), mock.patch.object(
                    client, "ccswitch_process_is_running", return_value=running
                ), mock.patch.object(client, "discover_codex_credentials", side_effect=AssertionError("No fallback")):
                    with self.assertRaises(client.CodexAccountCredentialError) as error:
                        self.resolve(confirm=confirm)
                    self.assertEqual(error.exception.payload["ccswitch_running"], running)

    def test_login_detection_handles_legacy_tokens_codex_plus_and_api_only(self):
        for data, expected in (({"tokens": {"id_token": "signed-in"}}, True),
                               ({"auth": {"auth_mode": "chatgptAuthTokens"}}, True),
                               ({"auth_mode": "apikey", "OPENAI_API_KEY": "real-key"}, False),
                               ({"tokens": {}, "OPENAI_API_KEY": "real-key"}, False)):
            with self.subTest(data=data):
                self.write_json(self.root / ".codex++/auth.json", data)
                self.assertEqual(client.discover_codex_account_login(), expected)

    def test_stopped_ccswitch_does_not_reuse_saved_key_when_account_signed_in(self):
        self.account()
        self.database([("keylink", 1, "saved-key", client.DEFAULT_BASE_URL)])
        with mock.patch.object(client, "ccswitch_process_is_running", return_value=False):
            with self.assertRaises(client.CodexAccountCredentialError):
                self.resolve()

    def test_manual_key_wins_even_when_account_signed_in(self):
        self.account()
        os.environ["KEYLINK_API_KEY"] = "manually-configured-key"
        self.assertEqual(self.resolve(), ("manually-configured-key", "environment:KEYLINK_API_KEY"))

    def test_api_only_auth_fallback_requires_confirmation_even_if_ccswitch_running(self):
        self.write_json(self.root / ".codex/auth.json", {"OPENAI_API_KEY": "private-auth-key"})
        for running in (True, False):
            with self.subTest(running=running), mock.patch.object(client, "ccswitch_process_is_running", return_value=running):
                with self.assertRaises(client.CredentialConfirmationRequired) as error:
                    self.resolve()
                self.assertEqual(error.exception.payload["ccswitch_running"], running)
                self.assertNotIn("private-auth-key", json.dumps(error.exception.payload))
                self.assertEqual(self.resolve(confirm=True), ("private-auth-key", "codex-auth"))

    def test_proxy_placeholder_is_never_used_even_after_confirmation(self):
        self.database([("keylink", 1, "PROXY_MANAGED", client.DEFAULT_BASE_URL)])
        self.write_json(self.root / ".codex/auth.json", {"OPENAI_API_KEY": "PROXY_MANAGED"})
        os.environ["OPENAI_API_KEY"] = "PROXY_MANAGED"
        self.assertEqual(self.resolve(confirm=True), (None, None))

    def test_malformed_local_state_and_database_allow_api_only_fallback(self):
        (self.cc / "cc-switch.db").write_bytes(b"not sqlite")
        (self.cc / "live-state.json").write_text("{broken", encoding="utf-8")
        self.write_json(self.root / ".codex/auth.json", {"OPENAI_API_KEY": "fallback-key"})
        self.assertEqual(self.resolve(confirm=True)[0], "fallback-key")

    def test_models_does_not_make_requests_until_api_key_confirmation(self):
        self.write_json(self.root / ".codex/auth.json", {"OPENAI_API_KEY": "private-auth-key"})
        with mock.patch.object(client, "HttpClient") as http, mock.patch.object(
            client, "model_catalog", return_value=[{"id": "gpt-image-2"}]
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(client.main(["models"]), 1)
            http.assert_not_called()
            self.assertTrue(json.loads(output.getvalue())["credential_confirmation_required"])
            with contextlib.redirect_stdout(output):
                self.assertEqual(client.main(["models", "--confirm-codex-auth"]), 0)
            self.assertEqual(http.call_args.args[:2], (client.DEFAULT_BASE_URL, "private-auth-key"))
            self.assertNotIn("private-auth-key", output.getvalue())

    def test_account_errors_are_actionable_and_never_make_requests(self):
        self.account(OPENAI_API_KEY="stale-auth-key")
        for args in (["models"], ["models", "--confirm-codex-auth"],
                     ["run", "--prompt", "black hole", "--model", "gpt-image-2", "--selection-token", "fixture"]):
            with self.subTest(command=args[0]), mock.patch.object(client, "HttpClient") as http:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(client.main(args), 1)
                http.assert_not_called()
                result = json.loads(output.getvalue())
                self.assertTrue(result["codex_account_detected"])
                self.assertTrue(result["manual_key_required"])
                self.assertIn("KEYLINK_API_KEY", result["error"])
                self.assertNotIn("private-login-token", output.getvalue())
                self.assertNotIn("stale-auth-key", output.getvalue())
