#!/usr/bin/env python3
"""Keylink image generation and editing client for Codex skills."""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import csv
import datetime as dt
import errno
import hashlib
import html
import http.client
import io
import json
import mimetypes
import os
import re
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_BASE_URL = "https://keylinkclub.com"
DEFAULT_MODEL_PRIORITY = (
    "gpt-image-2",
    "gpt-image-2.5",
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5-flare",
    "gemini-3-pro-image",
    "gemini-3.1-flash-image",
    "gemini-nano-banana-2.1",
)
CONSERVATIVE_SIZES = ("1024x1024", "1536x1024", "1024x1536")
EXPERIMENTAL_SIZES = ("2560x1440", "3840x2160")
OUTPUT_MAX_COUNT = 4
PROMPT_MODES = (
    {"id": "restrained", "label": "克制模式", "description": "完全使用用户提示词，不做任何修改"},
    {"id": "master", "label": "大师模式", "description": "保留用户目标和硬性要求，全面优化提示词后生成"},
    {"id": "professional", "label": "专业模式", "description": "先选择风格参考图，再调优提示词并与素材图一起作为参考"},
)
SIZE_RE = re.compile(r"(?<!\d)(\d{2,5})[xX](\d{2,5})(?!\d)")
DATA_URL_RE = re.compile(
    r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/_-]+(?:\r?\n[A-Za-z0-9+/_-]+)*={0,2})", re.I
)
INLINE_DATA_URL_RE = re.compile(
    r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/_-]+={0,2})", re.I
)
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", re.I)
HTTP_URL_RE = re.compile(r"https?://[^\s<>\]\[\"']+", re.I)
HIGH_RES_INTENT_RE = re.compile(
    r"更高分辨率|高分辨率|更清晰|高清|超高清|\b(?:2k|4k|uhd)\b|"
    r"higher\s+resolution|high\s+resolution|ultra\s+high\s+definition",
    re.I,
)
EDIT_PROMPT_MARKER_RE = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]+)?(?:编辑提示|编辑要求|本轮修改|本次修改|"
    r"edit\s+prompt|requested\s+change)[ \t]*(?:[:：][ \t]*|\n)"
)
EDIT_ROLE_LINE_RE = re.compile(
    r"(?im)^\s*(?:system|assistant|user|developer|系统|助手|用户|开发者)\s*[:：]"
)
EDIT_CONTEXT_LINE_RE = re.compile(
    r"(?im)^\s*(?:上下文|历史(?:对话|消息)?|conversation|context|transcript)\s*[:：]"
)
EDIT_PROMPT_MAX_CHARS = 4000
REFERENCE_MAX_DIMENSION = 2048
REFERENCE_MAX_IMAGE_BYTES = 1024 * 1024
REFERENCE_MAX_COUNT = 15
REFERENCE_MAX_TOTAL_BYTES = REFERENCE_MAX_COUNT * REFERENCE_MAX_IMAGE_BYTES
REFERENCE_MIN_DIMENSION = 768
# Fifteen 1 MiB inputs become about 40 MiB in the dual-Base64 Chat format.
REQUEST_MAX_BYTES = 48 * 1024 * 1024
IMAGE_URL_KEYS = {
    "url", "uri", "image_url", "image_uri", "file_url", "fileurl",
    "imageurl", "output_image", "outputimage", "generated_image", "generatedimage", "image",
}
BASE64_KEYS = {"b64_json", "base64", "image_base64", "imagebase64", "b64"}
IMAGE_CONTAINER_KEYS = {
    "data", "images", "output", "outputs", "result", "results", "choices",
    "message", "messages", "content", "parts", "attachments", "artifacts",
    "generated_images", "generatedimages", "output_images", "outputimages",
    "text", "output_text", "candidates", "source", "inline_data", "inlinedata",
}
CORRECTION_INTENT_RE = re.compile(
    r"不满意|画面不对|刚才|上一张|上张|这张图|这幅图|继续修改|继续改|"
    r"把.+改成|换成|改一下|去掉|移除|"
    r"previous\s+image|last\s+image|just\s+generated|this\s+image|"
    r"edit\s+it|change\s+only|keep\s+everything\s+else",
    re.I,
)


class ClientError(RuntimeError):
    """Expected user-facing client failure."""


class RefusalError(ClientError):
    """An explicit provider refusal is not an image result or a retry signal."""


class ReferenceBudgetError(ClientError):
    """A reference cannot reach a byte budget without excessive detail loss."""


class CredentialConfirmationRequired(ClientError):
    """A local Codex credential needs explicit confirmation before use."""

    def __init__(self, sources: list[str], *, ccswitch_running: bool) -> None:
        self.payload = {
            "credential_confirmation_required": True,
            "ccswitch_running": ccswitch_running,
            "codex_credential_sources": sources,
            "recommended_action": (
                "Confirm that the Codex/Codex++ credential is a Keylink API key, "
                "then retry with --confirm-codex-auth."
            ),
        }
        context = (
            "CCSwitch is running but has no usable Keylink provider key. "
            if ccswitch_running else "CCSwitch is not detected as running. "
        )
        super().__init__(
            context + "No Codex account login was detected. An API key was found in the "
            "Codex/Codex++ configuration or OPENAI_API_KEY. "
            "Confirm that it is a Keylink API key before using it."
        )


class CodexAccountCredentialError(ClientError):
    """Account sign-in cannot supply a third-party API key."""

    def __init__(self, *, ccswitch_running: bool) -> None:
        self.payload = {
            "codex_account_detected": True,
            "manual_key_required": True,
            "ccswitch_running": ccswitch_running,
        }
        context = (
            "CCSwitch is running but no usable Keylink provider key was found. "
            if ccswitch_running else "CCSwitch is not running. "
        )
        super().__init__(
            "Codex/Codex++ account login detected. " + context
            + "Manually configure and save the Keylink provider in CCSwitch "
            "(add it to aggregation when enabled), "
            "or set KEYLINK_API_KEY. Account auth.json and OPENAI_API_KEY will not be used; "
            "--confirm-codex-auth cannot override account login."
        )


class CCSwitchDetectionRequired(ClientError):
    """No matching saved key is available and live state cannot be checked."""

    def __init__(self) -> None:
        self.payload = {
            "ccswitch_detection_required": True,
            "ccswitch_running": None,
            "requires_host_execution": True,
            "recommended_action": (
                "Retry the same command with host process/network permissions through the "
                "client's approval flow. Do not change credentials or assume CCSwitch is stopped."
            ),
        }
        super().__init__(
            "CCSwitch running state could not be checked: process enumeration or the local "
            "listener probe was denied, unavailable, or timed out. Retry with approved host "
            "execution; no matching saved Keylink credential was available, "
            "no credential was selected and no API request was sent."
        )


class EndpointError(ClientError):
    def __init__(self, endpoint: str, message: str):
        super().__init__(message)
        self.endpoint = endpoint


class APIError(ClientError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ImageCandidate:
    kind: str
    value: str
    mime_type: str | None = None


@dataclass(frozen=True)
class ReferencePreparation:
    paths: list[Path]
    items: list[dict[str, Any]]
    warnings: list[str]
    original_bytes: int
    prepared_bytes: int


@dataclass
class CCSwitchState:
    proxy_base_url: str | None = None
    provider_base_urls: list[str] | None = None
    api_key: str | None = None
    running: bool | None = None
    credential_source: str = "ccswitch-current-provider"
    credential_error: str | None = None
    aggregation: bool = False

    def __post_init__(self) -> None:
        if self.provider_base_urls is None:
            self.provider_base_urls = []


def emit(payload: dict[str, Any], *, stream: Any = None) -> None:
    if stream is None:
        stream = sys.stdout
    json.dump(payload, stream, ensure_ascii=False, indent=2)
    stream.write("\n")


def read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}-{uuid.uuid4().hex}.tmp"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def recursive_lookup(mapping: Any, accepted_keys: set[str]) -> str | None:
    if not isinstance(mapping, dict):
        return None
    for key, value in mapping.items():
        if key.upper() in accepted_keys and isinstance(value, str) and value.strip():
            return value.strip()
    for value in mapping.values():
        found = recursive_lookup(value, accepted_keys)
        if found:
            return found
    return None


def user_home() -> Path:
    try:
        return Path.home()
    except RuntimeError:
        return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".").expanduser()


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else user_home() / ".codex"


def base_url_from_codex_config(config: Any) -> str | None:
    if isinstance(config, str):
        try:
            config = tomllib.loads(config)
        except tomllib.TOMLDecodeError:
            return None
    if not isinstance(config, dict):
        return None

    provider_name = config.get("model_provider")
    providers = config.get("model_providers")
    if isinstance(provider_name, str) and isinstance(providers, dict):
        provider = providers.get(provider_name)
        if isinstance(provider, dict):
            value = provider.get("base_url")
            if isinstance(value, str) and value.strip():
                return value.strip()

    value = config.get("base_url")
    return value.strip() if isinstance(value, str) and value.strip() else None


def discover_codex_base_url() -> str | None:
    try:
        config = (codex_home() / "config.toml").read_text(encoding="utf-8-sig")
    except OSError:
        return None
    return base_url_from_codex_config(config)


def codex_credential_paths() -> list[Path]:
    paths: list[Path] = []
    explicit_names = (
        "KEYLINK_CODEX_AUTH_FILE",
        "CODEX_PLUS_AUTH_FILE",
        "CODEXPP_AUTH_FILE",
        "KEYLINK_CODEX_CONFIG",
        "CODEX_PLUS_CONFIG",
        "CODEXPP_CONFIG",
    )
    for name in explicit_names:
        value = os.environ.get(name)
        if value:
            paths.append(Path(value).expanduser())

    home = user_home()
    roots = [
        codex_home(),
        home / ".codex++",
        home / ".codex-plus",
        home / ".codexpp",
        home / ".codex_plus",
    ]
    for name in ("KEYLINK_CODEX_CONFIG_DIR", "CODEX_PLUS_HOME", "CODEXPP_HOME"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value).expanduser())
    for parent_name in ("APPDATA", "LOCALAPPDATA"):
        parent = os.environ.get(parent_name)
        if parent:
            for name in ("Codex++", "Codex Plus", "CodexPlus", "codex-plus", "codexpp"):
                roots.append(Path(parent) / name)
    for root in roots:
        for name in ("auth.json", "config.json", "config.toml"):
            paths.append(root / name)

    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.expanduser().resolve()).casefold()
        if key not in seen:
            seen.add(key)
            result.append(path.expanduser())
    return result


def read_codex_config(path: Path) -> Any:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    if path.suffix.lower() == ".toml":
        try:
            return tomllib.loads(raw)
        except tomllib.TOMLDecodeError:
            return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def discover_codex_credentials() -> list[tuple[Path, str]]:
    results: list[tuple[Path, str]] = []
    for path in codex_credential_paths():
        if not path.is_file():
            continue
        data = read_codex_config(path)
        key = usable_api_key(data)
        if key:
            results.append((path.resolve(), key))
    return results


def has_codex_account_login(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    mode = str(data.get("auth_mode", "")).casefold().replace("_", "")
    if mode.startswith("chatgpt"):
        return True
    tokens = data.get("tokens")
    if isinstance(tokens, dict) and any(
        isinstance(tokens.get(key), str) and tokens[key].strip()
        for key in ("access_token", "refresh_token", "id_token")
    ):
        return True
    return any(has_codex_account_login(value) for value in data.values() if isinstance(value, dict))


def discover_codex_account_login() -> bool:
    # Inspect only login markers, never turn access/refresh tokens into API keys.
    return any(
        has_codex_account_login(read_codex_config(path))
        for path in codex_credential_paths() if path.is_file()
    )


def usable_api_key(data: Any) -> str | None:
    if not isinstance(data, dict) or has_codex_account_login(data):
        return None
    accepted = {"OPENAI_API_KEY", "KEYLINK_API_KEY", "API_KEY", "APIKEY"}
    for name, value in data.items():
        if name.upper() in accepted and isinstance(value, str):
            value = value.strip()
            if value and value.upper() != "PROXY_MANAGED":
                return value
    for value in data.values():
        found = usable_api_key(value)
        if found:
            return found
    return None


def discover_codex_api_key(
    *, confirm: bool = False, ccswitch_running: bool = False,
) -> tuple[str | None, str | None]:
    credentials = discover_codex_credentials()
    if not credentials:
        return None, None
    path, key = credentials[0]
    if not confirm:
        raise CredentialConfirmationRequired(
            [str(item[0]) for item in credentials], ccswitch_running=ccswitch_running
        )
    label = "codex++-config" if "++" in str(path) or "plus" in str(path).lower() else "codex-auth"
    return key, label


def local_endpoint_is_running(url: str | None) -> bool | None:
    if not url:
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return False
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=0.25):
            return True
    except ValueError:
        return False
    except OSError as error:
        if error.errno == errno.ECONNREFUSED or getattr(error, "winerror", None) == 10061:
            return False
        # Access denied (including WSAEACCES/10013) and timeouts cannot prove
        # that the listener is absent. Keep the state unknown for an approved retry.
        return None


