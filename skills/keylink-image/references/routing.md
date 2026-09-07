# Routing and compatibility

## API address

Send requests directly to `https://keylinkclub.com`, unless the user explicitly sets `--base-url` or `KEYLINK_BASE_URL`. Do not inherit `OPENAI_BASE_URL`, `OPENAI_API_BASE`, Codex provider URLs, or the CCSwitch local API listener. CCSwitch's Codex listener can return 404 for Images and reject or transform Chat requests. Its current Keylink credential can still be reused without routing any requests through the listener. Model discovery uses the same selected host and does not fall back to other providers. Ordinary HTTP network proxies remain the transport's responsibility.

## Automatic endpoint order

| Model family | Text generation | Reference-image edit |
| --- | --- | --- |
| `gpt-image-2` and `gpt-image-*` | Images Generations, then Chat | Images Edits, then Chat |
| `gemini-3-pro-image`, `gemini-3.1-flash-image`, and Gemini image IDs | Images Generations, then Chat | Images Edits, then Chat |
| Other pass-through model IDs | Images, then Chat | Images Edits, then Chat |

Automatic fallback occurs for HTTP errors, successful responses with no image, and image download or decoding failures. Both attempts use the same model ID, prompt, size, and references. Explicit `images`, `chat`, and custom endpoint modes make exactly one attempt.

Images generation uses JSON with `POST /v1/images/generations`. Images editing uses multipart form data with repeated `image` file fields at `POST /v1/images/edits`.

Chat uses `POST /v1/chat/completions`. References are sent both as standard `messages[].content[].image_url` content and as compatible top-level `images[].image_url` values.

## Sizes

Before every request, `models` must query `GET /v1/models` and the user must choose from the returned catalog. `run` requires the one-use selection token returned by that query. Tokens are bound to the Codex task and API host and cannot be reused for another image request.

The conservative size set is:

- `1024x1024`
- `1536x1024`
- `1024x1536`

When the user requests higher resolution, fetch the model catalog first. Present the server-published sizes and label `2560x1440` or `3840x2160` as attempts that the channel may reject when they are not published. Confirmation is required before passing a non-conservative size to `run`.

Before sending `3840x2160`, tell the user that 4K generation can take several minutes and ask them to wait patiently. While the request is active, keep waiting for that process instead of submitting the same request again.

Do not claim a lower-resolution result is 2K or 4K. Do not retry a confirmed high-resolution request at a smaller size. For Chat results, requested pixel dimensions are advisory and are not guaranteed by the endpoint.

## Response formats

Saving an image and displaying it are separate stages. Codex's image viewer may fail to load a large 4K PNG with an `invalid base64`/`Invalid padding` error even when the file is intact. The client generates a small JPEG display copy with optional Pillow and always preserves the original for delivery and later edits. Preview failures do not trigger endpoint fallback or another generation. `last` recovers saved results without API calls, including results saved by older client versions.

The client accepts image URLs, `data:image/...;base64,...` values, common Base64 fields such as `b64_json`, and image values inside Chat message content or message image arrays. URL downloads inherit authentication only when the image URL uses the same host as the configured API root, preventing credentials from being sent to unrelated hosts.

If a configured system HTTP proxy fails to connect to a returned image URL, retry downloading that same image directly once before treating the endpoint as failed. HTTP error responses are not retried this way. This download recovery does not issue another generation request or alter model, prompt, size, or endpoint selection.
