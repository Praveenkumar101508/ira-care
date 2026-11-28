# IRA CARE: architecture diagrams

Written 25 November 2026 for the Innovator Founder endorsement and the investor demonstration.
Three diagrams, in Mermaid (GitHub renders them in place). Each one is drawn from the code and
names the files it comes from, so a reader can check it. Where a box is simulated, unbuilt or a
stand-in, the diagram says so.

- **Solid boxes** are built and tested in this repository.
- **Boxes marked "simulated", "unbuilt" or "log-only"** are exactly that today.
- Nothing in these diagrams has run on a physical watch or phone, or with real people.

## 1. System context

Who and what IRA CARE talks to. The care server is one deployment per operator; inside it,
every organisation (tenant) and workspace is separated by PostgreSQL row-level security.

```mermaid
flowchart LR
    subgraph Wearer["The person being looked after"]
        W["IRA watch app<br/>Wear OS and AOSP flavours<br/>(watch/)"]
        BW["Browser watch<br/>demonstration only<br/>(dashboard /watch)"]
        IW["IRA watch hardware<br/>unbuilt (docs/hardware/)"]
    end

    subgraph Circle["Family and carers"]
        FA["Family phone app<br/>Expo / React Native<br/>(mobile/)"]
    end

    subgraph Org["Care organisation"]
        DB["Carer dashboard<br/>Next.js<br/>(dashboard/)"]
    end

    subgraph Server["IRA CARE deployment (deploy/docker-compose.yml)"]
        CADDY["Caddy<br/>HTTPS"]
        API["Care API<br/>(supracloud-jarvis/ira/care/routes.py)"]
        WK["Care worker<br/>check-ins, monitoring, escalation, delivery<br/>(care/worker.py)"]
        PG[("PostgreSQL 16<br/>forced row-level security<br/>per tenant and workspace")]
        RD[("Redis")]
        OL["Ollama<br/>open models, optional<br/>(off, CPU or GPU profile)"]
        BK["Backup service<br/>encrypted, off-site copy<br/>(deploy/backup.sh, deploy/offsite.sh)"]
    end

    subgraph External["Outside services (none contracted)"]
        PUSH["Expo push<br/>log-only by default"]
        SMS["Text and call provider<br/>adapter off until credentials<br/>never an emergency number"]
        MAIL["Mail server<br/>adapter off until configured"]
        MC["Monitoring centre<br/>webhook adapter, no contract"]
        OTA["OS update server side<br/>(care/ota.py); IRA CARE OS image unbuilt"]
    end

    W -- "readings, Help, fall, answers<br/>device token, idempotency key" --> CADDY
    BW -- "same device API" --> CADDY
    IW -.->|"future: same app, sensor HAL contract"| W
    FA -- "signed-in account" --> CADDY
    DB -- "signed-in account" --> CADDY
    CADDY --> API
    API --> PG
    API --> RD
    WK --> PG
    API -- "talk, check-in wording,<br/>answer reading (bounded by rules)" --> OL
    WK --> OL
    WK --> PUSH
    WK --> SMS
    WK --> MAIL
    WK --> MC
    BK --> PG
    W -.->|"update check"| OTA
```

Sources: `deploy/docker-compose.yml`, `deploy/compose.gpu.yml`, `deploy/compose.nomodel.yml`,
`supracloud-jarvis/ira/care/providers.py`, `supracloud-jarvis/ira/care/push.py`,
`docs/os/ARCHITECTURE.md`, `docs/evidence/staging/CONTAINERS.md`.

## 2. Data flow of a Help request

What happens between the wearer pressing Help (or saying "help") and the watch showing that
someone is on the way. Steps the code guarantees are in the notes; every provider in a
demonstration is log-only, so nothing leaves the machine and nothing dials anyone.

