---
name: keylink-image
description: Generate or edit bitmap images through Keylink's OpenAI-compatible API, including reference-image continuation, model discovery, endpoint fallback, and saved local results. Use for image creation or image editing when Keylink is the requested or configured provider. Do not use for SVG or code-native graphics.
---

# Keylink Image

This is a standalone Codex skill. Resolve paths relative to the directory containing this `SKILL.md`. Use the bundled client at `scripts/keylink_image.py` with Python 3.11 or newer. On Windows, `scripts/keylink-image.ps1` resolves the Python runtime automatically. On macOS/Linux, use `sh scripts/keylink-image.sh`; it checks the skill's `.venv`, PATH, and Homebrew locations, or accepts an explicit `KEYLINK_PYTHON` interpreter. Keep API keys out of command-line arguments and output.

## Decide the operation

1. Treat an explicitly uploaded image as the reference image, even when a previous result exists.
2. When the user says the result is unsatisfactory, visually wrong, or asks to change the image just produced, edit the latest successful image for this Codex task. Pass only the requested changes as `--prompt` and add `--use-last`.
3. When it is unclear whether the user wants a new image or an edit, ask which operation they intend. After they choose editing, reuse the latest image without asking them to upload it again.
4. Pass explicit uploads with one `--image <path>` per image. Uploaded images take priority over `--use-last` inside the client.
5. Preserve the user's model ID exactly. Model IDs are pass-through values, including `gpt-image-2.5`, `gpt-image-2.5-sunburst`, and `gpt-image-2.5-flare`. Treat variants as separate catalog choices; never strip their suffixes or substitute the base model during generation, editing, or fallback.

## Choose model, size, and endpoint

- Before every generation or edit, run `models`. It calls `GET /v1/models`, saves a one-use selection token for the current Codex task, and reports server-published sizes.
- Show the returned `image_models` to the user and wait for the user to choose one. Never choose a model automatically, silently reuse the previous model, or start an image request in the same turn as model discovery unless the user already replied with a choice from that exact discovery result.
- Pass the chosen model unchanged with `--model` and pass the returned token with `--selection-token`. The client rejects missing, stale, reused, wrong-task, wrong-host, and unlisted model selections. A new generation or edit requires a fresh `models` call and a fresh user choice.
- Prefer `1024x1024`, `1536x1024`, or `1024x1536` for `gpt-image-2`, the `gpt-image-2.5` family (including `sunburst` and `flare`), and supported Gemini image models. Choose among them from the requested aspect ratio. Read sizes for the exact selected model ID; do not inherit published sizes from its base model or another variant. Do not assume `2048x2048` when the catalog does not publish it.
- Treat "更高分辨率", "高分辨率", "高清", "超高清", "2K", "4K", "UHD", or an explicitly larger pixel size as high-resolution intent. Show the published sizes plus relevant experimental candidates such as `2560x1440` and `3840x2160`, then wait for user confirmation before running the image request.
- Before starting a confirmed `3840x2160` request, tell the user that 4K generation can take several minutes and ask them to wait patiently. Continue waiting on the same request and do not submit duplicates while it is still running.
- After confirmation, pass `--confirm-high-res`. Never locally upscale, silently downgrade to 1K, or change the model. A Chat success may not honor the requested pixels; state that limitation and report detected dimensions when available.
- Use `--endpoint auto` unless the user explicitly fixes `images`, `chat`, or a custom endpoint. Explicit endpoint choices never fall back.
- For custom endpoints, pass `--endpoint custom --custom-url <url> --custom-kind chat|images`.

Read [references/routing.md](references/routing.md) when endpoint order, high-resolution handling, or response compatibility affects the request.

## Run the client

Use JSON output to determine the saved image path and warnings:

```powershell
& "<skill-dir>\scripts\keylink-image.ps1" models
& "<skill-dir>\scripts\keylink-image.ps1" run --prompt "..." --model "gpt-image-2" --selection-token "<token-from-models>" --aspect landscape --endpoint auto
& "<skill-dir>\scripts\keylink-image.ps1" run --prompt "change only the sky to sunset" --model "gpt-image-2" --selection-token "<fresh-token>" --use-last
& "<skill-dir>\scripts\keylink-image.ps1" run --prompt "..." --model "gemini-3.1-flash-image" --selection-token "<fresh-token>" --image "C:\path\reference.png"
```

