# Multimodal surfaces

Five surfaces behind `/api/v1/assist`, each local-first, consent-gated and
resource-capped — except browsing, which is the one that genuinely leaves the
box and says so.

| Endpoint                     | What it does                                              | Backend                              |
| ---------------------------- | --------------------------------------------------------- | ------------------------------------ |
| `POST /assist/vision`        | answers a question about an uploaded image                 | local VL model (`utils.llm.vision_complete`) |
| `POST /assist/video`         | frame samples + transcribes a short video, then summarises | ffmpeg frames + local faster-whisper |
| `POST /assist/image`         | generates an image                                          | local SD WebUI; external only on explicit approval |
| `POST /assist/image/edit`    | edits an image from a text instruction                      | same local-first gate as generation  |
| `POST /assist/browse`        | searches the web, or reads one page, and summarises it       | the existing SSRF-guarded Chromium   |
| `GET  /assist/surfaces`      | which surfaces are usable right now, and their caps          | —                                    |

Everything reuses what already existed: the vision client in `utils/llm.py`, the
Whisper STT in `voice/stt.py`, `_extract_frames_ffmpeg` from
`api/routes/video_gen.py`, and the image backends in `api/routes/image_gen.py`.
No second STT and no second vision client were added.

## The local-first rule

No surface calls an external provider while `IRA_ALLOW_EXTERNAL_API` is false.
Image generation in particular resolves in this order:

1. a local generator (`IMAGE_GEN_URL`) → use it;
2. no local generator and no external one → **"Local image generation is not
   installed…"**, and nothing is called;
3. external configured but the master switch is off → **"…external APIs are off.
   Nothing was sent off the device."**;
4. external configured, switch on, but the request did not set
   `approve_external` → refused with an explanation.

Only step 4 with explicit approval reaches an external provider.

## Caps

`ASSIST_MAX_IMAGE_MB` (15), `ASSIST_MAX_VIDEO_MB` (100),
`ASSIST_MAX_VIDEO_FRAMES` (12). Over-cap uploads are refused with the cap named,
never truncated silently.

## Fail-soft

Missing tooling is reported, not raised: no VL model → "No local vision model is
configured…"; no ffmpeg **and** no Whisper → a message naming both. A missing
model never produces a 500.

## The older endpoints are gated too (closed)

`POST /api/v1/image/generate`, `/image/edit`, `/video/generate`,
`/audio/generate`, `/audio/tts` and `/audio/transcribe` used to reach Replicate
whenever `REPLICATE_API_TOKEN` was set, without consulting
`IRA_ALLOW_EXTERNAL_API`. That is fixed: `utils/external_gate.py` enforces the
master switch **inside the helpers that make the outbound call**, so every
caller — these routes, the chat feature dispatcher, and the `/assist/*` surfaces
— is covered by construction and a new caller cannot forget.

A configured token is no longer treated as consent. With the switch off, those
endpoints return **403** with an explanation naming the switch, and a refused
attempt is recorded as a consent audit event.

`/audio/transcribe` additionally became **local-first**: it now runs the same
faster-whisper the voice pipeline uses, so transcription works offline and the
audio never leaves the box. Replicate is only a fallback, and only when the
switch permits it. The response tags which provider produced the transcript.

## Browsing

`POST /assist/browse` reuses the browser stack that already exists — shared
headless Chromium, `utils.net_safety.check_url` SSRF guard, cookie-banner
handling, page-text extraction. No second browser was built.

- Needs **both** switches: `BROWSER_AGENT_ENABLED` (the capability) and
  `IRA_ALLOW_EXTERNAL_API` (the egress ceiling). Either one off means nothing is
  fetched, and the refusal names both.
- Capped by `BROWSER_TASK_TIMEOUT_S` on top of the browser's own timeouts.
- Every result carries `local_only: false` and names the page it read. Browsing
  is egress; the payload never implies otherwise.
- A blocked URL, missing Playwright, a timeout or an empty page all fail soft
  with a specific message.

### Why the master switch governs browsing

Fetching a public page sends the query text and this machine's IP off the
device. That is egress, so `IRA_ALLOW_EXTERNAL_API` — the switch that means
"nothing leaves the box" — governs it, exactly as it governs image, video and
audio generation.

Enforcement is at `_fetch_page_text`, the single navigation chokepoint every
browsing path converges on (the browser agent, flight search, `/assist/browse`),
so no caller can bypass it. `browser_lookup` and `search_flights` also check
upfront, so a blocked call reports the real reason rather than the generic
"couldn't reach the search engines" fallback, and the refusal is recorded as a
`blocked` consent audit event.

Self-hosted services are unaffected: the SSRF guard already refuses loopback and
private addresses, so a local SearXNG or Crawl4AI never travels this path.

**Note the default:** `IRA_ALLOW_EXTERNAL_API` is `false` out of the box while
`BROWSER_AGENT_ENABLED` is `true`, so browsing is now off until you flip the
master switch. That is the intended posture — local-first means the box stays
quiet unless you say otherwise.

## Demo notes

Vision understanding is the most demo-safe surface here. Video, image generation
and browsing depend on tooling (ffmpeg, faster-whisper, a local SD server,
Chromium) and VRAM headroom that must be verified on the exact machine first.
A browser agent failing live is among the worst things a client can watch.