```mermaid
sequenceDiagram
    autonumber
    actor P as Wearer
    participant W as Watch app (outbox)
    participant API as Care API
    participant DB as PostgreSQL (RLS)
    participant WK as Care worker
    participant PR as Providers (push, text, call, email)
    actor F as Family or carer (phone app)

    P->>W: presses Help, or says "help"
    W->>W: stores the request in its outbox ("Sending…")
    W->>API: POST /care/devices/me/help with X-Idempotency-Key
    API->>DB: device token checked, alert opened (severity sos) under the workspace's row-level security
    API->>DB: escalation tier 1 written as queued attempts in the same transaction
    API-->>W: 201, SOS state "received" (a repeat of the same key gets the stored answer)
    W-->>P: "Help request received", never "help is coming" until the server says so
    opt no position within 2 s of the press
        W->>API: POST /care/devices/me/help/location, once a fix comes (up to 60 s), through the outbox
        API->>DB: the position stored on the open SOS that has none (an existing position stands)
    end
    WK->>DB: picks up queued attempts after commit
    WK->>PR: tier 1: push, text, call to the circle (refuses emergency numbers)
    PR-->>WK: outcome recorded per attempt (log-only in demonstrations)
    Note over WK,DB: no acknowledgement within the wait: tier 2 (care organisation), then tier 3 (monitoring centre if contracted, otherwise repeat and show the family "call 999 now")
    F->>API: "I'm on my way"
    API->>DB: acknowledgement recorded, next_action_at cleared, so the chain stops
    API-->>W: SOS stream: "Sravani is on the way"
    W-->>P: shows and speaks who is coming
```

Sources: `supracloud-jarvis/ira/care/routes.py` (the device routes for Help and the SOS stream),
`supracloud-jarvis/ira/care/sos.py`, `supracloud-jarvis/ira/care/escalation.py`,
`supracloud-jarvis/ira/care/delivery.py`, `supracloud-jarvis/ira/care/providers.py`.
Tests: `supracloud-jarvis/ira/tests/tenancy/test_care_sos_db.py`,
`supracloud-jarvis/ira/tests/tenancy/test_care_escalation_db.py`,
`supracloud-jarvis/ira/tests/tenancy/test_care_delivery_db.py`,
`supracloud-jarvis/ira/tests/tenancy/test_care_journey_db.py`.

Measured through the browser watch against the real server (not a device):
reading upload to "are you OK?" question in 0.028 s (`docs/evidence/sample_to_alert.json`). Through
the containerised stack, a Help request under 50 simulated watches took 435 ms at the median and
807 ms at the 95th percentile (`docs/evidence/staging/containers.json`, a single shared machine).

## 3. Intelligence components and their safety boundaries

Which parts are learned models and which are fixed rules, and where the rules stand between a
model and anything that matters to safety. The rule: **no model can raise, suppress or close an
alert on its own, and no model can record a dose as taken.** The open conversation models only
word questions and replies; the deterministic rules decide.

