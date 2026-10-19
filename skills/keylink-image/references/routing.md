# Routing and compatibility

## API address

Send requests directly to `https://keylinkclub.com`, unless the user explicitly sets `--base-url` or `KEYLINK_BASE_URL`. Do not inherit `OPENAI_BASE_URL`, `OPENAI_API_BASE`, Codex provider URLs, or the CCSwitch local API listener. CCSwitch's Codex listener can return 404 for Images and reject or transform Chat requests. Its current Keylink credential can still be reused without routing any requests through the listener. Model discovery uses the same selected host and does not fall back to other providers. Ordinary HTTP network proxies remain the transport's responsibility.

Credential selection is separate from request routing. An explicit `KEYLINK_API_KEY` wins. Otherwise prefer the Keylink key in a running CCSwitch. For older versions, retain `providers.is_current` selection; respect device-local `settings.json.currentProviderCodex` when present. New routing uses `live-state.json.apps.codex.proxy_route`. Aggregation is active only when `mode` is `proxy` and `stack.enabled` is true: consider that route and `stack.members`, not the historical `stack.keys` registry. Match provider API hosts to the request host; use `website_url` only if no remote API endpoint is available. Do not assume a default OpenAI account or another provider supplies a Keylink key. Ambiguous matching keys require an explicit default-provider choice or `KEYLINK_API_KEY`. Missing optional proxy/endpoint tables or newer columns must not disable legacy provider reads. These files and the SQLite database are read-only.

If no CCSwitch key is available, check common Codex/Codex++ auth/config locations for account login (`auth_mode` indicating ChatGPT, or populated login token fields). If signed in, stop with `codex_account_detected` and `manual_key_required`: the user must manually configure a Keylink provider in running CCSwitch or set `KEYLINK_API_KEY`. Do not reuse account auth/config keys or `OPENAI_API_KEY`, even with `--confirm-codex-auth`. Preserve account login files.

Process/listener probes use three states. A positive probe confirms running. A successful process query without a match and an absent/refused listener confirm stopped. Permission errors, nonzero `tasklist` failures, failed `pgrep` queries (exit 2+), unavailable commands and timeouts leave the state unknown unless the other probe confirms running. Unknown returns `ccswitch_detection_required` and `requires_host_execution` before selecting any implicit credential. Follow the approved host retry described in `SKILL.md`; never treat unreadable process state as proof that CCSwitch is stopped or running.

If no account login is detected, API-key-only Codex/Codex++ auth/config files and `OPENAI_API_KEY` may be offered as fallback whether CCSwitch is stopped or has no usable key. Return `credential_confirmation_required`, then require confirmation that the candidate belongs to Keylink before retrying with `--confirm-codex-auth`. Reject `PROXY_MANAGED` placeholders and never extract OAuth access, refresh or ID tokens as API keys.

## Automatic endpoint order

| Model family | Text generation | Reference-image edit |
| --- | --- | --- |
| `gpt-image-2` and `gpt-image-*` | Images Generations, then Chat | Images Edits, then Chat |
| `gpt-image-2.5`, `gpt-image-2.5-sunburst`, `gpt-image-2.5-flare` | Images Generations, then Chat | Images Edits, then Chat |
| `gemini-3-pro-image`, `gemini-3.1-flash-image`, and Gemini image IDs | Images Generations, then Chat | Images Edits, then Chat |
| Other pass-through model IDs | Images, then Chat | Images Edits, then Chat |

Automatic fallback occurs for HTTP errors, successful responses with no image, and image download or decoding failures. Both endpoints use the same model ID, cleaned prompt, requested output size, and prepared references. Explicit `images`, `chat`, and custom endpoint modes stay on that endpoint.

The `gpt-image-2.5` variants are distinct pass-through IDs, not aliases for `gpt-image-2.5` or `gpt-image-2`. Display only models returned by the current catalog, preserve variant suffixes in every request and saved result, and read published sizes from each variant's own catalog entry. New model names alone do not imply new size support or different API fields.