def ccswitch_process_is_running() -> bool | None:
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="mbcs", errors="replace", timeout=1, check=False,
            )
            if completed.returncode != 0:
                return None
            text = completed.stdout.casefold()
        else:
            completed = subprocess.run(
                ["pgrep", "-afil", "cc[-_ ]?switch|ccswitch"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", timeout=1, check=False,
            )
            if completed.returncode == 1:  # pgrep: successful query, no matches
                return False
            if completed.returncode != 0:
                return None
            text = completed.stdout.casefold()
        return any(name in text for name in ("cc-switch", "ccswitch", "cc switch"))
    except (OSError, subprocess.SubprocessError):
        return None


def ccswitch_rows(connection: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    # Optional tables/columns differ across versions. Failure in proxy discovery
    # must not prevent reading provider credentials from an older database.
    try:
        return [dict(row) for row in connection.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []


def json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def discover_ccswitch_state(base_url: str = DEFAULT_BASE_URL) -> CCSwitchState:
    state = CCSwitchState()
    directory = user_home() / ".cc-switch"
    live = json_mapping(read_codex_config(directory / "live-state.json"))
    live = json_mapping(json_mapping(live.get("apps")).get("codex"))
    device_settings = json_mapping(read_codex_config(directory / "settings.json"))
    stack = json_mapping(live.get("stack"))
    state.aggregation = live.get("mode") == "proxy" and stack.get("enabled") is True
    db_path = directory / "cc-switch.db"
    if db_path.is_file():
        connection: sqlite3.Connection | None = None
        try:
            uri = db_path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=1)
            connection.row_factory = sqlite3.Row

            proxies = ccswitch_rows(connection,
                "SELECT * "
                "FROM proxy_config WHERE app_type = 'codex'"
            )
            proxy = proxies[0] if proxies else {}
            if proxy.get("proxy_enabled") or proxy.get("enabled"):
                host = str(proxy.get("listen_address") or "127.0.0.1")
                try:
                    port = int(proxy.get("listen_port") or 15721)
                except (TypeError, ValueError):
                    port = 15721
                if host in {"0.0.0.0", "::"}:
                    host = "127.0.0.1"
                elif ":" in host and not host.startswith("["):
                    host = f"[{host}]"
                state.proxy_base_url = f"http://{host}:{port}"

            providers = ccswitch_rows(connection, "SELECT * FROM providers WHERE app_type = 'codex'")
            current_id = (
                live.get("proxy_route") if live.get("mode") == "proxy"
                else device_settings.get("currentProviderCodex")
            )
            if not current_id:
                current_id = device_settings.get("currentProviderCodex")
            if not current_id:
                current = sorted(
                    (p for p in providers if p.get("is_current")),
                    key=lambda p: (p.get("sort_index") or 0, str(p.get("id", ""))),
                )
                current_id = current[0].get("id") if current else None
            members = stack.get("members")
            member_ids = {m for m in members if isinstance(m, str)} if isinstance(members, list) else set()
            candidates: list[tuple[str, str, list[str]]] = []
            for provider in providers:
                provider_id = provider.get("id")
                if provider_id != current_id and not (state.aggregation and provider_id in member_ids):
                    continue
                settings = json_mapping(provider.get("settings_config"))
                key = usable_api_key(settings)
                if not key:
                    continue
                endpoints = ccswitch_rows(connection,
                    "SELECT url FROM provider_endpoints WHERE app_type = 'codex' AND provider_id = ?",
                    (provider_id,),
                )
                urls = [row["url"].strip() for row in endpoints if isinstance(row.get("url"), str) and row["url"].strip()]
                provider_url = base_url_from_codex_config(settings.get("config")) or base_url_from_codex_config(settings)
                if provider_url:
                    urls.insert(0, provider_url)
                website = provider.get("website_url")
                remote_hosts = {normalized_host(url) for url in urls if normalized_host(url) and not is_loopback(url)}
                if not remote_hosts and isinstance(website, str) and website.strip():
                    remote_hosts.add(normalized_host(website))
                if isinstance(website, str) and website.strip():
                    urls.append(website.strip())
                target_match = normalized_host(base_url) in remote_hosts
                # Legacy selected providers may only have a local forwarding URL.
                legacy_local = (
                    not state.aggregation and not remote_hosts and provider_id == current_id
                    and normalized_host(base_url) == normalized_host(DEFAULT_BASE_URL)
                )
                if target_match or legacy_local:
                    candidates.append((provider_id, key, urls))
            selected = next((p for p in candidates if p[0] == current_id), None)
            if selected is None and len({p[1] for p in candidates}) == 1:
                selected = candidates[0]
            if selected:
                state.api_key = selected[1]
                state.provider_base_urls = selected[2]
                if state.aggregation:
                    state.credential_source = "ccswitch-aggregation-provider"
            elif candidates:
                state.credential_error = (
                    "Multiple CCSwitch aggregation members match the Keylink API host with different keys. "
                    "Select the intended Keylink provider as the CCSwitch default or set KEYLINK_API_KEY."
                )
        except (sqlite3.Error, OSError, ValueError):
            pass
        finally:
            if connection is not None:
                connection.close()

    state.provider_base_urls = list(dict.fromkeys(state.provider_base_urls))
    listener = local_endpoint_is_running(state.proxy_base_url)
    process = ccswitch_process_is_running() if listener is not True else None
    if listener is True or process is True:
        state.running = True
    elif listener is None or process is None:
        state.running = None
    else:
        state.running = False
    return state


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise ClientError("The API base URL is empty.")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ClientError(f"Invalid API base URL: {value}")

    path = parsed.path.rstrip("/")
    endpoint_suffixes = (
        "/v1/chat/completions",
        "/v1/images/generations",
        "/v1/images/edits",
        "/v1/models",
    )
    for suffix in endpoint_suffixes:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if path.endswith("/v1"):
        path = path[:-3]

    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path.rstrip("/"), "", "")
    ).rstrip("/")


def endpoint_url(base_url: str, endpoint_path: str) -> str:
    return f"{normalize_base_url(base_url)}{endpoint_path}"


def discover_base_url(explicit: str | None) -> tuple[str, str, CCSwitchState]:
    if explicit:
        base_url, source = normalize_base_url(explicit), "argument"
    elif os.environ.get("KEYLINK_BASE_URL"):
        base_url, source = normalize_base_url(os.environ["KEYLINK_BASE_URL"]), "environment:KEYLINK_BASE_URL"
    else:
        base_url, source = DEFAULT_BASE_URL, "default"
    return base_url, source, discover_ccswitch_state(base_url)


def normalized_host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_loopback(url: str) -> bool:
    return normalized_host(url) in {"127.0.0.1", "localhost", "::1"}


def discover_api_key(
    base_url: str, base_source: str, ccswitch: CCSwitchState,
    *, confirm_codex_auth: bool = False,
) -> tuple[str | None, str | None]:
    value = os.environ.get("KEYLINK_API_KEY")
    if value and value.strip():
        if value.strip().upper() == "PROXY_MANAGED":
            raise ClientError("KEYLINK_API_KEY contains a CCSwitch placeholder. Configure a real Keylink API key.")
        return value.strip(), "environment:KEYLINK_API_KEY"

    # Saved provider/member selection is independent of the UI process. Require
    # a matching remote host before using a saved key without a running probe.
    # This never treats a saved database as evidence that CCSwitch is running.
    if ccswitch.credential_error:
        raise ClientError(ccswitch.credential_error)
    provider_hosts = {
        normalized_host(url) for url in ccswitch.provider_base_urls
        if normalized_host(url) and not is_loopback(url)
    }
    selected_host_matches = normalized_host(base_url) in provider_hosts
    legacy_running_match = (
        ccswitch.running is True and not provider_hosts
        and normalized_host(base_url) == normalized_host(DEFAULT_BASE_URL)
    )
    if ccswitch.api_key and (selected_host_matches or legacy_running_match):
        source = ccswitch.credential_source
        if ccswitch.running is not True:
            source = "ccswitch-saved-aggregation-provider" if ccswitch.aggregation else "ccswitch-saved-provider"
        return ccswitch.api_key, source

    if ccswitch.running is None:
        raise CCSwitchDetectionRequired()

    if discover_codex_account_login():
        raise CodexAccountCredentialError(ccswitch_running=bool(ccswitch.running))

    key, source = discover_codex_api_key(
        confirm=confirm_codex_auth, ccswitch_running=bool(ccswitch.running),
    )
    if key:
        return key, source

    value = usable_api_key({"OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY")})
    if value:
        if not confirm_codex_auth:
            raise CredentialConfirmationRequired(
                ["environment:OPENAI_API_KEY"], ccswitch_running=bool(ccswitch.running)
            )
        return value, "environment:OPENAI_API_KEY"

    return None, None


def safe_response_text(raw: bytes, limit: int = 1200) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


class HttpClient:
    def __init__(self, base_url: str, api_key: str | None, timeout: float):
        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key
        self.timeout = timeout
        self.opener = urllib.request.build_opener()
        self.last_request_bytes: int | None = None
        self.request_attempts: list[dict[str, Any]] = []

    def headers(self, *, json_body: bool = False) -> dict[str, str]:
        result = {
            "Accept": "application/json, image/*",
            "User-Agent": "keylink-image-codex-plugin/0.1.0",
        }
        if json_body:
            result["Content-Type"] = "application/json"
        if self.api_key:
            result["Authorization"] = f"Bearer {self.api_key}"
        return result

    def request_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            check_request_budget(body)
        headers = self.headers(json_body=payload is not None)
        if extra_headers:
            headers.update(extra_headers)
        self.last_request_bytes = len(body or b"")
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        raw, content_type = self._open(request)
        return parse_api_response(raw, content_type)

    def request_multipart(
        self,
        url: str,
        fields: dict[str, str],
        image_paths: list[Path],
    ) -> Any:
        body, content_type = encode_multipart(fields, image_paths)
        check_request_budget(body)
        headers = self.headers()
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
        self.last_request_bytes = len(body)
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        raw, content_type = self._open(request)
        return parse_api_response(raw, content_type)

    def download(self, url: str) -> tuple[bytes, str | None]:
        headers = {
            "Accept": "image/*,*/*;q=0.8",
            "User-Agent": "keylink-image-codex-plugin/0.1.0",
        }
        if self.api_key and normalized_host(url) == normalized_host(self.base_url):
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            return self._open(request)
        except ClientError as proxy_error:
            if isinstance(proxy_error.__cause__, urllib.error.HTTPError):
                raise
            proxies = urllib.request.getproxies()
            if not (proxies.get("http") or proxies.get("https")):
                raise
            # Recover the existing image if a system proxy cannot reach its CDN.
            direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                return self._open(request, opener=direct_opener)
            except ClientError as direct_error:
                raise ClientError(
                    f"Image download failed through proxy ({proxy_error}) "
                    f"and directly ({direct_error})."
                ) from direct_error

    def _open(
        self, request: urllib.request.Request, *, opener: Any = None
    ) -> tuple[bytes, str | None]:
        started = time.monotonic()
        record = {
            "endpoint": urllib.parse.urlsplit(request.full_url).path,
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "request_bytes": len(request.data or b""), "http_status": None,
        }
        try:
            with (opener or self.opener).open(request, timeout=self.timeout) as response:
                record["http_status"] = response.status
                content_type = response.headers.get_content_type()
                return response.read(), content_type
        except urllib.error.HTTPError as error:
            record.update(http_status=error.code, error_kind="http_error")
            raw_error = b""
            try:
                raw_error = error.read()
                detail = safe_response_text(raw_error) or error.reason
            except (OSError, http.client.HTTPException) as read_error:
                detail = f"{error.reason} (could not read error body: {read_error})"
            finally:
                error.close()
            try:
                refusal_payload = json.loads(raw_error)
            except (ValueError, TypeError, UnicodeError):
                pass
            else:
                check_provider_refusal(refusal_payload)
            raise APIError(
                f"HTTP {error.code} for {request.full_url}: {detail}", error.code
            ) from error
        except urllib.error.URLError as error:
            record["error_kind"] = "transport_error"
            raise ClientError(f"Request failed for {request.full_url}: {error.reason}") from error
        except (OSError, http.client.HTTPException) as error:
            record["error_kind"] = "transport_error"
            raise ClientError(f"Request failed for {request.full_url}: {error}") from error
        finally:
            if request.get_method() == "POST":
                record["elapsed_seconds"] = round(time.monotonic() - started, 3)
                self.request_attempts.append(record)


