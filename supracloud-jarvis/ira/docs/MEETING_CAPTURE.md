# Meeting capture — Phase 1 (capture · store · search)

Record a call, get a speaker-labelled transcript, and ask IRA about it
afterwards. **This phase is capture + store + after-the-fact search.** The live
in-meeting overlay and the anticipation loop are Phase 2.

## Consent is not optional

`meetings/consent.py` owns the single recording state. `POST /meetings/start`
refuses with **HTTP 428** unless the request carries
`consent_acknowledged: true`, and the notice it returns says plainly that
recording other people may require their consent depending on jurisdiction.
`GET /meetings/status` is the one source of truth for the recording indicator —
no surface computes its own.

## Raw audio never touches disk

`meetings/capture.py` reads a window of PCM, transcribes it in memory, extends
the transcript, and drops the buffer before the next read. `pcm_to_wav` builds
the WAV container in a `BytesIO`. What persists is text. A test asserts that no
audio file is opened for writing during a capture session.

## Capture source

Windows WASAPI loopback (`pyaudiowpatch`) records what the meeting app is
*playing*, so Teams / Meet / Zoom all work without a per-platform bot. Off
Windows, `resolve_source()` refuses with an explanation rather than pretending —
route meeting audio to a virtual input and supply a custom `AudioSource` (it is
a small Protocol: `open` / `read_chunk` / `close`).

## Speaker separation

`meetings/diarise.py` uses pyannote when installed. When it is not, the
transcript is stored unlabelled as `speaker?` **plus a notice** — never a crash,
and never a fabricated speaker name. Alignment within a window is proportional
to each speaker's share of speech time; it answers "who said roughly what" and
is honest about being coarse (word-level alignment would need a forced aligner).

## Storage and search

Transcripts are chunked and embedded into the same pgvector table under
`user_id = "meeting"`, isolated from owner memory and from the self-knowledge
corpus. `POST /meetings/ask` retrieves within that namespace only and answers
**strictly from the transcript**: when nothing clears the relevance bar the model
is not called and the reply is `That isn't in the meeting transcript.` Answers
carry the same confidence object as the rest of IRA.

## API

```
GET  /api/v1/meetings/status   recording state + consent notice + diarisation availability
POST /api/v1/meetings/start    {"consent_acknowledged": true, "title": "Client call"}
POST /api/v1/meetings/stop     ends capture, stores the transcript text
GET  /api/v1/meetings/live     rolling labelled turns while recording
GET  /api/v1/meetings          meetings held in the namespace
POST /api/v1/meetings/ask      {"question": "...", "meeting_id": null}
```

All owner-only.

## Remaining for Phase 2

- The live in-meeting overlay (own screen only, talking points, confidence badges)
- The pre-fetch / anticipation loop
- Sub-second response — Phase 1 targets "the transcript keeps up", not real-time
  assistance

Honest note: Phase 1 is the part worth using daily right away. The live overlay
is the part to run in your own real meetings for weeks before it goes anywhere
near a client — a real-time audio+overlay system debuting live is the highest
on-stage failure risk in this whole build.

---

# Meeting overlay — Phase 2 (live in-meeting assistance)

An always-on-top window on **the owner's own screen** showing the live
transcript and 2-3 short talking points drawn from the owner's own records.

## What it is, and what it is not

It is an aid for the owner. It is **not** a covert coaching tool:

- `meetings/live_assist.py` has **no audio-output path at all** — a test parses
  its imports and fails if anything audio/TTS-related appears. It cannot speak
  into, inject into, or touch the call.
- The Electron window sets `setContentProtection(true)`, so it is excluded from
  screen capture: sharing your screen never shows the overlay to anyone else.
- Suggestions are talking **points**, capped at 3 lines of ≤22 words. Not a
  script. A read-out answer collapses at the first follow-up, so the design
  refuses to produce one.
- Every point carries the backend's confidence. Ungrounded points are labelled
  **UNVERIFIED**, and the badge is known *before* the first token streams — the
  owner should never read a point before knowing whether to trust it.

## Flow

```
rolling transcript → detect_question() (cheap heuristic, no model call)
   → gather_context(): self-knowledge + this meeting, both namespace-scoped
   → confidence from retrieval scores
   → stream ≤3 short points
```

`POST /api/v1/meetings/live/suggest` streams SSE: one `context` event carrying
the confidence, then `token` events, then a `done` event with the parsed points.
`stream: false` returns the finished draft instead.

## Running the overlay

```
cd supracloud-jarvis/overlay
npm install
IRA_API_BASE=http://127.0.0.1:8000 IRA_API_TOKEN=<jwt> npm start
```

It is a plain Electron app with no build step and is not part of the CI build.

## The limits no tuning removes

- **Off-corpus questions get nothing useful.** Retrieval only finds what is in
  your records; for anything else the overlay correctly shows an unverified or
  empty result. That is the honest behaviour, not a bug to tune away.
- **False triggers.** The question heuristic over-triggers by design and will
  draft for things that were not really questions. Getting that rate low enough
  to stop being distracting is weeks of real use, not a demo-week task.
- **Split attention.** Even glanceable bullets pull focus from the conversation.
  This is why it surfaces *your own knowledge* rather than answers to relay —
  you can only use it well for things you already know.

Use it in your own real meetings until the false-trigger rate stops annoying you
and you trust the badges. Only then consider it demo material.

---

# Anticipation loop (pre-fetch)

**OFF by default** (`ANTICIPATION_ENABLED=false`). A personal-productivity
refinement, not a demo feature and not a differentiator to pitch.

While the other person speaks, `meetings/anticipate.py` tracks the topic from
the rolling transcript (frequency-ranked content words — no model call),
pre-retrieves the owner's relevant material, and speculatively drafts 1-2
candidate talking points for the most likely next question. When a question
lands and a cached draft *closely* matches it, the points are already there.

```
transcript window → observe() → topic shift? → schedule_prefetch()  (background)
question detected → match()  → hit: surface instantly
                             → miss: fall back to live drafting
```

## The knobs

| setting                        | default | what it does                                    |
| ------------------------------ | ------- | ----------------------------------------------- |
| `ANTICIPATION_ENABLED`         | `false` | master switch                                    |
| `ANTICIPATION_MATCH_THRESHOLD` | `0.85`  | cosine floor for a cache hit                     |
| `ANTICIPATION_MAX_DRAFTS`      | `2`     | speculative drafts in flight                     |

`GET /api/v1/meetings/live/anticipation` reports the live topic, cache size and
hit/miss counts, so the threshold can be tuned from real numbers.

## Guarantees

- A near-miss returns nothing and the caller drafts live. A mismatched
  pre-draft is never shown as if it answered the question.
- A hit is one-shot — a draft is removed once surfaced, so it can't reappear
  against a later, different question.
- A topic shift (or a 4-minute TTL) expires stale drafts.
- Pre-drafting never upgrades confidence: an ungrounded pre-draft stays
  UNVERIFIED.
- Speculative work is fire-and-forget and swallows its own failures; the live
  transcript path never waits on it.

## The ceiling no tuning removes

Pre-fetch only wins on questions predictable from the topic. In a real
one-to-one, many are not — and for those this correctly gives you nothing. The
match threshold is the whole game: loose surfaces drafts that don't fit, strict
rarely fires. Balancing that is months of your own real-meeting use.

Net: it makes you faster on the knowable half. It does not read minds. For the
unknowable half, "let me think on that" is still the professional answer.