Images generation uses JSON with `POST /v1/images/generations`. Images editing uses multipart form data with repeated `image` file fields at `POST /v1/images/edits`.

Chat uses `POST /v1/chat/completions`. References are sent both as standard `messages[].content[].image_url` content and as compatible top-level `images[].image_url` values.

The top-level compatibility field initially uses a data URL string, preserving channels that already accept it. Only a 400/422 rejection explicitly naming `images[].image_url` as missing/invalid triggers one retry on the same Chat endpoint with `{ "image_url": { "url": "data:..." } }`. This also handles a JSON `status_code` error wrapped in HTTP 200. Disconnects, 413, unrelated errors and missing-image responses do not trigger a format retry. Do not assume that a required-field error alone proves which schema the channel supports.

## Edit prompts and reference budgets

References determine the operation for both `run` and `start`: `--image` or `--use-last` uses Images Edits first, then Chat in automatic endpoint mode, even with `--mode generate`. Explicit endpoint choices still apply. Prefer `--mode auto` when supplying references. For an independent text-only generation, omit both reference options and use `--mode generate` to disable correction-based last-image reuse.

Only the current edit instructions belong in the prompt. The client unwraps explicit JSON `prompt`/`messages`, transcript role blocks, edit headings and tagged context. Ambiguous remaining context or more than 4,000 characters after cleanup stops locally so Codex can produce a concise prompt while retaining required image text and constraints. Ordinary generation prompts are preserved. No semantic truncation or automatic removal of visual details is performed.

Default **client budgets**, not claims about Keylink's model limits:

- At most 16 references, longest edge 2048 pixels, no cropping or upscaling.
- At most 1 MiB per reference and 1 MiB across all references. Larger references are compressed before either endpoint; the total budget is divided among multiple references. The two Base64 copies in Chat then occupy about 2.67 MiB plus JSON/prompt overhead, rather than about 5.3 MiB for a single 2 MiB source. This cannot guarantee acceptance by a channel with a lower undocumented limit.
- At most 9 MiB for the encoded request body, including the two Chat Base64 representations and prompt. Oversized bodies stop before networking.

The exact selected model's explicit `reference_image_limits` or `input_image_limits`, at the entry root or in `capabilities`, can lower these budgets through integer `max_bytes`, `max_total_bytes`, `max_width`, `max_height`, and `max_images`. Output resolution metadata is never interpreted as a reference limit. When no input limits are published, `reference_limit_source` reports `client-budget`; an undocumented channel may still reject the request with 413.

Pillow validates references and produces separate PNG/JPEG upload copies when needed, preserves alpha for transparent images, applies EXIF orientation, and reduces dimensions/encoding size within the budget. Animated references require an explicit exported frame. If fitting the budget would require excessive detail loss, the client stops and asks for fewer/smaller references. Original files, saved results and confirmed output resolution are preserved. Without Pillow, only recognized small supported images with readable dimensions can pass; references needing normalization stop before submission.

`start` stores durable snapshots under the job directory and records the cleaned prompt, so a worker can continue even when an uploaded file is later moved. `reference_images` reports original/prepared dimensions and bytes; `prompt_info` reports character/byte counts. `request_attempts` records each POST's body size, UTC start time, elapsed seconds and HTTP status, without storing its body or authorization header. Response diagnostics retain field names/types/counts, not image data or message text.

## Sizes

Before every new generation or edit, `models` must query `GET /v1/models` and the user must choose from the returned catalog. `run` and `start` require the one-use selection token returned by that query. Tokens are bound to the Codex task and API host and cannot be reused for another image request. If input preflight fails before any job starts and while the token is still unused and current, correct the same request with that token and chosen model; do not refresh the catalog or ask for the same choice again. Check `status` for an existing job, and never reset a used token.

The conservative size set is:

- `1024x1024`
- `1536x1024`
- `1024x1536`

When the user requests higher resolution, fetch the model catalog first. Present the server-published sizes and label `2560x1440` or `3840x2160` as attempts that the channel may reject when they are not published. Confirmation is required before passing a non-conservative size to `start`.