def check_request_budget(body: bytes) -> None:
    if len(body) > REQUEST_MAX_BYTES:
        raise ClientError(f"Request body is {len(body)} bytes, exceeding the client budget of {REQUEST_MAX_BYTES} bytes. Reduce references or prompt; no request was sent.")


def parse_api_response(raw: bytes, content_type: str | None) -> Any:
    if detect_mime_type(raw):
        return {"b64_json": base64.b64encode(raw).decode("ascii")}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        text = raw.decode("utf-8", errors="replace").strip()
        if content_type in {"text/plain", "text/markdown"} and extract_image_candidates(text):
            return {"content": text}
        raise ClientError("The endpoint returned non-JSON content without a recognized image response.") from error
    validate_api_payload(payload)
    return payload


def validate_api_payload(payload: Any) -> None:
    """Validate decoded responses, including JSON embedded in Chat text."""
    check_provider_refusal(payload)
    if isinstance(payload, dict):
        nested = payload.get("error")
        code = payload.get("status_code", payload.get("code"))
        if isinstance(nested, dict):
            code = nested.get("status_code", nested.get("code", code))
        try:
            status = int(code)
        except (ValueError, TypeError):
            status = None
        if nested or (status is not None and status >= 400):
            detail = safe_response_text(json.dumps(nested or payload, ensure_ascii=False).encode("utf-8"))
            raise APIError(f"API error{f' {status}' if status else ''}: {detail}", status)


def check_provider_refusal(payload: Any) -> None:
    """Recognize explicit structured refusal signals, not prompt wording."""
    def visit(value: Any, depth: int = 0) -> bool:
        if depth > 16:
            return False
        if isinstance(value, list):
            return any(visit(item, depth + 1) for item in value)
        if not isinstance(value, dict):
            return False
        if str(value.get("role", "")).lower() in {"user", "system", "developer", "tool"}:
            return False
        for key, nested in value.items():
            field = str(key).lower()
            if field in {"input", "prompt", "revised_prompt", "metadata", "usage"}:
                continue
            if field == "refusal" and nested:
                return True
            if field in {"finish_reason", "finishreason", "block_reason", "blockreason", "code", "type"}:
                if str(nested).lower() in {
                    "content_filter", "content_policy_violation", "safety", "safety_violation",
                    "prohibited_content", "blocklist", "moderation_blocked", "refusal",
                }:
                    return True
            if isinstance(nested, (dict, list)) and visit(nested, depth + 1):
                return True
        return False

    if visit(payload):
        raise RefusalError("The provider explicitly refused this request. No new image was generated; previous images must not be presented as its result.")


def encode_multipart(
    fields: dict[str, str], image_paths: list[Path]
) -> tuple[bytes, str]:
    boundary = f"----keylink-{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "ascii"
                ),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    for image_path in image_paths:
        mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", image_path.name) or "image"
        try:
            image_bytes = image_path.read_bytes()
        except OSError as error:
            raise ClientError(f"Unable to read reference image {image_path}: {error}") from error
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                (
                    f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
                ).encode("ascii"),
                f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"),
                image_bytes,
                b"\r\n",
            ]
        )

    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def image_to_data_url(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ClientError(f"Unable to read reference image {path}: {error}") from error
    mime_type = mimetypes.guess_type(path.name)[0] or detect_mime_type(raw)
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "application/octet-stream"
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def parse_size(size: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{2,5})[xX](\d{2,5})", size.strip())
    if not match:
        raise ClientError(f"Invalid size '{size}'. Expected WIDTHxHEIGHT.")
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        raise ClientError("Image dimensions must be positive.")
    return width, height


def choose_size(explicit_size: str | None, aspect: str | None) -> str:
    if explicit_size:
        width, height = parse_size(explicit_size)
        return f"{width}x{height}"
    normalized = (aspect or "square").lower().replace(" ", "")
    if normalized in {"portrait", "vertical", "9:16", "2:3", "3:4"}:
        return "1024x1536"
    if normalized in {"landscape", "horizontal", "16:9", "3:2", "4:3"}:
        return "1536x1024"
    return "1024x1024"


def has_high_resolution_intent(prompt: str) -> bool:
    if HIGH_RES_INTENT_RE.search(prompt):
        return True
    for match in SIZE_RE.finditer(prompt):
        width, height = int(match.group(1)), int(match.group(2))
        if width > 1536 or height > 1536:
            return True
    return False


def has_correction_intent(prompt: str) -> bool:
    return bool(CORRECTION_INTENT_RE.search(prompt))


def extract_current_prompt(prompt: str) -> str:
    """Unwrap explicit transcripts only; never guess which visual details matter."""
    value = prompt.strip()
    fenced = re.fullmatch(r"```(?:json|text|markdown)?\s*\n(.*?)\n```", value, re.S | re.I)
    if fenced:
        value = fenced.group(1).strip()
    try:
        structured = json.loads(value)
    except (ValueError, TypeError):
        structured = None
    if isinstance(structured, dict):
        messages = structured.get("messages")
        if isinstance(messages, list):
            users = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
            if users:
                content = users[-1].get("content")
                if isinstance(content, list):
                    content = "\n".join(p["text"] for p in content
                                        if isinstance(p, dict) and isinstance(p.get("text"), str))
                if isinstance(content, str):
                    value = content.strip()
        elif isinstance(structured.get("prompt"), str):
            value = structured["prompt"].strip()
    # XML conversation wrappers are common in copied Codex context.
    users = list(re.finditer(r"<user(?:\s[^>]*)?>(.*?)</user>", value, re.S | re.I))
    if users:
        value = users[-1].group(1).strip()
    roles = list(EDIT_ROLE_LINE_RE.finditer(value))
    user_roles = [m for m in roles if re.search(r"(?:user|用户)\s*[:：]", m.group(), re.I)]
    if user_roles and roles[0].start() == 0:
        last = user_roles[-1]
        end = next((m.start() for m in roles if m.start() > last.start()), len(value))
        value = value[last.end():end].strip()
    markers = list(EDIT_PROMPT_MARKER_RE.finditer(value))
    if markers:
        value = value[markers[-1].end():].strip()
    return value


def sanitize_edit_prompt(prompt: str, *, preserve_text: bool = False) -> tuple[str, dict[str, Any]]:
    value = extract_current_prompt(prompt)
    # Remove explicitly tagged non-prompt material, not arbitrary prose or code
    # the user may want depicted in the image.
    value = re.sub(
        r"<(context|history|system|developer|tool_result|environment_context)(?:\s[^>]*)?>"
        r".*?</\1>", "", value, flags=re.S | re.I,
    ).strip()
    if preserve_text:
        if value != prompt.strip():
            raise ClientError(
                "The selected prompt mode requires a clean final edit prompt. "
                "Conversation wrappers would change its text. Supply only the visual "
                "instructions; no prompt was rewritten and no request was sent."
            )
        value = prompt  # Preserve even whitespace in an explicitly prepared prompt.
    else:
        value = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", value)
    if not value:
        raise ClientError("The edit prompt is empty after removing conversation wrappers. Supply only the requested changes; no request was sent.")
    if EDIT_ROLE_LINE_RE.search(value) or EDIT_CONTEXT_LINE_RE.search(value):
        raise ClientError("The edit prompt still contains conversation/context sections. Supply only the current edit instructions; no request was sent.")
    if len(value) > EDIT_PROMPT_MAX_CHARS:
        raise ClientError(
            f"Edit prompt has {len(value)} characters after cleanup (client limit {EDIT_PROMPT_MAX_CHARS}). "
            "Summarize the current changes while preserving required text and constraints; no request was sent."
        )
    if "data:image/" in value.lower():
        raise ClientError("Pass reference images through --image, not Base64 inside the edit prompt; no request was sent.")
    return value, {
        "compacted": False if preserve_text else value != prompt.strip(), "original_chars": len(prompt),
        "sent_chars": len(value), "sent_bytes": len(value.encode("utf-8")),
    }


def extract_sizes(value: Any) -> list[str]:
    found: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, str):
            for match in SIZE_RE.finditer(item):
                found.add(f"{int(match.group(1))}x{int(match.group(2))}")
        elif isinstance(item, dict):
            for nested in item.values():
                walk(nested)
        elif isinstance(item, list):
            for nested in item:
                walk(nested)

    walk(value)
    preferred_order = {size: index for index, size in enumerate(CONSERVATIVE_SIZES)}

    def sort_key(size: str) -> tuple[int, int, int]:
        width, height = parse_size(size)
        return (preferred_order.get(size, len(preferred_order)), width * height, width)

    return sorted(found, key=sort_key)