```mermaid
flowchart TB
    subgraph Watch["On the watch (watch/)"]
        ACC["Accelerometer window"] --> RULE["Rule fall detector<br/>impact then stillness<br/>(the backstop)"]
        ACC --> FV3["IRA fall model v3<br/>our own, 6,705 parameters<br/>BITS-2 + UCI HAR"]
        RULE --> CD["30 s 'are you OK?' countdown"]
        FV3 -- "recover mode: can raise a<br/>suspected fall the rule missed" --> CD
        ACT["IRA activity model<br/>our own, WISDM"] --> SUM["Hourly summaries<br/>only with consent"]
        HB["Help button / spoken 'help'"]
    end

    subgraph Server["On the care server (supracloud-jarvis/ira/care/)"]
        CD -- "no answer" --> SOS["SOS opened"]
        HB --> SOS
        HR["Heart-rate readings"] --> RNG["Carer-set range rule<br/>two out of range over 2 min"]
        RNG --> ASK["Ask first: 'are you OK?'"]
        ASK -- "no answer" --> SOS
        SOS --> ESC["Escalation tiers 1 to 3<br/>(rules, durable outbox)"]

        Q["Check-in question"] --> GATE1["One-question rule<br/>name and wording checks"]
        GEM["Gemma 4 via Ollama<br/>third-party open model<br/>(gemma4:e4b, e2b; 12b on GPU)"] -- "drafts wording" --> GATE1
        GATE1 -- "refused or late" --> TPL["Fixed template question"]
        ANS["Person's answer"] --> KW["Keyword and red-flag rules<br/>run on every answer"]
        ANS --> GEM2["Gemma 4 reads the answer as JSON"]
        GEM2 --> MERGE["Stricter reading wins<br/>doubt stays 'unknown'; no dose from doubt"]
        KW --> MERGE
        MERGE --> FOLLOW["Follow-up, notice or ask-first"]
        TALK["Talk turn"] --> GEM
        GEM -- "reply" --> GUARD["No diagnosis or advice words;<br/>fixed line on failure or timeout"]
        MEM["Memory: what the person agreed<br/>IRA may remember"] -- "authorised items only" --> GEM
        CLS["Answer-classifier heads<br/>our own, on MiniLM (third-party)<br/>off by default"] -.-> MERGE
        HSEL["Heart selector<br/>our own, PPG-DaLiA<br/>no alert path; needs raw PPG"] -.-> TREND["Wellbeing trend only"]
        STT["faster-whisper small + its VAD filter<br/>third-party, server speech to text"] --> TALK
    end

    subgraph Reports["What people see"]
        ESC --> CIRCLE["Family and carers"]
        FOLLOW --> REC["Carer record: summaries,<br/>never the person's words"]
    end
```

What each boundary is held by:

| Boundary | Held by | Evidence |
| --- | --- | --- |
| Red-flag words always reach the rules, whatever the model says | keyword rules on every answer (D-056) | `supracloud-jarvis/ira/tests/care/test_care_safety_answers.py`, `docs/MODEL_CHOICE.md` (red-flag recall 1.00 for every model, because the rules run regardless) |
| Doubt never becomes "taken" | the stricter reading wins; `medication_taken: unknown` | `supracloud-jarvis/ira/tests/tenancy/test_care_safety_answers_db.py`, `docs/evidence/demo/REAL_MODEL_RUN.md` |
| One question at a time; fixed template on refusal or timeout | the one-question rule and time budgets in `care/conversation.py` | `docs/evidence/demo/REAL_MODEL_RUN.md` (the guard refused the model's wording in 2 of 8 runs) |
| No diagnosis or advice words in replies | reply guard and fallback line | `supracloud-jarvis/ira/tests/care/test_care_conversation_rules.py` |
| Memory and transcripts follow the person's consent | an authorised preference reaches the prompt; transcripts are private without recorded consent; the person or their representative can correct or clear what IRA remembers | `supracloud-jarvis/ira/tests/tenancy/test_care_journey_db.py` (step 6), `supracloud-jarvis/ira/tests/tenancy/test_care_conversation_db.py`, `supracloud-jarvis/ira/tests/tenancy/test_care_memory_db.py`, `supracloud-jarvis/ira/tests/care/test_care_personal_rules.py` |
| Fall model cannot remove a rule fall | recover mode: the rule detector still raises every fall it raises | `docs/ai/fall/MODEL_CARD_V3.md` |
| Activity and heart selector raise nothing | no alert path reads them | `supracloud-jarvis/ira/tests/care/test_activity_no_alert_path.py`, `docs/ai/heart/MODEL_CARD.md` |
| The system never dials emergency services | refused in the provider layer before any provider is reached | `supracloud-jarvis/ira/care/providers.py` |
| The product works with no model at all | `IRA_CARE_MODEL=off`: set questions and rules only | `deploy/compose.nomodel.yml`, `docs/MODEL_CHOICE.md` (rules-only row) |

Not shown because it does not exist yet: the IRA watch's own sensor hardware and sensor HAL
(`docs/hardware/SENSOR_HAL.md` is the contract), and the IRA CARE OS image (`docs/os/ARCHITECTURE.md`).