On macOS/Linux, use the same arguments with the shell launcher, keeping paths quoted:

```sh
sh "<skill-dir>/scripts/keylink-image.sh" models
sh "<skill-dir>/scripts/keylink-image.sh" run --prompt "..." --model "gpt-image-2" --selection-token "<token-from-models>" --aspect landscape
sh "<skill-dir>/scripts/keylink-image.sh" run --prompt "change only the sky to sunset" --model "gpt-image-2" --selection-token "<fresh-token>" --use-last
sh "<skill-dir>/scripts/keylink-image.sh" run --prompt "..." --model "gemini-3.1-flash-image" --selection-token "<fresh-token>" --image "/Users/name/Pictures/reference image.png"
```

Send model discovery, generation, and editing requests directly to `https://keylinkclub.com`. Do not route them through Codex or CCSwitch's local API listener. The only address overrides are an explicit `--base-url` or `KEYLINK_BASE_URL`; generic `OPENAI_BASE_URL` and `OPENAI_API_BASE` are ignored. HTTP/SOCKS network proxies are separate from the API base URL; do not use their listening port as `--base-url`.

Credentials are read without printing them from `KEYLINK_API_KEY`, `OPENAI_API_KEY`, or the current CCSwitch provider in `~/.cc-switch/cc-switch.db` when its host matches the selected Keylink API host. CCSwitch is a credential source only. If none is available, explain which environment variables can provide it. A macOS app opened from Finder may not inherit terminal exports; use its existing matching CCSwitch provider or launch Codex from the configured terminal.

The client uses Python's system/environment HTTP proxy support, including `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY`. For a VPN, use its HTTP/mixed port or system tunnel; a SOCKS-only URL is not supported by this transport. Do not change the API host to address a network proxy issue.

The latest successful image is stored under `.keylink-image/threads/<thread-id>/` in the current workspace. The client uses `CODEX_THREAD_ID` or `CODEX_SESSION_ID` automatically and records the latest image only after it is saved successfully.

## Return the result

- Display each image using the returned `display_markdown` and include its `original_markdown` link. Paths use forward slashes and angle brackets for Windows/space compatibility.
- Large images get a separate JPEG display preview when Pillow is available. This preview is only for inspection/display; the original file and its actual dimensions remain the deliverable and the reference for subsequent edits. Never pass the preview as the next reference image.
- If `view_image` fails with `invalid base64`, `Invalid padding`, or an oversized payload, use `display_path`. A preview error does not mean generation failed; still return the saved original link, without sending a new image request.
- Include the model, endpoint actually used, requested size, and detected pixel size when available.
- If both automatic endpoints fail, summarize both errors and ask whether to switch models. Do not switch models before the user agrees.
- If the user explicitly fixed an endpoint, report that endpoint's failure without trying another one.

## Recover a missing display

When the user reports success without an image, or resumes an interrupted generation, run `last` in the original workspace with the original `--thread-id` first. It reads the saved result and creates a small preview locally; no credentials, model selection, or network requests are needed. Compare `saved_at` with the request time so an older result is not mistaken for a still-running request. If the generation session is still active, poll that same session for its final result.

```powershell
& "<skill-dir>\scripts\keylink-image.ps1" last --thread-id "<original-task-id>"
```

Use `--preview-dir <writable-directory>` when recovering from a workspace that cannot be written. Pillow is optional for generation; it is needed to create display previews. If unavailable, return the original image link and explain the preview limitation.

On macOS/Linux, recovery uses `sh "<skill-dir>/scripts/keylink-image.sh" last --thread-id "<original-task-id>"`. To enable previews without modifying system Python, create a Python 3.11+ virtual environment at `<skill-dir>/.venv` and install Pillow into it; the shell launcher discovers it automatically.
