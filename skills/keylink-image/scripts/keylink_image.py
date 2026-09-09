#!/usr/bin/env python3
"""Keylink image generation and editing client for Codex skills."""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import datetime as dt
import errno
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import struct
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
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
)
CONSERVATIVE_SIZES = ("1024x1024", "1536x1024", "1024x1536")
EXPERIMENTAL_SIZES = ("2560x1440", "3840x2160")
SIZE_RE = re.compile(r"(?<!\d)(\d{2,5})[xX](\d{2,5})(?!\d)")
DATA_URL_RE = re.compile(
    r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=_\s-]+)", re.I
)
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", re.I)
HTTP_URL_RE = re.compile(r"https?://[^\s<>\]\[\"']+", re.I)
HIGH_RES_INTENT_RE = re.compile(
    r"更高分辨率|高分辨率|更清晰|高清|超高清|\b(?:2k|4k|uhd)\b|"
    r"higher\s+resolution|high\s+resolution|ultra\s+high\s+definition",
    re.I,
)
CORRECTION_INTENT_RE = re.compile(
    r"不满意|画面不对|刚才|上一张|上张|这张图|这幅图|继续修改|继续改|"
    r"把.+改成|换成|改一下|去掉|移除|"
    r"previous\s+image|last\s+image|just\s+generated|this\s+image|"
    r"edit\s+it|change\s+only|keep\s+everything\s+else",
    re.I,
)


class ClientError(RuntimeError):
    """Expected user-facing client failure."""


class EndpointError(ClientError):
    def __init__(self, endpoint: str, message: str):
        super().__init__(message)
        self.endpoint = endpoint


@dataclass(frozen=True)
class ImageCandidate:
    kind: str
    value: str
    mime_type: str | None = None


@dataclass
class CCSwitchState:
    proxy_base_url: str | None = None
    provider_base_urls: list[str] | None = None
    api_key: str | None = None

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


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


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


def discover_codex_api_key() -> str | None:
    data = read_json_file(codex_home() / "auth.json")
    return recursive_lookup(data, {"OPENAI_API_KEY", "KEYLINK_API_KEY"})


def discover_ccswitch_state() -> CCSwitchState:
    state = CCSwitchState()
    db_path = Path.home() / ".cc-switch" / "cc-switch.db"
    if not db_path.is_file():
        return state

    connection: sqlite3.Connection | None = None
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        connection.row_factory = sqlite3.Row

        proxy = connection.execute(
            "SELECT proxy_enabled, listen_address, listen_port, enabled "
            "FROM proxy_config WHERE app_type = 'codex'"
        ).fetchone()
        if proxy and (bool(proxy["proxy_enabled"]) or bool(proxy["enabled"])):
            host = str(proxy["listen_address"] or "127.0.0.1")
            port = int(proxy["listen_port"] or 15721)
            state.proxy_base_url = f"http://{host}:{port}"

        provider = connection.execute(
            "SELECT id, settings_config, website_url FROM providers "
            "WHERE app_type = 'codex' AND is_current = 1 "
            "ORDER BY sort_index LIMIT 1"
        ).fetchone()
        if not provider:
            return state

        endpoints = connection.execute(
            "SELECT url FROM provider_endpoints "
            "WHERE app_type = 'codex' AND provider_id = ? ORDER BY id",
            (provider["id"],),
        ).fetchall()
        for row in endpoints:
            value = row["url"]
            if isinstance(value, str) and value.strip():
                state.provider_base_urls.append(value.strip())

        website_url = provider["website_url"]
        if isinstance(website_url, str) and website_url.strip():
            state.provider_base_urls.append(website_url.strip())

        try:
            settings = json.loads(provider["settings_config"] or "{}")
        except json.JSONDecodeError:
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        provider_url = base_url_from_codex_config(settings.get("config"))
        if provider_url:
            state.provider_base_urls.insert(0, provider_url)
        state.api_key = recursive_lookup(
            settings, {"OPENAI_API_KEY", "KEYLINK_API_KEY"}
        )
    except (sqlite3.Error, OSError, ValueError):
        return state
    finally:
        if connection is not None:
            connection.close()

    state.provider_base_urls = list(dict.fromkeys(state.provider_base_urls))
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
    ccswitch = discover_ccswitch_state()
    if explicit:
        return normalize_base_url(explicit), "argument", ccswitch

    value = os.environ.get("KEYLINK_BASE_URL")
    if value:
        return normalize_base_url(value), "environment:KEYLINK_BASE_URL", ccswitch

    # Codex/CCSwitch endpoints may only support text or Responses traffic.
    return normalize_base_url(DEFAULT_BASE_URL), "default", ccswitch