Before sending `2560x1440` or `3840x2160`, tell the user that high-resolution generation can take several minutes and ask them to wait patiently. Start these requests with the detached job command and poll its status. The worker owns the single API request and writes the result after the Codex turn ends; never submit the same selection again.

Do not claim a lower-resolution result is 2K or 4K. Do not retry a confirmed high-resolution request at a smaller size. For Chat results, requested pixel dimensions are advisory and are not guaranteed by the endpoint.

## Response formats

Saving an image and displaying it are separate stages. Codex's image viewer may fail to load a large 4K PNG with an `invalid base64`/`Invalid padding` error even when the file is intact. The client generates a small JPEG display copy with optional Pillow and always preserves the original for delivery and later edits. Preview failures do not trigger endpoint fallback or another generation. `last` recovers saved results without API calls, including results saved by older client versions.

For high-resolution requests, `start` creates a detached worker and returns a job ID. `status` reads the worker's atomic result file and never starts another request. If the Codex task is interrupted while Keylink is processing, resume with the same workspace and thread ID, poll `status`, and then display the saved result. A request started by an older client cannot be reconstructed from the Keylink billing log; use `last` only when the original client actually saved a local result.

`latest-request.json` tracks the current request ID and running/succeeded/failed/refused state. `last` refuses to deliver an older success as the current request's result; legacy tasks also check consumed-selection and job start timestamps. API rejection does not overwrite `last.json`. Explicitly requested historical recovery uses `last --allow-previous` and is labeled `historical: true`, never a new generation. Input preparation and `--use-last` may still read the last successful original for a new edit.

The default `status` rejects a background job superseded by a later request; explicitly querying an old job marks its output historical. Timestamps are compared as instants across UTC/offset formats. State updates use an OS lock released on process exit, and an earlier request finishing late cannot replace the current request's saved reference. If a saved image's history update fails, return the image with `state_recorded: false`; never use endpoint fallback to repeat a generation because of that local state error.

Structured `refusal`, `content_filter`, Gemini `blockReason`/`finishReason` safety blocks, and content-policy error codes are failures even if the response also contains an image field. Inspect the full HTTP error response before truncating diagnostic text; JSON embedded in Chat content and nested response containers must pass the same refusal/API-error checks before extracting images. An explicit provider refusal stops automatic endpoint fallback and model-switch suggestions. Echoed messages with user/system/developer/tool roles are not output images. No prompt is automatically rewritten to bypass a refusal.

Pillow verifies and decodes returned images before saving them as successful results; truncated or corrupt candidates follow the download/decoding failure policy. Without Pillow, validation is limited to image signatures, readable dimensions where supported, and basic completeness checks. Installing Pillow is recommended for complete decoding validation as well as reference normalization.

Model selection writes and consumption share the process-lifetime state lock, so interrupted processes cannot leave a permanent selection lock and concurrent requests cannot consume one selection twice. Background metadata/launch failures release the unsubmitted selection claim. Completed worker results are published before scheduled-task cleanup; a cleanup failure is logged without losing the image or submitting another generation.

The client accepts image URLs, data URLs, Base64 fields, camelCase URL fields, nested Chat text/images, generated image/artifact arrays, Gemini `inlineData`, and image `source.data`. Markdown/HTML image links and bare image URLs are recognized. JSON error envelopes (including `status_code=400/413` inside HTTP 200) remain API errors rather than missing-image errors. Saved files must have recognizable image bytes; an `image/*` header alone is not enough. An image candidate that failed to download/decode is diagnosed separately from a response with no candidate. URL downloads inherit authentication only when the image URL uses the same host as the configured API root, preventing credentials from being sent to unrelated hosts.

If a configured system HTTP proxy fails to connect to a returned image URL, retry downloading that same image directly once before treating the endpoint as failed. HTTP error responses are not retried this way. This download recovery does not issue another generation request or alter model, prompt, size, or endpoint selection.