def model_id_from_entry(entry: Any) -> str | None:
    if isinstance(entry, str) and entry.strip():
        return entry.strip()
    if not isinstance(entry, dict):
        return None
    for key in ("id", "model", "name"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def model_entries(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "models", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def is_image_model(model_id: str, entry: Any = None) -> bool:
    lowered = model_id.lower()
    if model_id in DEFAULT_MODEL_PRIORITY:
        return True
    if "image" in lowered or "imagen" in lowered:
        return True
    if isinstance(entry, dict):
        modality_text = json.dumps(
            {
                key: entry.get(key)
                for key in ("type", "modality", "modalities", "capabilities")
                if key in entry
            },
            ensure_ascii=False,
        ).lower()
        return "image" in modality_text
    return False


def format_model_catalog(payload: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for entry in model_entries(payload):
        model_id = model_id_from_entry(entry)
        if not model_id:
            continue
        result.append(
            {
                "id": model_id,
                "image_candidate": is_image_model(model_id, entry),
                "published_sizes": extract_sizes(entry),
                "reference_image_limits": published_reference_limits(entry),
            }
        )
    return result


def published_reference_limits(entry: Any) -> dict[str, int]:
    """Only explicit input-image limits count; output sizes are unrelated."""
    if not isinstance(entry, dict):
        return {}
    limits: dict[str, int] = {}
    for container in (entry, entry.get("capabilities")):
        if not isinstance(container, dict):
            continue
        for key in ("reference_image_limits", "input_image_limits"):
            source = container.get(key)
            if isinstance(source, dict):
                for field in ("max_bytes", "max_total_bytes", "max_width", "max_height", "max_images"):
                    value = source.get(field)
                    if type(value) is int and value > 0:
                        limits[field] = min(limits.get(field, value), value)
    return limits


def endpoint_order(_model: str, endpoint_mode: str) -> list[str]:
    if endpoint_mode in {"images", "chat"}:
        return [endpoint_mode]
    if endpoint_mode == "custom":
        return ["custom"]
    return ["images", "chat"]


def resolve_thread_id(explicit: str | None) -> str:
    raw = explicit
    if not raw:
        raw = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    if not raw:
        workspace = os.path.normcase(str(Path.cwd().resolve()))
        raw = "workspace-" + hashlib.sha256(workspace.encode()).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", raw).strip("-.")
    return safe or "default"


def thread_state_dir(thread_id: str) -> Path:
    configured = os.environ.get("KEYLINK_IMAGE_STATE_DIR")
    root = Path(configured).expanduser() if configured else Path.cwd() / ".keylink-image" / "threads"
    return root.resolve() / thread_id


def load_last_image(state_dir: Path) -> Path:
    data = read_json_file(state_dir / "last.json")
    raw_path = data.get("path") if isinstance(data, dict) else None
    if not isinstance(raw_path, str) or not raw_path:
        raise ClientError("No successful image is saved for this Codex task.")
    path = Path(raw_path)
    if not path.is_file():
        raise ClientError(f"The saved latest image no longer exists: {path}")
    return path.resolve()


def save_model_selection(
    state_dir: Path,
    catalog: list[dict[str, Any]],
    base_url: str,
) -> str:
    if not catalog:
        raise ClientError("GET /v1/models returned an empty model catalog.")
    state_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    payload = {
        "token": token,
        "base_url": normalize_base_url(base_url),
        "models": catalog,
        "issued_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "used_at": None,
    }
    with image_state_lock(state_dir):
        write_json_file(state_dir / "model-selection.json", payload)
    return token


def validate_model_selection(
    state_dir: Path,
    selection_token: str,
    model: str,
    base_url: str,
) -> dict[str, Any]:
    target = state_dir / "model-selection.json"
    data = read_json_file(target)
    if not isinstance(data, dict):
        raise ClientError(
            "No current model selection was found. Run models, show the available image "
            "models to the user, wait for their choice, then pass --selection-token."
        )
    if data.get("token") != selection_token:
        raise ClientError(
            "The model selection token is invalid or has been replaced. Run models again "
            "and wait for the user to choose a model."
        )
    if data.get("used_at"):
        raise ClientError(
            "The model selection token has already been used. Run models again before "
            "another generation or edit."
        )
    if normalize_base_url(str(data.get("base_url", ""))) != normalize_base_url(base_url):
        raise ClientError(
            "The model selection belongs to a different API address. Run models against "
            "the current address and ask the user to choose again."
        )
    catalog = data.get("models")
    available = (
        {
            str(entry.get("id"))
            for entry in catalog
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        }
        if isinstance(catalog, list)
        else set()
    )
    if model not in available:
        raise ClientError(
            f"Model '{model}' was not present in the latest GET /v1/models response. "
            "Run models again and ask the user to choose one of the listed models."
        )
    return data


def consume_model_selection(
    state_dir: Path,
    selection_token: str,
    model: str,
    base_url: str,
) -> None:
    with image_state_lock(state_dir):
        data = validate_model_selection(state_dir, selection_token, model, base_url)
        data["used_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        write_json_file(state_dir / "model-selection.json", data)
        _record_image_request_unlocked(state_dir, selection_token, model)


def image_request_id(selection_token: str) -> str:
    return hashlib.sha256(selection_token.encode("utf-8")).hexdigest()[:24]


@contextlib.contextmanager
def image_state_lock(state_dir: Path) -> Iterable[None]:
    """Serialize state updates; OS locks are released if a worker is killed."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / ".image-state.lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if not stream.tell():
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            acquire = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        for attempt in range(100):
            try:
                acquire()
                break
            except OSError:
                if attempt == 99:
                    raise ClientError("Image state is busy; wait for the active task and check its status.")
                time.sleep(0.05)
        try:
            yield
        finally:
            release()


def record_image_request(state_dir: Path, token: str, model: str, *, job_id: str | None = None) -> None:
    with image_state_lock(state_dir):
        _record_image_request_unlocked(state_dir, token, model, job_id=job_id)


def _record_image_request_unlocked(state_dir: Path, token: str, model: str, *, job_id: str | None = None) -> None:
    # Caller holds image_state_lock, including when consuming a selection.
    identifier = image_request_id(token)
    previous = read_json_file(state_dir / "latest-request.json")
    record = previous if isinstance(previous, dict) and previous.get("request_id") == identifier else {
        "request_id": identifier, "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    record.update(status="running", model=model)
    record.pop("finished_at", None)
    if job_id:
        record["job_id"] = job_id
    write_json_file(state_dir / "latest-request.json", record)


def finish_image_request(state_dir: Path, token: str, status: str) -> None:
    with image_state_lock(state_dir):
        record = read_json_file(state_dir / "latest-request.json")
        if isinstance(record, dict) and record.get("request_id") == image_request_id(token):
            record.update(status=status, finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
            write_json_file(state_dir / "latest-request.json", record)


def latest_image_request(state_dir: Path) -> dict[str, Any] | None:
    record = read_json_file(state_dir / "latest-request.json")
    candidates = [record] if isinstance(record, dict) else []
    # Recover enough provenance for tasks created before latest-request.json existed.
    selection = read_json_file(state_dir / "model-selection.json")
    if (isinstance(selection, dict) and selection.get("used_at")
            and (not isinstance(record, dict)
                 or record.get("request_id") != image_request_id(str(selection.get("token", ""))))):
        candidates.append({"started_at": selection["used_at"], "status": "unknown",
                           "request_id": image_request_id(str(selection.get("token", ""))),
                           "model": selection.get("model")})
    latest_job = read_json_file(state_dir / "latest-job.json")
    if isinstance(latest_job, dict) and latest_job.get("job_id"):
        job = read_json_file(job_directory(state_dir, latest_job["job_id"]) / "job.json")
        if isinstance(job, dict) and job.get("started_at"):
            if isinstance(record, dict) and record.get("job_id") == latest_job["job_id"]:
                # A worker can fail before reaching API submission, or stop abruptly.
                if job.get("status") == "failed" and record.get("status") != "refused":
                    record["status"] = "failed"
            else:
                candidates.append(job)
    return max(candidates, key=lambda item: timestamp_value(item.get("started_at")), default=None)


def timestamp_value(value: Any) -> float:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or dt.timezone.utc).timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return float("-inf")


def matches_latest_request(latest: dict[str, Any] | None, *, request_id: str | None = None,
                           job_id: str | None = None, timestamp: Any = None) -> bool:
    if not latest:
        return True
    if request_id and latest.get("request_id"):
        return request_id == latest["request_id"]
    if job_id and latest.get("job_id"):
        return job_id == latest["job_id"]
    return (timestamp_value(timestamp) != float("-inf")
            and timestamp_value(timestamp) >= timestamp_value(latest.get("started_at")))


def validate_reference_paths(paths: Iterable[str]) -> list[Path]:
    result: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise ClientError(f"Reference image does not exist: {path}")
        result.append(path)
    return result


def prepare_reference_images(
    paths: list[Path], destination: Path, published: dict[str, int] | None = None,
    *, snapshot: bool = False,
) -> ReferencePreparation:
    limits = published or {}
    max_count = min(REFERENCE_MAX_COUNT, limits.get("max_images", REFERENCE_MAX_COUNT))
    if len(paths) > max_count:
        raise ClientError(f"Too many reference images ({len(paths)}); current limit is {max_count}. Choose fewer references; none were discarded.")
    total_budget = min(REFERENCE_MAX_TOTAL_BYTES, limits.get("max_total_bytes", REFERENCE_MAX_TOTAL_BYTES))
    budget = min(REFERENCE_MAX_IMAGE_BYTES, limits.get("max_bytes", REFERENCE_MAX_IMAGE_BYTES), total_budget)
    bounds = (min(REFERENCE_MAX_DIMENSION, limits.get("max_width", REFERENCE_MAX_DIMENSION)),
              min(REFERENCE_MAX_DIMENSION, limits.get("max_height", REFERENCE_MAX_DIMENSION)))
    prepared: list[Path] = []
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, path in enumerate(paths):
        try:
            if path.stat().st_size > 128 * 1024 * 1024:
                raise ClientError(f"Reference {index + 1} exceeds the 128 MiB local processing limit. Export a smaller source image first.")
            raw = path.read_bytes()
            mime = detect_mime_type(raw)
            dimensions = detect_dimensions(raw, mime)
            original_dimensions = dimensions
            oriented_dimensions = dimensions
            original_bytes = len(raw)
            original_raw = raw
            try:
                from PIL import Image, ImageOps
            except ImportError:
                Image = None
            if Image is None:
                if (mime not in {"image/png", "image/jpeg", "image/webp"} or not dimensions
                        or dimensions[0] <= 0 or dimensions[1] <= 0
                        or dimensions[0] > bounds[0] or dimensions[1] > bounds[1]
                        or len(raw) > budget):
                    raise ClientError("Reference image needs validation/resizing/compression. Install Pillow in the Python runtime used by this skill and retry; no request was sent.")
            else:
                try:
                    source = Image.open(path)
                except Image.DecompressionBombError as error:
                    raise ClientError("Reference has too many pixels to process safely. Export a smaller source image first.") from error
                with source:
                    original_dimensions = source.size
                    if getattr(source, "n_frames", 1) > 1:
                        raise ClientError("Animated references are not supported. Export the intended frame as PNG/JPEG first.")
                    source.verify()
                with Image.open(path) as source:
                    orientation = source.getexif().get(274, 1)
                    oriented_dimensions = (source.height, source.width) if orientation in {5, 6, 7, 8} else source.size
                    needs_change = (mime not in {"image/png", "image/jpeg", "image/webp"}
                                    or source.width > bounds[0] or source.height > bounds[1]
                                    or len(raw) > budget or orientation != 1)
                    if needs_change:
                        picture = ImageOps.exif_transpose(source)
                        picture.thumbnail(bounds, Image.Resampling.LANCZOS)
                        alpha = "A" in picture.getbands() or "transparency" in picture.info
                        picture = picture.convert("RGBA" if alpha else "RGB")
                        picture.info.clear()
                        floor = min(max(picture.size), REFERENCE_MIN_DIMENSION)
                        while True:
                            buffer = io.BytesIO()
                            picture.save(buffer, "PNG", optimize=True)
                            raw, mime = buffer.getvalue(), "image/png"
                            if len(raw) > budget and not alpha:
                                for quality in (90, 82, 74):
                                    buffer = io.BytesIO()
                                    picture.save(buffer, "JPEG", quality=quality, optimize=True)
                                    raw, mime = buffer.getvalue(), "image/jpeg"
                                    if len(raw) <= budget:
                                        break
                            if len(raw) <= budget:
                                break
                            if max(picture.size) <= floor:
                                raise ReferenceBudgetError("References cannot fit the request budget without excessive detail loss. Use fewer/smaller references; no request was sent.")
                            edge = max(floor, int(max(picture.size) * 0.8))
                            picture.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                        dimensions = picture.size
                    else:
                        dimensions = source.size
            changed = raw != original_raw
            target = path
            # Use durable task/job storage, never a short-lived TemporaryDirectory.
            if changed or snapshot or mimetypes.guess_type(path.name)[0] != mime:
                folder = destination / str(index + 1)
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / (safe_slug(path.stem) + extension_for_mime(mime))
                target.write_bytes(raw)
            prepared.append(target.resolve())
            items.append({
                "source_path": str(path), "request_path": str(target.resolve()),
                "original_bytes": original_bytes, "prepared_bytes": len(raw),
                "original_dimensions": list(original_dimensions) if original_dimensions else None,
                "original_oriented_dimensions": list(oriented_dimensions) if oriented_dimensions else None,
                "prepared_dimensions": list(dimensions) if dimensions else None,
                "mime_type": mime, "normalized": changed,
            })
        except (OSError, ValueError) as error:
            raise ClientError(f"Cannot prepare reference {index + 1}: {error}") from error
    # Enforce per-image limits first, then reduce the largest uploads only as
    # needed. Equal shares unnecessarily recompress small images in mixed sets.
    total = sum(item["prepared_bytes"] for item in items)
    fair_share = total_budget // max(1, len(paths))
    for index in sorted(range(len(items)), key=lambda i: items[i]["prepared_bytes"], reverse=True):
        if total <= total_budget:
            break
        previous_size = items[index]["prepared_bytes"]
        remaining = total_budget - (total - previous_size)
        targets = [remaining] if remaining > 0 else []
        if remaining < fair_share:
            targets.append(fair_share)
        for target in targets:
            if target >= previous_size:
                continue
            try:
                # Always encode from the original, never recompress a JPEG copy.
                reduced = prepare_reference_images(
                    [paths[index]], destination / str(index + 1) / "total-budget",
                    {**limits, "max_total_bytes": target}, snapshot=snapshot,
                )
            except ReferenceBudgetError:
                continue
            prepared[index], items[index] = reduced.paths[0], reduced.items[0]
            total += items[index]["prepared_bytes"] - previous_size
            break
    if total > total_budget:
        raise ReferenceBudgetError("References cannot fit the total upload budget without excessive detail loss. Use fewer/smaller references; no request was sent.")
    for index, item in enumerate(items):
        if item["normalized"]:
            warnings.append(
                f"Reference {index + 1} normalized for upload: {tuple(item['original_dimensions'])} / "
                f"{item['original_bytes']} bytes → {tuple(item['prepared_dimensions'])} / "
                f"{item['prepared_bytes']} bytes. Original preserved."
            )
    return ReferencePreparation(prepared, items, warnings,
                                sum(item["original_bytes"] for item in items),
                                sum(item["prepared_bytes"] for item in items))


def prepare_request_inputs(
    args: argparse.Namespace, state_dir: Path, selection: dict[str, Any],
    destination: Path, *, snapshot: bool = False,
) -> dict[str, Any]:
    references = validate_reference_paths(args.image)
    source = "uploaded" if references else "none"
    current_prompt = extract_current_prompt(args.prompt)
    use_last = args.use_last or args.mode == "edit" or (
        args.mode == "auto" and has_correction_intent(current_prompt)
    )
    if not references and use_last:
        references = [load_last_image(state_dir)]
        source = "last-successful-image" if args.use_last or args.mode == "edit" else "auto-correction-last-image"
    # Explicit references (including --use-last) determine the image operation.
    # generate only disables implicit reuse based on correction wording.
    prompt, info = sanitize_edit_prompt(
        args.prompt, preserve_text=getattr(args, "prompt_mode", None) is not None,
    ) if references else (args.prompt, {
        "compacted": False, "original_chars": len(args.prompt),
        "sent_chars": len(args.prompt), "sent_bytes": len(args.prompt.encode("utf-8")),
    })
    args.prompt = prompt
    entry = next((entry for entry in selection.get("models", [])
                  if isinstance(entry, dict) and entry.get("id") == args.model), {})
    preparation = prepare_reference_images(references, destination,
                                          entry.get("reference_image_limits"), snapshot=snapshot)
    size = validate_size_request(args, preparation.items)
    return {
        "prompt": prompt, "prompt_info": info, "reference_source": source,
        "prompt_mode": getattr(args, "prompt_mode", None),
        "requested_size": size,
        "size_source": ("explicit-size" if args.size else "explicit-aspect" if args.aspect
                        else "original-reference" if references else "default-square"),
        "references": [str(path) for path in references],
        "request_references": [str(path) for path in preparation.paths],
        "reference_images": preparation.items, "reference_warnings": preparation.warnings,
        "original_reference_bytes": preparation.original_bytes,
        "prepared_reference_bytes": preparation.prepared_bytes,
        "reference_limit_source": "published-and-client-budget" if entry.get("reference_image_limits") else "client-budget",
    }


def save_last_state(
    state_dir: Path,
    image_path: Path,
    model: str,
    endpoint: str,
    requested_size: str,
    result: dict[str, Any] | None = None,
) -> bool:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "path": str(image_path.resolve()),
        "model": model,
        "endpoint": endpoint,
        "requested_size": requested_size,
        "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if result is not None:
        payload["result"] = result
    with image_state_lock(state_dir):
        latest = latest_image_request(state_dir)
        if result and result.get("request_id") and not matches_latest_request(
            latest, request_id=result["request_id"], timestamp=payload["saved_at"],
        ):
            # The older job keeps its own saved files/result, but cannot replace
            # a later request's last image or reference for the next edit.
            return False
        write_json_file(state_dir / "last.json", payload)
    return True


def build_chat_payload(
    prompt: str, model: str, size: str, references: list[Path], *, image_url_object: bool = False,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    compatible_images: list[dict[str, Any]] = []
    for path in references:
        data_url = image_to_data_url(path)
        content.append(
            {"type": "image_url", "image_url": {"url": data_url}}
        )
        compatible_images.append({"image_url": {"url": data_url} if image_url_object else data_url})
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "size": size,
    }
    if compatible_images:
        payload["images"] = compatible_images
    return payload


def attempt_chat(
    client: HttpClient,
    prompt: str,
    model: str,
    size: str,
    references: list[Path],
    custom_url: str | None = None,
) -> tuple[Any, str]:
    url = custom_url or endpoint_url(client.base_url, "/v1/chat/completions")
    payload = build_chat_payload(prompt, model, size, references)
    try:
        return client.request_json("POST", url, payload), "chat"
    except APIError as error:
        # Only a definite field-validation rejection permits one format retry.
        # A disconnect, 413, no-image result, or download failure never does.
        if not (references and error.status_code in {400, 422}
                and re.search(r"images\s*\[.*?\]\s*\.\s*image_url", str(error), re.I)
                and re.search(r"required|invalid|must|expected|type|unmarshal", str(error), re.I)):
            raise
        payload = build_chat_payload(prompt, model, size, references, image_url_object=True)
        return client.request_json("POST", url, payload), "chat"


def attempt_images(
    client: HttpClient,
    prompt: str,
    model: str,
    size: str,
    references: list[Path],
    custom_url: str | None = None,
    *, count: int = 1,
) -> tuple[Any, str]:
    if references:
        url = custom_url or endpoint_url(client.base_url, "/v1/images/edits")
        fields = {"model": model, "prompt": prompt, "size": size, "n": str(count)}
        return client.request_multipart(url, fields, references), "images-edits"

    url = custom_url or endpoint_url(client.base_url, "/v1/images/generations")
    payload = {"model": model, "prompt": prompt, "size": size, "n": count}
    return client.request_json("POST", url, payload), "images-generations"


def add_candidate(
    candidates: list[ImageCandidate], seen: set[tuple[str, str]], candidate: ImageCandidate
) -> None:
    key = (candidate.kind, candidate.value)
    if candidate.value and key not in seen:
        seen.add(key)
        candidates.append(candidate)


def extract_image_candidates(payload: Any) -> list[ImageCandidate]:
    candidates: list[ImageCandidate] = []
    seen: set[tuple[str, str]] = set()

    def add_url(value: str) -> None:
        url = html.unescape(value).strip().strip('<>"\'`')
        url = url.rstrip(".,;，。；！!）】")
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        if url.lower().startswith(("http://", "https://")):
            add_candidate(candidates, seen, ImageCandidate("url", url))

    def from_string(value: str, hint: str | None, depth: int) -> None:
        stripped = value.strip()
        if not stripped:
            return
        if stripped.startswith(("{", "[", "```json")):
            serialized = re.sub(r"^```json\s*|\s*```$", "", stripped, flags=re.I)
            try:
                decoded = json.loads(serialized)
            except ValueError:
                pass
            else:
                validate_api_payload(decoded)
                walk(decoded, hint, depth + 1)
                return
        data_match = DATA_URL_RE.fullmatch(stripped)
        if data_match:
            add_candidate(candidates, seen, ImageCandidate("data_url", stripped, data_match.group(1)))
            return
        if hint in BASE64_KEYS:
            add_candidate(candidates, seen, ImageCandidate("base64", stripped))
            return
        if hint in IMAGE_URL_KEYS and stripped.lower().startswith(("http://", "https://")) and not re.search(r"\s", stripped):
            add_url(stripped)
            return
        if hint in IMAGE_URL_KEYS | {"data", "result"} and re.match(r"^(?:iVBORw0KGgo|/9j/|R0lGOD|UklGR)", stripped):
            add_candidate(candidates, seen, ImageCandidate("base64", stripped))
            return
        # Embedded URIs end at a line break; following prose can also consist
        # entirely of Base64 letters. Standalone wrapped values are handled above.
        for match in INLINE_DATA_URL_RE.finditer(stripped):
            add_candidate(
                candidates,
                seen,
                ImageCandidate("data_url", match.group(0), match.group(1)),
            )
        for match in re.finditer(r"!\[[^\]]*\]\(<?(https?://[^\s>]+?)>?(?:\s+[\"'][^\"']*[\"'])?\)", stripped, re.I):
            add_url(match.group(1))
        for match in re.finditer(r"<img\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", stripped, re.I):
            from_string(html.unescape(match.group(1)), "image_url", depth + 1)
        if hint in {None, "content", "text", "output_text", "result", "output"}:
            for match in HTTP_URL_RE.finditer(stripped):
                url = match.group(0).rstrip("`.,;:，。；！!）】")
                # Text may contain documentation/help links. Only accept naked
                # URLs, image extensions, or links introduced as image results.
                if (stripped == match.group(0) or re.search(r"\.(png|jpe?g|webp|gif|avif)(?:[?#)]|$)", url, re.I)
                        or re.search(r"图片|图像|下载|image|download", stripped[:match.start()], re.I)):
                    add_url(url)

    def walk(value: Any, hint: str | None = None, depth: int = 0) -> None:
        if depth > 16:
            return
        if isinstance(value, str):
            from_string(value, hint, depth)
            return
        if isinstance(value, list):
            for item in value:
                walk(item, hint, depth + 1)
            return
        if not isinstance(value, dict):
            return

        normalized = {key.lower(): nested for key, nested in value.items()}
        if str(normalized.get("role", "")).lower() in {"user", "system", "developer", "tool"}:
            return
        validate_api_payload(normalized)
        item_type = str(normalized.get("type", "")).lower()
        mime = str(normalized.get("mime_type", normalized.get("mimetype", normalized.get("media_type", ""))))
        image_context = "image" in item_type or mime.startswith("image/") or hint in {"image", "output_image", "source", "inline_data", "inlinedata"}
        for key, nested in normalized.items():
            if key in {"prompt", "revised_prompt", "input", "metadata", "usage", "error"}:
                continue
            if image_context and key in {"data", "result"} and isinstance(nested, str):
                walk(nested, "base64" if not nested.startswith(("data:", "http://", "https://")) else "image_url", depth + 1)
            elif key in IMAGE_URL_KEYS | BASE64_KEYS | IMAGE_CONTAINER_KEYS:
                walk(nested, key, depth + 1)

    walk(payload)
    return candidates


def decode_base64(value: str) -> bytes:
    compact = re.sub(r"\s+", "", value)
    compact = compact.replace("-", "+").replace("_", "/")
    compact += "=" * (-len(compact) % 4)
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ClientError("The response contained invalid Base64 image data.") from error


def decode_candidate(
    candidate: ImageCandidate, client: HttpClient
) -> tuple[bytes, str | None]:
    if candidate.kind == "url":
        return client.download(candidate.value)
    if candidate.kind == "data_url":
        match = DATA_URL_RE.fullmatch(candidate.value.strip())
        if not match:
            raise ClientError("The response contained an invalid image data URL.")
        return decode_base64(match.group(2)), match.group(1).lower()
    if candidate.kind == "base64":
        return decode_base64(candidate.value), candidate.mime_type
    raise ClientError(f"Unknown image candidate kind: {candidate.kind}")


def detect_mime_type(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    if len(raw) >= 12 and raw[4:12] in {b"ftypavif", b"ftypavis"}:
        return "image/avif"
    return None


def extension_for_mime(mime_type: str | None) -> str:
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/avif": ".avif",
    }
    if mime_type in mapping:
        return mapping[mime_type]
    guessed = mimetypes.guess_extension(mime_type or "")
    return guessed if guessed and guessed.startswith(".") else ".img"


def detect_dimensions(raw: bytes, mime_type: str | None) -> tuple[int, int] | None:
    detected = detect_mime_type(raw) or mime_type
    try:
        if detected == "image/png" and len(raw) >= 24:
            return struct.unpack(">II", raw[16:24])
        if detected == "image/gif" and len(raw) >= 10:
            return struct.unpack("<HH", raw[6:10])
        if detected == "image/jpeg":
            offset = 2
            while offset + 9 < len(raw):
                if raw[offset] != 0xFF:
                    offset += 1
                    continue
                marker = raw[offset + 1]
                offset += 2
                if marker in {0xD8, 0xD9}:
                    continue
                length = int.from_bytes(raw[offset : offset + 2], "big")
                if length < 2 or offset + length > len(raw):
                    break
                if marker in {
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                }:
                    height = int.from_bytes(raw[offset + 3 : offset + 5], "big")
                    width = int.from_bytes(raw[offset + 5 : offset + 7], "big")
                    return width, height
                offset += length
    except (IndexError, struct.error, ValueError):
        return None
    return None


def safe_slug(value: str, limit: int = 48) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", value).strip("-.")
    return (slug or "image")[:limit]


def save_candidates(
    candidates: list[ImageCandidate],
    client: HttpClient,
    output_dir: Path,
    model: str,
    endpoint_label: str,
    *, limit: int | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not candidates:
        raise ClientError("The endpoint returned HTTP 200 but no image was found.")
    output_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, Any]] = []
    failures: list[str] = []
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:12]

    for index, candidate in enumerate(candidates, start=1):
        if limit is not None and len(saved) >= limit:
            break
        try:
            raw, declared_mime = decode_candidate(candidate, client)
            detected_mime = detect_mime_type(raw)
            mime_type = detected_mime
            if not raw or not mime_type:
                raise ClientError("Downloaded or decoded content is not a recognized image.")
            dimensions = detect_dimensions(raw, mime_type)
            try:
                from PIL import Image
            except ImportError:
                if mime_type in {"image/png", "image/jpeg", "image/gif"} and not dimensions:
                    raise ClientError("Image has no readable dimensions; its data may be truncated.")
                if (mime_type == "image/png" and b"\0\0\0\0IEND\xaeB`\x82" not in raw
                        or mime_type == "image/jpeg" and b"\xff\xd9" not in raw[2:]
                        or mime_type == "image/gif" and b";" not in raw[10:]
                        or mime_type == "image/webp" and len(raw) < int.from_bytes(raw[4:8], "little") + 8):
                    raise ClientError("Image data is truncated. Install Pillow for full decoding validation.")
            else:
                try:
                    with Image.open(io.BytesIO(raw)) as picture:
                        picture.verify()
                    with Image.open(io.BytesIO(raw)) as picture:
                        picture.load()
                        dimensions = picture.size
                except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
                    raise ClientError(f"Image data could not be decoded or validated: {error}") from error
            if dimensions and min(dimensions) <= 0:
                raise ClientError("Image has invalid pixel dimensions.")
            extension = extension_for_mime(mime_type)
            filename = (
                f"{timestamp}-{safe_slug(model)}-{safe_slug(endpoint_label)}-{index}{extension}"
            )
            path = (output_dir / filename).resolve()
            path.write_bytes(raw)
            saved.append(
                {
                    "path": str(path),
                    "mime_type": mime_type,
                    "bytes": len(raw),
                    "width": dimensions[0] if dimensions else None,
                    "height": dimensions[1] if dimensions else None,
                }
            )
        except (ClientError, OSError) as error:
            failures.append(f"candidate {index}: {error}")

    if not saved:
        raise ClientError("Image candidates were returned, but download/decoding/validation failed: " + "; ".join(failures))
    return saved, failures


def response_shape(payload: Any) -> Any:
    """Diagnostic structure only: no prompt, Base64, signed URLs or credentials."""
    def shape(value: Any, depth: int = 0) -> Any:
        if depth > 5:
            return type(value).__name__
        if isinstance(value, dict):
            return {safe_slug(str(key), 48): shape(nested, depth + 1)
                    for key, nested in list(value.items())[:24]}
        if isinstance(value, list):
            return {"count": len(value), "sample": [shape(item, depth + 1) for item in value[:2]]}
        if isinstance(value, str):
            return {"type": "string", "chars": len(value)}
        return type(value).__name__
    return shape(payload)


def model_catalog(client: HttpClient) -> list[dict[str, Any]]:
    payload = client.request_json("GET", endpoint_url(client.base_url, "/v1/models"))
    return format_model_catalog(payload)


def prepare_display(
    saved: list[dict[str, Any]], preview_dir: Path | None = None
) -> list[str]:
    warnings: list[str] = []
    for item in saved:
        path = Path(item["path"]).resolve()
        display_path = path
        item.pop("preview_path", None)
        item.pop("preview_width", None)
        item.pop("preview_height", None)
        if item["bytes"] > 4 * 1024 * 1024 or max(
            item.get("width") or 0, item.get("height") or 0
        ) > 2048:
            try:
                from PIL import Image, ImageOps

                destination = preview_dir or path.parent / ".previews"
                destination.mkdir(parents=True, exist_ok=True)
                preview = destination / f"{path.stem}-{hashlib.sha256(str(path).encode()).hexdigest()[:8]}.preview.jpg"
                with Image.open(path) as original:
                    thumbnail = ImageOps.exif_transpose(original)
                    thumbnail.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                    rgba = thumbnail.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    background.save(preview, "JPEG", quality=85, optimize=True)
                display_path = preview.resolve()
                item.update(
                    preview_path=str(display_path),
                    preview_width=background.width,
                    preview_height=background.height,
                )
            except (ImportError, OSError, ValueError) as error:
                warnings.append(
                    f"Image saved, but a display preview could not be created: {error}. "
                    "Use the original file link; do not regenerate the image."
                )
        item["display_path"] = str(display_path)
        item["display_markdown"] = f"![Image preview](<{display_path.as_posix()}>)"
        item["original_markdown"] = f"[Original image](<{path.as_posix()}>)"
    return warnings


def check_output_dimensions(images: list[dict[str, Any]], size: Any) -> list[str]:
    requested = None
    if isinstance(size, str):
        with contextlib.suppress(ClientError):
            requested = parse_size(size)
    warnings = []
    for image in images:
        actual = (image.get("width"), image.get("height"))
        image["matches_requested_size"] = actual == requested if requested and all(actual) else None
        if image["matches_requested_size"] is False:
            warnings.append(
                f"Provider returned {actual[0]}x{actual[1]}, but {size} was requested. "
                "The returned image was saved unchanged; report its actual dimensions, "
                "do not claim the requested resolution or automatically resubmit."
            )
    return warnings


def validate_size_request(args: argparse.Namespace, reference_images: list[dict[str, Any]] | None = None) -> str:
    inherited = None
    if not args.size and not args.aspect and reference_images:
        original_sizes: set[tuple[int, int]] = set()
        for image in reference_images:
            dimensions = image.get("original_oriented_dimensions") or image.get("original_dimensions")
            if (not isinstance(dimensions, (list, tuple)) or len(dimensions) != 2
                    or any(type(value) is not int or value <= 0 for value in dimensions)):
                raise ClientError("Cannot determine the original reference dimensions. Ask the user for --size or --aspect; no request was sent.")
            original_sizes.add(tuple(dimensions))
        if len(original_sizes) != 1:
            sizes = ", ".join(f"{w}x{h}" for w, h in sorted(original_sizes))
            raise ClientError(f"References have different original dimensions ({sizes}). Ask the user to choose --size or --aspect; no request was sent.")
        width, height = next(iter(original_sizes))
        inherited = f"{width}x{height}"
    size = choose_size(args.size or inherited, args.aspect)
    high_resolution_intent = has_high_resolution_intent(args.prompt)
    if high_resolution_intent and not args.confirm_high_res:
        raise ClientError(
            "The prompt expresses high-resolution intent. Run models, present the published "
            "and experimental sizes, obtain user confirmation, and retry with an explicit "
            "--size plus --confirm-high-res."
        )
    if high_resolution_intent and not args.size:
        raise ClientError(
            "Confirmed high-resolution intent requires an explicit --size; refusing to send "
            "a default 1K request."
        )
    if size not in CONSERVATIVE_SIZES and not args.confirm_high_res:
        if inherited:
            raise ClientError(
                f"Preserving original reference size {size} requires user confirmation. "
                "Present published and experimental sizes, confirm this exact size, then "
                f"retry with --size {size} --confirm-high-res. No lower-resolution request was sent."
            )
        raise ClientError(
            f"Size {size} requires user confirmation. Run models, present published and "
            "experimental sizes, then retry with --confirm-high-res."
        )
    return size


def run_argv(args: argparse.Namespace, thread_id: str) -> list[str]:
    result = [
        "run", "--prompt=" + args.prompt, "--model", args.model,
        "--selection-token", args.selection_token, "--mode", args.mode,
        "--endpoint", args.endpoint, "--count", str(args.count),
        "--thread-id", thread_id, "--timeout", str(args.timeout),
    ]
    if getattr(args, "prompt_mode", None):
        result.extend(("--prompt-mode", args.prompt_mode))
    for path in args.image:
        result.extend(("--image", path))
    if args.use_last:
        result.append("--use-last")
    if args.confirm_high_res:
        result.append("--confirm-high-res")
    if args.confirm_codex_auth:
        result.append("--confirm-codex-auth")
    for flag, value in (
        ("--custom-url", args.custom_url), ("--custom-kind", args.custom_kind),
        ("--aspect", args.aspect),
        ("--size", args.size), ("--base-url", args.base_url),
        ("--output-dir", args.output_dir),
    ):
        if value:
            result.extend((flag, str(value)))
    return result


def latest_job_id(state_dir: Path) -> str | None:
    latest = read_json_file(state_dir / "latest-job.json")
    value = latest.get("job_id") if isinstance(latest, dict) else None
    return value if isinstance(value, str) and value else None


def job_directory(state_dir: Path, job_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", job_id):
        raise ClientError("Invalid background job ID.")
    return state_dir / "jobs" / job_id


def process_is_running(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) terminates processes on Windows instead of probing them.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such PID
        try:
            return kernel32.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        return error.errno != errno.ESRCH
    return True


def create_windows_worker_task(command: list[str], cwd: str, job_id: str) -> str:
    if os.name != "nt":
        raise ClientError("Windows task scheduling is only available on Windows.")
    schtasks = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "schtasks.exe"
    if not schtasks.is_file():
        raise ClientError("Windows task scheduler is unavailable; the high-resolution job was not started.")
    task_name = r"\KeylinkImage-" + job_id
    identity = subprocess.run(
        [str(schtasks.with_name("whoami.exe")), "/USER", "/FO", "CSV", "/NH"],
        cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="mbcs", errors="replace",
        check=False,
    )
    rows = list(csv.reader(identity.stdout.splitlines()))
    if identity.returncode != 0 or not rows or len(rows[0]) != 2:
        raise ClientError("Could not identify the current Windows user for the image job.")
    user_sid = rows[0][1]
    if not re.fullmatch(r"S-\d+(?:-\d+)+", user_sid):
        raise ClientError("Windows returned an invalid user SID for the image job.")

    # XML separates executable/arguments without /TR's 261-character limit.
    # No triggers: only /Run starts the job, so it cannot repeat a paid request.
    task = ET.Element("Task", version="1.2", xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task")
    principal = ET.SubElement(ET.SubElement(task, "Principals"), "Principal", id="Author")
    ET.SubElement(principal, "UserId").text = user_sid
    ET.SubElement(principal, "LogonType").text = "InteractiveToken"
    ET.SubElement(principal, "RunLevel").text = "LeastPrivilege"
    settings = ET.SubElement(task, "Settings")
    for name, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("AllowStartOnDemand", "true"),
        ("Enabled", "true"),
        ("ExecutionTimeLimit", "PT0S"),
    ):
        ET.SubElement(settings, name).text = value
    action = ET.SubElement(ET.SubElement(task, "Actions", Context="Author"), "Exec")
    executable = Path(command[0])
    windowless = executable.with_name("pythonw.exe")
    ET.SubElement(action, "Command").text = str(windowless if windowless.is_file() else executable)
    ET.SubElement(action, "Arguments").text = subprocess.list2cmdline(command[1:])
    ET.SubElement(action, "WorkingDirectory").text = cwd
    with tempfile.TemporaryDirectory(prefix="keylink-task-") as temporary:
        definition = Path(temporary) / "worker.xml"
        ET.ElementTree(task).write(definition, encoding="utf-16", xml_declaration=True)
        create = subprocess.run(
            [str(schtasks), "/Create", "/TN", task_name, "/XML", str(definition), "/F"],
            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="mbcs", errors="replace",
            check=False,
        )
    if create.returncode != 0:
        detail = (create.stderr or create.stdout).strip()
        raise ClientError(
            "Windows task scheduler could not create the persistent image job"
            + (f": {detail}" if detail else ".")
        )
    return task_name


def run_windows_worker_task(task_name: str, cwd: str) -> None:
    schtasks = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "schtasks.exe"
    start = subprocess.run(
        [str(schtasks), "/Run", "/TN", task_name],
        cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="mbcs", errors="replace",
        check=False,
    )
    if start.returncode != 0:
        subprocess.run(
            [str(schtasks), "/Delete", "/TN", task_name, "/F"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )
        detail = (start.stderr or start.stdout).strip()
        raise ClientError(
            "Windows task scheduler could not start the persistent image job"
            + (f": {detail}" if detail else ".")
        )


def delete_windows_task(task_name: Any) -> None:
    if os.name != "nt" or not isinstance(task_name, str) or not task_name:
        return
    schtasks = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "schtasks.exe"
    subprocess.run(
        [str(schtasks), "/Delete", "/TN", task_name, "/F"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False,
    )


def command_start(args: argparse.Namespace) -> int:
    base_url, _, _ = discover_base_url(args.base_url)
    thread_id = resolve_thread_id(args.thread_id)
    state_dir = thread_state_dir(thread_id)
    selection = validate_model_selection(
        state_dir, args.selection_token, args.model, base_url
    )

    job_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    claim = state_dir / "jobs" / ("selection-" + hashlib.sha256(
        args.selection_token.encode("utf-8")
    ).hexdigest()[:24] + ".claim")
    claim.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = claim.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        raise ClientError(
            "This model selection already started background job "
            f"{existing}. Check its status instead of submitting again."
        )

    directory = job_directory(state_dir, job_id)
    directory.mkdir(parents=True, exist_ok=False)
    prepared_input = prepare_request_inputs(args, state_dir, selection, directory / "references", snapshot=True)
    size = validate_size_request(args, prepared_input["reference_images"])
    try:
        claim_fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(claim_fd, (job_id + "\n").encode("utf-8"))
        finally:
            os.close(claim_fd)
    except FileExistsError:
        existing = claim.read_text(encoding="utf-8", errors="replace").strip()
        try:
            directory.rmdir()
        except OSError:
            pass
        raise ClientError(
            "This model selection already started background job "
            f"{existing or 'unknown'}. Check its status instead of submitting again."
        )
    except Exception:
        if directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass
        raise

    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    request = {
        "argv": run_argv(args, thread_id),
        "cwd": str(Path.cwd().resolve()),
        "prepared_input": prepared_input,
    }
    job = {
        "job_id": job_id, "status": "starting", "thread_id": thread_id,
        "request_id": image_request_id(args.selection_token),
        "model": args.model, "requested_size": size, "requested_count": args.count,
        "prompt_mode": prepared_input.get("prompt_mode"),
        "started_at": started_at,
        "cwd": request["cwd"],
    }
    command = [
        sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
        "_worker", "--job-dir", str(directory.resolve()),
    ]
    popen_kwargs: dict[str, Any] = {
        "cwd": request["cwd"], "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        "close_fds": True, "env": os.environ.copy(),
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
            | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
        )
    else:
        popen_kwargs["start_new_session"] = True
    process = None
    launch_mode = "detached-process"
    task_name = None
    try:
        write_json_file(directory / "request.json", request)
        write_json_file(directory / "job.json", job)
        write_json_file(state_dir / "latest-job.json", {"job_id": job_id})
        record_image_request(state_dir, args.selection_token, args.model, job_id=job_id)
        try:
            process = subprocess.Popen(command, **popen_kwargs)
        except PermissionError:
            if os.name != "nt":
                raise
            task_name = create_windows_worker_task(command, request["cwd"], job_id)
            launch_mode = "scheduled-task"
            job.update(task_name=task_name, launch_mode=launch_mode)
            write_json_file(directory / "job.json", job)
            run_windows_worker_task(task_name, request["cwd"])
    except (OSError, ClientError) as error:
        try:
            delete_windows_task(task_name)
        except OSError:
            pass
        job.update(status="failed", finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                   error=f"Could not start background worker: {error}")
        # Storage itself may have caused the failure. Release the unused claim
        # even when the failure record cannot be written.
        with contextlib.suppress(OSError, ClientError):
            write_json_file(directory / "job.json", job)
            finish_image_request(state_dir, args.selection_token, "failed")
        try:
            claim.unlink()
        except OSError:
            pass
        raise ClientError(job["error"]) from error

    emit({
        "status": "started", "background": True, "job_id": job_id,
        "pid": process.pid if process is not None else None,
        "launch_mode": launch_mode,
        "task_name": task_name,
        "thread_id": thread_id, "model": args.model,
        "requested_size": size, "requested_count": args.count, "started_at": started_at,
        "size_source": prepared_input.get("size_source"),
        "prompt_info": prepared_input["prompt_info"],
        "prompt_mode": prepared_input.get("prompt_mode"),
        "prepared_prompt": prepared_input["prompt"],
        "reference_images": prepared_input["reference_images"],
        "warnings": prepared_input["reference_warnings"],
        "status_args": ["status", "--job-id", job_id, "--thread-id", thread_id],
        "message": "Background image job started. Poll status; do not submit the request again.",
    })
    return 0


def command_worker(args: argparse.Namespace) -> int:
    directory = Path(args.job_dir).resolve()
    request = read_json_file(directory / "request.json")
    job = read_json_file(directory / "job.json")
    if not isinstance(request, dict) or not isinstance(job, dict):
        return 1
    job.update(status="running", pid=os.getpid(), worker_started_at=dt.datetime.now(dt.timezone.utc).isoformat())
    write_json_file(directory / "job.json", job)
    temporary = directory / f".result-{uuid.uuid4().hex}.tmp"
    exit_code = 1
    try:
        argv = request.get("argv")
        cwd = request.get("cwd")
        if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
            raise ClientError("Background job request arguments are invalid.")
        if not isinstance(cwd, str) or not Path(cwd).is_dir():
            raise ClientError("Background job workspace no longer exists.")
        os.chdir(cwd)
        with temporary.open("w", encoding="utf-8") as output, (directory / "worker.log").open(
            "a", encoding="utf-8"
        ) as log, contextlib.redirect_stdout(output), contextlib.redirect_stderr(log):
            exit_code = main(argv, prepared_input=request.get("prepared_input"))
    except BaseException as error:
        with temporary.open("w", encoding="utf-8") as output:
            emit({"status": "error", "error": f"Background worker failed: {error}"}, stream=output)
        exit_code = 1
    payload = read_json_file(temporary)
    job.update(
        status="succeeded" if exit_code == 0 and isinstance(payload, dict)
        and payload.get("status") == "ok" else "failed",
        exit_code=exit_code,
        finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    write_json_file(directory / "job.json", job)
    # Publishing the result is the final step; readers can then use it immediately.
    temporary.replace(directory / "result.json")
    try:
        delete_windows_task(job.get("task_name"))
    except OSError as error:
        # Scheduler cleanup must not hide an already completed image request.
        with contextlib.suppress(OSError):
            with (directory / "worker.log").open("a", encoding="utf-8") as log:
                log.write(f"Result saved; scheduled-task cleanup failed: {error}\n")
    return 0


def command_status(args: argparse.Namespace) -> int:
    thread_id = resolve_thread_id(args.thread_id)
    state_dir = thread_state_dir(thread_id)
    job_id = args.job_id or latest_job_id(state_dir)
    if not job_id:
        raise ClientError("No background image job is recorded for this Codex task.")
    directory = job_directory(state_dir, job_id)
    job = read_json_file(directory / "job.json")
    if not isinstance(job, dict):
        raise ClientError(f"Background job {job_id} has no readable status record.")
    payload = read_json_file(directory / "result.json")
    current = matches_latest_request(
        latest_image_request(state_dir), job_id=job_id, timestamp=job.get("started_at"),
        request_id=job.get("request_id") or (payload.get("request_id") if isinstance(payload, dict) else None),
    )
    if not current and not args.job_id:
        emit({
            "status": "error", "thread_id": thread_id, "images": [],
            "current_request_succeeded": False, "previous_job_id": job_id,
            "error": "The saved background job belongs to an earlier request. Check the latest command/session result; this job is not evidence that the latest request succeeded.",
        })
        return 1
    if isinstance(payload, dict):
        result = dict(payload)
        result.update(background=True, job_id=job_id,
                      job_status="succeeded" if result.get("status") == "ok" else "failed",
                      historical=not current,
                      current_request_succeeded=current and result.get("status") == "ok")
        if not current:
            result["warnings"] = list(result.get("warnings", [])) + ["Historical job result only; this is not the latest request's result."]
        emit(result)
        return 0 if result.get("status") == "ok" else 1
    status = str(job.get("status", "unknown"))
    if status in {"succeeded", "failed"} and "exit_code" in job and process_is_running(job.get("pid")):
        status = "running"  # The worker is publishing its final result.
    if status in {"starting", "running"} and job.get("pid") and not process_is_running(job.get("pid")):
        status = "failed"
        job.update(
            status=status, finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            error="The background worker stopped before writing a result.",
        )
        write_json_file(directory / "job.json", job)
    emit({
        "status": status, "background": True, "job_id": job_id,
        "historical": not current, "current_request_succeeded": False,
        "thread_id": thread_id, "model": job.get("model"),
        "prompt_mode": job.get("prompt_mode"),
        "requested_size": job.get("requested_size"),
        "requested_count": job.get("requested_count", 1), "started_at": job.get("started_at"),
        "pid": job.get("pid"), "error": job.get("error"),
        "message": (
            "Background image job is still running; poll this job again and do not resubmit."
            if status in {"starting", "running"}
            else "Background image job ended without a recoverable image result."
        ),
    })
    return 0 if status in {"starting", "running"} else 1


def command_last(args: argparse.Namespace) -> int:
    thread_id = resolve_thread_id(args.thread_id)
    state_dir = thread_state_dir(thread_id)
    state = read_json_file(state_dir / "last.json")
    latest = latest_image_request(state_dir)
    result = state.get("result") if isinstance(state, dict) else None
    current = matches_latest_request(
        latest, timestamp=state.get("saved_at") if isinstance(state, dict) else None,
        request_id=result.get("request_id") if isinstance(result, dict) else None,
    )
    if not current and not args.allow_previous:
        emit({
            "status": "error", "thread_id": thread_id,
            "error": "The latest request has no saved successful image. The previous image is not its result. Check the original command or background job status.",
            "latest_request": latest, "current_request_succeeded": False,
            "previous_result_available": isinstance(state, dict), "images": [],
        })
        return 1
    first_path = load_last_image(state_dir)
    result = dict(result) if isinstance(result, dict) else None
    if not isinstance(result, dict):
        result = {
            "model": state.get("model"),
            "endpoint": state.get("endpoint"),
            "requested_size": state.get("requested_size"),
            "images": [{"path": str(first_path)}],
        }
    saved = []
    for item in result.get("images", []):
        path = Path(item["path"])
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise ClientError(f"Cannot read saved image {path}: {error}") from error
        mime = detect_mime_type(raw)
        if not mime:
            raise ClientError(f"Saved file is not a recognized image: {path}")
        dimensions = detect_dimensions(raw, mime)
        if dimensions is None:
            try:
                from PIL import Image
            except ImportError:
                pass
            else:
                try:
                    with Image.open(io.BytesIO(raw)) as picture:
                        picture.load()
                        dimensions = picture.size
                except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
                    raise ClientError(f"Cannot decode saved image {path}: {error}") from error
        saved.append({
            "path": str(path.resolve()), "mime_type": mime, "bytes": len(raw),
            "width": dimensions[0] if dimensions else None,
            "height": dimensions[1] if dimensions else None,
        })
    if not saved:
        raise ClientError("No image files are recorded for the latest result.")
    preview_dir = Path(args.preview_dir).expanduser().resolve() if args.preview_dir else None
    warnings = list(result.get("warnings", []))
    for warning in check_output_dimensions(saved, result.get("requested_size")):
        if warning not in warnings:
            warnings.append(warning)
    if not current:
        warnings.append("Historical image only: the latest request has no saved successful image. This is not a newly generated result.")
    warnings.extend(prepare_display(saved, preview_dir))
    result.update(
        status="ok", recovered=True, thread_id=thread_id,
        saved_at=state.get("saved_at"), images=saved, warnings=warnings,
        historical=not current, current_request_succeeded=current,
    )
    emit(result)
    return 0


def command_models(args: argparse.Namespace) -> int:
    base_url, base_source, ccswitch = discover_base_url(args.base_url)
    api_key, credential_source = discover_api_key(
        base_url, base_source, ccswitch,
        confirm_codex_auth=args.confirm_codex_auth,
    )
    if not api_key and not is_loopback(base_url):
        if ccswitch.running:
            raise ClientError(
                "CCSwitch is running, but no usable Keylink provider key or API-key-only "
                "Codex/Codex++ credential was found. Configure a Keylink provider "
                "(add it to aggregation if enabled) or set KEYLINK_API_KEY. "
                "PROXY_MANAGED and account tokens are not API keys."
            )
        raise ClientError(
            "No API credential was found. Start CCSwitch with the Keylink Provider, "
            "set KEYLINK_API_KEY, or confirm that the Codex/Codex++ credential is a Keylink key."
        )
    client = HttpClient(base_url, api_key, args.timeout)
    catalog = model_catalog(client)
    thread_id = resolve_thread_id(args.thread_id)
    selection_token = save_model_selection(
        thread_state_dir(thread_id), catalog, base_url
    )
    emit(
        {
            "status": "ok",
            "base_url": base_url,
            "base_url_source": base_source,
            "credential_source": credential_source or "none",
            "catalog_base_url": base_url,
            "thread_id": thread_id,
            "models": catalog,
            "image_models": [
                entry for entry in catalog if entry.get("image_candidate")
            ],
            "selection_token": selection_token,
            "selection_required": True,
            "output_count_choices": list(range(1, OUTPUT_MAX_COUNT + 1)),
            "default_output_count": 1,
            "max_output_count": OUTPUT_MAX_COUNT,
            "prompt_mode_choices": list(PROMPT_MODES),
            "prompt_mode_required": True,
            "preferred_sizes": list(CONSERVATIVE_SIZES),
            "experimental_sizes": list(EXPERIMENTAL_SIZES),
            "four_k_notice": (
                "4K generation can take several minutes. Tell the user to wait patiently "
                "before starting a 3840x2160 request."
            ),
        }
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    base_url, base_source, ccswitch = discover_base_url(args.base_url)
    api_key, credential_source = discover_api_key(
        base_url, base_source, ccswitch,
        confirm_codex_auth=args.confirm_codex_auth,
    )
    if not api_key and not is_loopback(base_url):
        if ccswitch.running:
            raise ClientError(
                "CCSwitch is running, but no usable Keylink provider key or API-key-only "
                "Codex/Codex++ credential was found. Configure a Keylink provider "
                "(add it to aggregation if enabled) or set KEYLINK_API_KEY. "
                "PROXY_MANAGED and account tokens are not API keys."
            )
        raise ClientError(
            "No API credential was found. Start CCSwitch with the Keylink Provider, "
            "set KEYLINK_API_KEY, or confirm that the Codex/Codex++ credential is a Keylink key."
        )
    client = HttpClient(base_url, api_key, args.timeout)

    model = args.model

    thread_id = resolve_thread_id(args.thread_id)
    state_dir = thread_state_dir(thread_id)
    selection = validate_model_selection(state_dir, args.selection_token, model, base_url)
    prepared_input = getattr(args, "_prepared_input", None)
    if not prepared_input:
        prepared_input = prepare_request_inputs(
            args, state_dir, selection, state_dir / "request-inputs" / uuid.uuid4().hex,
        )
    args.prompt = prepared_input["prompt"]
    size = validate_size_request(args, prepared_input["reference_images"])
    references = validate_reference_paths(prepared_input["request_references"])
    reference_source = prepared_input["reference_source"]

    consume_model_selection(state_dir, args.selection_token, model, base_url)

    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else state_dir.resolve()
    )

    attempts: list[dict[str, Any]] = []
    order = endpoint_order(model, args.endpoint)
    for route in order:
        if route == "custom":
            route_kind = args.custom_kind
            custom_url = args.custom_url
        else:
            route_kind = route
            custom_url = None

        endpoint_label = "chat" if route_kind == "chat" else ("images-edits" if references else "images-generations")
        started = time.monotonic()
        response = None
        candidates = []
        phase = "request"
        try:
            saved: list[dict[str, Any]] = []
            partial_failures: list[str] = []
            batch_refused = False
            # Chat's n counts reply candidates, not output images. Request one
            # image at a time without feeding generated outputs into later edits.
            for _ in range(args.count if route_kind == "chat" else 1):
                response = None
                candidates = []
                phase = "request"
                try:
                    if route_kind == "chat":
                        response, endpoint_label = attempt_chat(
                            client, args.prompt, model, size, references, custom_url
                        )
                    elif route_kind == "images":
                        response, endpoint_label = attempt_images(
                            client, args.prompt, model, size, references, custom_url,
                            count=args.count,
                        )
                    else:
                        raise ClientError(f"Unsupported endpoint kind: {route_kind}")

                    candidates = extract_image_candidates(response)
                    phase = "no_image_in_response" if not candidates else "image_download_or_decode"
                    remaining = args.count - len(saved)
                    if len(candidates) > remaining:
                        partial_failures.append("The provider returned more image candidates than requested; only the requested number was retained.")
                    batch, failures = save_candidates(
                        candidates, client, output_dir, model, endpoint_label, limit=remaining
                    )
                    saved.extend(batch)
                    partial_failures.extend(failures)
                    if failures or len(saved) >= args.count:
                        break
                except ClientError as error:
                    if not saved:
                        raise
                    batch_refused = isinstance(error, RefusalError)
                    attempts.append({
                        "endpoint": endpoint_label, "error": str(error),
                        "phase": "provider_refusal" if batch_refused else phase,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "candidate_count": len(candidates),
                    })
                    partial_failures.append("The multi-image request stopped after an error; saved images were retained and no replacement request was submitted.")
                    break
            first_path = Path(saved[0]["path"])

            warnings: list[str] = list(prepared_input["reference_warnings"])
            if prepared_input["prompt_info"]["compacted"]:
                warnings.append("Conversation wrappers were removed from the edit prompt; only current edit instructions were sent.")
            if any(item["endpoint"] != endpoint_label for item in attempts):
                warnings.append(
                    f"Succeeded through {endpoint_label} after earlier endpoint failure."
                )
            warnings.extend(partial_failures)
            if len(saved) < args.count:
                warnings.append(f"Requested {args.count} images, but only {len(saved)} were saved. The requested count was not completed; no automatic top-up or endpoint fallback was performed after saving an image.")
            if batch_refused:
                warnings.append("The provider refused a later image in this batch; no further images were requested.")
            warnings.extend(check_output_dimensions(saved, size))
            if endpoint_label == "chat" and size not in CONSERVATIVE_SIZES:
                warnings.append(
                    "Chat accepted the request, but the requested pixel dimensions are not guaranteed."
                )

            warnings.extend(prepare_display(saved))
            result = {
                    "status": "ok",
                    "request_id": image_request_id(args.selection_token),
                    "current_request_succeeded": True,
                    "model": model,
                    "endpoint": endpoint_label,
                    "requested_size": size,
                    "requested_count": args.count,
                    "returned_count": len(saved),
                    "count_complete": len(saved) == args.count,
                    "provider_refused": batch_refused,
                    "size_source": prepared_input.get("size_source"),
                    "reference_source": reference_source,
                    "references": prepared_input["references"],
                    "reference_images": prepared_input["reference_images"],
                    "reference_limit_source": prepared_input["reference_limit_source"],
                    "prompt_info": prepared_input["prompt_info"],
                    "prompt_mode": prepared_input.get("prompt_mode"),
                    "sent_prompt": args.prompt,
                    "request_attempts": client.request_attempts,
                    "thread_id": thread_id,
                    "base_url": base_url,
                    "base_url_source": base_source,
                    "credential_source": credential_source or "none",
                    "images": saved,
                    "failed_attempts": attempts,
                    "warnings": warnings,
                }
            try:
                published = save_last_state(state_dir, first_path, model, endpoint_label, size, result)
                if not published:
                    result.update(historical=True, current_request_succeeded=False)
                    warnings.append("This earlier request finished after a newer request started; its image was saved without replacing the latest result.")
                finish_image_request(state_dir, args.selection_token, "succeeded")
            except (OSError, ClientError) as state_error:
                # The image is already saved. A local state failure must never
                # trigger another potentially billable generation endpoint.
                result["state_recorded"] = False
                warnings.append(f"Image saved, but request state could not be updated: {state_error}. Use the returned image paths; do not resubmit.")
            emit(result)
            return 0
        except ClientError as error:
            if isinstance(error, RefusalError):
                phase = "provider_refusal"
            attempt = {"endpoint": endpoint_label, "error": str(error), "phase": phase,
                       "elapsed_seconds": round(time.monotonic() - started, 3),
                       "candidate_count": len(candidates)}
            if response is not None:
                attempt["response_shape"] = response_shape(response)
            attempts.append(attempt)
            if isinstance(error, RefusalError):
                break

    automatic = args.endpoint == "auto"
    refused = any(item.get("phase") == "provider_refusal" for item in attempts)
    finish_image_request(state_dir, args.selection_token, "refused" if refused else "failed")
    emit(
        {
            "status": "error",
            "request_id": image_request_id(args.selection_token),
            "error": "The provider refused this request; no new image was saved." if refused else "Image request failed; no new image was saved. See attempts for endpoint errors.",
            "current_request_succeeded": False,
            "provider_refused": refused,
            "requested_count": args.count, "returned_count": 0, "count_complete": False,
            "model": model,
            "requested_size": size,
            "reference_source": reference_source,
            "reference_images": prepared_input["reference_images"],
            "reference_limit_source": prepared_input["reference_limit_source"],
            "prompt_info": prepared_input["prompt_info"],
            "prompt_mode": prepared_input.get("prompt_mode"),
            **({"sent_prompt": args.prompt} if client.last_request_bytes is not None
               else {"prepared_prompt": args.prompt}),
            "request_attempts": client.request_attempts,
            "warnings": prepared_input["reference_warnings"],
            "base_url": base_url,
            "base_url_source": base_source,
            "attempts": attempts,
            "ask_user_to_switch_model": not refused and automatic and len(attempts) > 1,
        }
    )
    return 1


def add_run_arguments(parser: argparse.ArgumentParser) -> None:
    prompt = parser.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file", help="Read the exact final prompt from a UTF-8 file; avoids shell quoting changes.")
    parser.add_argument(
        "--prompt-mode", choices=tuple(mode["id"] for mode in PROMPT_MODES),
        help="Prompt mode selected with Codex: restrained, master, or professional. "
             "Codex prepares --prompt; this client never calls a text model or "
             "semantically rewrites it. Omit for legacy edit-wrapper cleanup.",
    )
    parser.add_argument("--model")
    parser.add_argument("--selection-token")
    parser.add_argument("--count", type=int, default=1,
                        help="Output image count (1-4, default 1); ask together with model selection.")
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--use-last", action="store_true")
    parser.add_argument(
        "--mode", choices=("auto", "generate", "edit"), default="auto",
        help="auto detects corrections; generate disables implicit last-image reuse; "
             "edit reuses the last image if no upload is supplied. "
             "--image and --use-last always use image editing.",
    )
    parser.add_argument(
        "--endpoint", choices=("auto", "images", "chat", "custom"), default="auto"
    )
    parser.add_argument("--custom-url")
    parser.add_argument("--custom-kind", choices=("images", "chat"))
    parser.add_argument("--size")
    parser.add_argument("--aspect", help="Explicit output aspect; edits otherwise preserve original reference dimensions, new images default to square.")
    parser.add_argument("--confirm-high-res", action="store_true")
    parser.add_argument("--base-url")
    parser.add_argument("--output-dir")
    parser.add_argument("--thread-id")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument(
        "--confirm-codex-auth", action="store_true",
        help="Confirm that a Codex/Codex++ credential is a Keylink API key.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and edit images through Keylink-compatible APIs."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    models = subparsers.add_parser("models", help="List available models and published sizes.")
    models.add_argument("--base-url")
    models.add_argument("--thread-id")
    models.add_argument("--timeout", type=float, default=120.0)
    models.add_argument(
        "--confirm-codex-auth", action="store_true",
        help="Confirm that a Codex/Codex++ credential is a Keylink API key.",
    )
    models.set_defaults(handler=command_models)

    last = subparsers.add_parser("last", help="Recover saved images locally without API requests.")
    last.add_argument("--thread-id")
    last.add_argument("--preview-dir", help="Directory for small display copies; originals are preserved.")
    last.add_argument("--allow-previous", action="store_true", help="Explicitly recover a historical image when the latest request has no saved result.")
    last.set_defaults(handler=command_last)

    run = subparsers.add_parser("run", help="Generate or edit an image.")
    add_run_arguments(run)
    run.set_defaults(handler=command_run)

    start = subparsers.add_parser(
        "start", help="Start a detached image job that survives a Codex task interruption."
    )
    add_run_arguments(start)
    start.set_defaults(handler=command_start)

    status = subparsers.add_parser("status", help="Read a background image job result.")
    status.add_argument("--job-id")
    status.add_argument("--thread-id")
    status.set_defaults(handler=command_status)

    worker = subparsers.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--job-dir", required=True)
    worker.set_defaults(handler=command_worker)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    is_request = getattr(args, "command", None) in {"run", "start"}
    if is_request and not 1 <= args.count <= OUTPUT_MAX_COUNT:
        raise ClientError(f"--count must be between 1 and {OUTPUT_MAX_COUNT}. No request was sent.")
    if is_request and getattr(args, "prompt_file", None) is not None:
        try:
            # Ignore an optional encoding signature, preserving content and CRLF.
            args.prompt = Path(args.prompt_file).expanduser().read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeError) as error:
            raise ClientError(f"Cannot read --prompt-file as UTF-8: {error}. No request was sent.") from error
    if is_request and not args.prompt.strip():
        raise ClientError("A non-empty --prompt is required. No request was sent.")
    if is_request and not args.model:
        raise ClientError(
            "--model is required. Run models, show the available image models to the user, "
            "and wait for their choice."
        )
    if is_request and not args.selection_token:
        raise ClientError(
            "--selection-token is required. Run models immediately before this request, "
            "show the available image models, and wait for the user's choice."
        )
    if getattr(args, "endpoint", None) == "custom":
        if not args.custom_url or not args.custom_kind:
            raise ClientError(
                "Custom endpoint mode requires --custom-url and --custom-kind chat|images."
            )
        parsed = urllib.parse.urlsplit(args.custom_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ClientError(f"Invalid custom endpoint URL: {args.custom_url}")
    elif getattr(args, "custom_url", None) or getattr(args, "custom_kind", None):
        raise ClientError("--custom-url and --custom-kind require --endpoint custom.")


def main(argv: list[str] | None = None, *, prepared_input: dict[str, Any] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args._prepared_input = prepared_input
    try:
        validate_args(args)
        return int(args.handler(args))
    except (CredentialConfirmationRequired, CodexAccountCredentialError, CCSwitchDetectionRequired) as error:
        payload = {"status": "error", "error": str(error)}
        payload.update(error.payload)
        emit(payload)
        return 1
    except ClientError as error:
        emit({"status": "error", "error": str(error)})
        return 1
    except KeyboardInterrupt:
        emit({"status": "error", "error": "Interrupted."})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