def normalized_host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_loopback(url: str) -> bool:
    return normalized_host(url) in {"127.0.0.1", "localhost", "::1"}


def discover_api_key(
    base_url: str, base_source: str, ccswitch: CCSwitchState
) -> tuple[str | None, str | None]:
    for env_name in ("KEYLINK_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(env_name)
        if value:
            return value, f"environment:{env_name}"

    ccswitch_hosts = {
        normalized_host(value) for value in ccswitch.provider_base_urls if value
    }
    may_use_ccswitch_key = (
        base_source.startswith("ccswitch")
        or (base_source == "codex-config" and is_loopback(base_url))
        or normalized_host(base_url) in ccswitch_hosts
    )
    if may_use_ccswitch_key and ccswitch.api_key:
        return ccswitch.api_key, "ccswitch-current-provider"

    if base_source == "codex-config":
        key = discover_codex_api_key()
        if key:
            return key, "codex-auth"

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
        headers = self.headers(json_body=payload is not None)
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        raw, _ = self._open(request)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ClientError(
                f"{method} {url} returned non-JSON content: {safe_response_text(raw)}"
            ) from error

    def request_multipart(
        self,
        url: str,
        fields: dict[str, str],
        image_paths: list[Path],
    ) -> Any:
        body, content_type = encode_multipart(fields, image_paths)
        headers = self.headers()
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        raw, _ = self._open(request)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ClientError(
                f"POST {url} returned non-JSON content: {safe_response_text(raw)}"
            ) from error

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
        try:
            with (opener or self.opener).open(request, timeout=self.timeout) as response:
                content_type = response.headers.get_content_type()
                return response.read(), content_type
        except urllib.error.HTTPError as error:
            raw = error.read()
            detail = safe_response_text(raw) or error.reason
            raise ClientError(
                f"HTTP {error.code} for {request.full_url}: {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise ClientError(f"Request failed for {request.full_url}: {error.reason}") from error
        except OSError as error:
            raise ClientError(f"Request failed for {request.full_url}: {error}") from error


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


def choose_size(explicit_size: str | None, aspect: str) -> str:
    if explicit_size:
        width, height = parse_size(explicit_size)
        return f"{width}x{height}"
    normalized = aspect.lower().replace(" ", "")
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
            }
        )
    return result


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
    lock_path = state_dir / ".model-selection.lock"
    lock_fd: int | None = None
    for _ in range(100):
        try:
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.05)
    if lock_fd is None:
        raise ClientError("The current model selection is already being used by another request.")
    try:
        data = validate_model_selection(state_dir, selection_token, model, base_url)
        data["used_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        write_json_file(state_dir / "model-selection.json", data)
    finally:
        os.close(lock_fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def validate_reference_paths(paths: Iterable[str]) -> list[Path]:
    result: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise ClientError(f"Reference image does not exist: {path}")
        result.append(path)
    return result


def save_last_state(
    state_dir: Path,
    image_path: Path,
    model: str,
    endpoint: str,
    requested_size: str,
    result: dict[str, Any] | None = None,
) -> None:
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
    write_json_file(state_dir / "last.json", payload)


def build_chat_payload(
    prompt: str, model: str, size: str, references: list[Path]
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    compatible_images: list[dict[str, str]] = []
    for path in references:
        data_url = image_to_data_url(path)
        content.append(
            {"type": "image_url", "image_url": {"url": data_url}}
        )
        compatible_images.append({"image_url": data_url})
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
    return client.request_json("POST", url, payload), "chat"


def attempt_images(
    client: HttpClient,
    prompt: str,
    model: str,
    size: str,
    references: list[Path],
    custom_url: str | None = None,
) -> tuple[Any, str]:
    if references:
        url = custom_url or endpoint_url(client.base_url, "/v1/images/edits")
        fields = {"model": model, "prompt": prompt, "size": size, "n": "1"}
        return client.request_multipart(url, fields, references), "images-edits"

    url = custom_url or endpoint_url(client.base_url, "/v1/images/generations")
    payload = {"model": model, "prompt": prompt, "size": size, "n": 1}
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

    def from_string(value: str, hint: str | None = None) -> None:
        stripped = value.strip()
        if not stripped:
            return
        data_match = DATA_URL_RE.fullmatch(stripped)
        if data_match:
            add_candidate(candidates, seen, ImageCandidate("data_url", stripped, data_match.group(1)))
            return
        if hint in {"b64_json", "base64", "image_base64", "b64"}:
            add_candidate(candidates, seen, ImageCandidate("base64", stripped))
            return
        if hint in {"url", "image_url"} and stripped.lower().startswith(("http://", "https://")):
            add_candidate(candidates, seen, ImageCandidate("url", stripped))
            return
        for match in DATA_URL_RE.finditer(stripped):
            add_candidate(
                candidates,
                seen,
                ImageCandidate("data_url", match.group(0), match.group(1)),
            )
        for match in MARKDOWN_IMAGE_RE.finditer(stripped):
            add_candidate(candidates, seen, ImageCandidate("url", match.group(1).rstrip(".,;")))
        if hint in {"content", "text", "output_text"}:
            for match in HTTP_URL_RE.finditer(stripped):
                add_candidate(candidates, seen, ImageCandidate("url", match.group(0).rstrip(".,;:)")))

    def walk(value: Any, hint: str | None = None, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(value, str):
            from_string(value, hint)
            return
        if isinstance(value, list):
            for item in value:
                walk(item, hint, depth + 1)
            return
        if not isinstance(value, dict):
            return

        image_url = value.get("image_url")
        if isinstance(image_url, dict):
            walk(image_url.get("url"), "image_url", depth + 1)
        elif isinstance(image_url, str):
            walk(image_url, "image_url", depth + 1)

        for key in ("url", "b64_json", "base64", "image_base64", "b64"):
            if key in value:
                walk(value[key], key, depth + 1)

        for key in (
            "data",
            "images",
            "output",
            "result",
            "results",
            "choices",
            "message",
            "content",
            "parts",
            "attachments",
        ):
            if key in value:
                walk(value[key], key, depth + 1)

        item_type = str(value.get("type", "")).lower()
        if "image" in item_type:
            for key, nested in value.items():
                if key not in {"type", "url", "image_url", "b64_json", "base64", "image_base64", "b64"}:
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
) -> tuple[list[dict[str, Any]], list[str]]:
    if not candidates:
        raise ClientError("The endpoint returned HTTP 200 but no image was found.")
    output_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, Any]] = []
    failures: list[str] = []
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")

    for index, candidate in enumerate(candidates, start=1):
        try:
            raw, declared_mime = decode_candidate(candidate, client)
            detected_mime = detect_mime_type(raw)
            mime_type = detected_mime or declared_mime
            if not raw or not mime_type or not mime_type.startswith("image/"):
                raise ClientError("Downloaded or decoded content is not a recognized image.")
            extension = extension_for_mime(mime_type)
            filename = (
                f"{timestamp}-{safe_slug(model)}-{safe_slug(endpoint_label)}-{index}{extension}"
            )
            path = (output_dir / filename).resolve()
            path.write_bytes(raw)
            dimensions = detect_dimensions(raw, mime_type)
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
        raise ClientError("All returned images failed to save: " + "; ".join(failures))
    return saved, failures


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


def validate_size_request(args: argparse.Namespace) -> str:
    size = choose_size(args.size, args.aspect)
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
        raise ClientError(
            f"Size {size} requires user confirmation. Run models, present published and "
            "experimental sizes, then retry with --confirm-high-res."
        )
    return size


def run_argv(args: argparse.Namespace, thread_id: str) -> list[str]:
    result = [
        "run", "--prompt", args.prompt, "--model", args.model,
        "--selection-token", args.selection_token, "--mode", args.mode,
        "--endpoint", args.endpoint, "--aspect", args.aspect,
        "--thread-id", thread_id, "--timeout", str(args.timeout),
    ]
    for path in args.image:
        result.extend(("--image", path))
    if args.use_last:
        result.append("--use-last")
    if args.confirm_high_res:
        result.append("--confirm-high-res")
    for flag, value in (
        ("--custom-url", args.custom_url), ("--custom-kind", args.custom_kind),
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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        return error.errno != errno.ESRCH
    return True


def command_start(args: argparse.Namespace) -> int:
    base_url, _, _ = discover_base_url(args.base_url)
    thread_id = resolve_thread_id(args.thread_id)
    state_dir = thread_state_dir(thread_id)
    size = validate_size_request(args)
    validate_model_selection(
        state_dir, args.selection_token, args.model, base_url
    )
    validate_reference_paths(args.image)

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
    }
    job = {
        "job_id": job_id, "status": "starting", "thread_id": thread_id,
        "model": args.model, "requested_size": size, "started_at": started_at,
        "cwd": request["cwd"],
    }
    write_json_file(directory / "request.json", request)
    write_json_file(directory / "job.json", job)
    write_json_file(state_dir / "latest-job.json", {"job_id": job_id})

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
    try:
        process = subprocess.Popen(command, **popen_kwargs)
    except OSError as error:
        job.update(status="failed", finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                   error=f"Could not start background worker: {error}")
        write_json_file(directory / "job.json", job)
        try:
            claim.unlink()
        except OSError:
            pass
        raise ClientError(job["error"]) from error

    emit({
        "status": "started", "background": True, "job_id": job_id,
        "pid": process.pid, "thread_id": thread_id, "model": args.model,
        "requested_size": size, "started_at": started_at,
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
            exit_code = main(argv)
    except BaseException as error:
        with temporary.open("w", encoding="utf-8") as output:
            emit({"status": "error", "error": f"Background worker failed: {error}"}, stream=output)
        exit_code = 1
    temporary.replace(directory / "result.json")
    payload = read_json_file(directory / "result.json")
    job.update(
        status="succeeded" if exit_code == 0 and isinstance(payload, dict)
        and payload.get("status") == "ok" else "failed",
        exit_code=exit_code,
        finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    write_json_file(directory / "job.json", job)
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
    if isinstance(payload, dict):
        result = dict(payload)
        result.update(background=True, job_id=job_id, job_status=job.get("status"))
        emit(result)
        return 0 if result.get("status") == "ok" else 1
    status = str(job.get("status", "unknown"))
    if status in {"starting", "running"} and job.get("pid") and not process_is_running(job.get("pid")):
        status = "failed"
        job.update(
            status=status, finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            error="The background worker stopped before writing a result.",
        )
        write_json_file(directory / "job.json", job)
    emit({
        "status": status, "background": True, "job_id": job_id,
        "thread_id": thread_id, "model": job.get("model"),
        "requested_size": job.get("requested_size"), "started_at": job.get("started_at"),
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
    first_path = load_last_image(state_dir)
    result = state.get("result")
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
        saved.append({
            "path": str(path.resolve()), "mime_type": mime, "bytes": len(raw),
            "width": dimensions[0] if dimensions else None,
            "height": dimensions[1] if dimensions else None,
        })
    if not saved:
        raise ClientError("No image files are recorded for the latest result.")
    preview_dir = Path(args.preview_dir).expanduser().resolve() if args.preview_dir else None
    warnings = list(result.get("warnings", []))
    warnings.extend(prepare_display(saved, preview_dir))
    result.update(
        status="ok", recovered=True, thread_id=thread_id,
        saved_at=state.get("saved_at"), images=saved, warnings=warnings,
    )
    emit(result)
    return 0


def command_models(args: argparse.Namespace) -> int:
    base_url, base_source, ccswitch = discover_base_url(args.base_url)
    api_key, credential_source = discover_api_key(base_url, base_source, ccswitch)
    if not api_key and not is_loopback(base_url):
        raise ClientError(
            "No API credential was found. Set KEYLINK_API_KEY or OPENAI_API_KEY."
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
    api_key, credential_source = discover_api_key(base_url, base_source, ccswitch)
    if not api_key and not is_loopback(base_url):
        raise ClientError(
            "No API credential was found. Set KEYLINK_API_KEY or OPENAI_API_KEY."
        )
    client = HttpClient(base_url, api_key, args.timeout)

    model = args.model

    size = validate_size_request(args)

    thread_id = resolve_thread_id(args.thread_id)
    state_dir = thread_state_dir(thread_id)
    references = validate_reference_paths(args.image)
    reference_source = "uploaded" if references else "none"
    should_use_last = (
        args.use_last
        or args.mode == "edit"
        or (args.mode == "auto" and has_correction_intent(args.prompt))
    )
    if not references and should_use_last:
        references = [load_last_image(state_dir)]
        reference_source = (
            "last-successful-image"
            if args.use_last or args.mode == "edit"
            else "auto-correction-last-image"
        )

    if args.mode == "edit" and not references:
        raise ClientError("Edit mode requires --image or --use-last.")
    if args.mode == "generate" and references:
        raise ClientError("Generate mode cannot discard reference images. Use auto or edit mode.")

    consume_model_selection(state_dir, args.selection_token, model, base_url)

    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else state_dir.resolve()
    )

    attempts: list[dict[str, str]] = []
    order = endpoint_order(model, args.endpoint)
    for route in order:
        if route == "custom":
            route_kind = args.custom_kind
            custom_url = args.custom_url
        else:
            route_kind = route
            custom_url = None

        endpoint_label = route_kind
        try:
            if route_kind == "chat":
                response, endpoint_label = attempt_chat(
                    client, args.prompt, model, size, references, custom_url
                )
            elif route_kind == "images":
                response, endpoint_label = attempt_images(
                    client, args.prompt, model, size, references, custom_url
                )
            else:
                raise ClientError(f"Unsupported endpoint kind: {route_kind}")

            candidates = extract_image_candidates(response)
            saved, partial_failures = save_candidates(
                candidates, client, output_dir, model, endpoint_label
            )
            first_path = Path(saved[0]["path"])

            warnings: list[str] = []
            if attempts:
                warnings.append(
                    f"Succeeded through {endpoint_label} after earlier endpoint failure."
                )
            warnings.extend(partial_failures)
            if endpoint_label == "chat" and size not in CONSERVATIVE_SIZES:
                warnings.append(
                    "Chat accepted the request, but the requested pixel dimensions are not guaranteed."
                )

            warnings.extend(prepare_display(saved))
            result = {
                    "status": "ok",
                    "model": model,
                    "endpoint": endpoint_label,
                    "requested_size": size,
                    "reference_source": reference_source,
                    "references": [str(path) for path in references],
                    "thread_id": thread_id,
                    "base_url": base_url,
                    "base_url_source": base_source,
                    "credential_source": credential_source or "none",
                    "images": saved,
                    "failed_attempts": attempts,
                    "warnings": warnings,
                }
            save_last_state(state_dir, first_path, model, endpoint_label, size, result)
            emit(result)
            return 0
        except ClientError as error:
            attempts.append({"endpoint": endpoint_label, "error": str(error)})

    automatic = args.endpoint == "auto"
    emit(
        {
            "status": "error",
            "model": model,
            "requested_size": size,
            "reference_source": reference_source,
            "base_url": base_url,
            "base_url_source": base_source,
            "attempts": attempts,
            "ask_user_to_switch_model": automatic and len(attempts) > 1,
        }
    )
    return 1


def add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--model")
    parser.add_argument("--selection-token")
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--use-last", action="store_true")
    parser.add_argument("--mode", choices=("auto", "generate", "edit"), default="auto")
    parser.add_argument(
        "--endpoint", choices=("auto", "images", "chat", "custom"), default="auto"
    )
    parser.add_argument("--custom-url")
    parser.add_argument("--custom-kind", choices=("images", "chat"))
    parser.add_argument("--size")
    parser.add_argument("--aspect", default="square")
    parser.add_argument("--confirm-high-res", action="store_true")
    parser.add_argument("--base-url")
    parser.add_argument("--output-dir")
    parser.add_argument("--thread-id")
    parser.add_argument("--timeout", type=float, default=600.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and edit images through Keylink-compatible APIs."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    models = subparsers.add_parser("models", help="List available models and published sizes.")
    models.add_argument("--base-url")
    models.add_argument("--thread-id")
    models.add_argument("--timeout", type=float, default=120.0)
    models.set_defaults(handler=command_models)

    last = subparsers.add_parser("last", help="Recover saved images locally without API requests.")
    last.add_argument("--thread-id")
    last.add_argument("--preview-dir", help="Directory for small display copies; originals are preserved.")
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        return int(args.handler(args))
    except ClientError as error:
        emit({"status": "error", "error": str(error)})
        return 1
    except KeyboardInterrupt:
        emit({"status": "error", "error": "Interrupted."})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
