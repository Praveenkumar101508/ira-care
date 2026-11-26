# IRA CARE: competitors and comparison

Compiled 26 November 2026 from desk research. It describes products that people in the UK
already use for the same jobs IRA CARE is designed for, and compares them with what IRA CARE
**actually** does today. It is not a market study: no buyer has been asked how they choose.

## How to read this document

- **Prices.** None of the vendors' own websites could be opened from the build environment on
  26 November 2026 (see the list at the end). Every price below was seen only in a search-engine
  summary of the vendor's page on that date. Each is therefore marked **unverified**, with the
  page it came from. The vendor's page date was not visible. Re-read every price at source,
  including VAT and set-up fees, before using it in a projection.
- **Features.** Feature descriptions come from the same summaries (vendor pages, support pages
  and council pages) and are also unverified unless stated. A vendor's description of its own
  product is a claim, not evidence of performance.
- **IRA CARE's column** is taken from the repository:
  `docs/evidence/TECHNICAL_ENDORSEMENT_REPORT.md`, `docs/PRODUCT_SPEC.md`,
  `docs/progress/GO_NO_GO.md`, `docs/compliance/CLAIMS_REGISTER.md` and
  `docs/ai/fall/MODEL_CARD_V3.md`. Three labels are used:
  - **Implemented:** the code exists and is tested in CI against a real database; it has **not**
    run on a physical watch or phone, or with a real older person.
  - **Simulated:** shown only with the browser watch or the watch app's simulated sensors, or with
    log-only message providers.
  - **Not built:** does not exist, or exists only as a design document or an interface with
    nothing behind it.

## 1. The products

### 1.1 Monitored personal alarms and telecare services

These are the established category: a pendant or wrist button (often with an optional fall
detector) connected to a 24-hour alarm receiving centre (ARC), whose operators speak to the
person and call a named responder or the emergency services. Councils run or commission many
such services; private providers sell directly to families.

| Provider | What it does (as described) | Published price seen | Source | Status |
| --- | --- | --- | --- | --- |
| **Careline365** | Pendant, GPS pendant and alarm watch plans with 24/7 monitoring; fall detection on some plans; newer "EverNear" plans | Device £99 one-off plus monitoring from £27.50 a month (EverNear); older plan pages show from £9.99 to £22.67 a month and fall detection as a £2 to £5 a month add-on; prices differ between the vendor's own pages | <https://www.careline.co.uk/shop-page>; <https://careline.co.uk/pages/careline-plans> | Unverified. Which? reported in 2025 that Careline365 was among providers still selling analogue alarms (unverified) |
| **Taking Care** (also supplies Age UK's branded alarms) | Digital home alarm, Digital Fall Alarm, "Taking Care Anywhere" GPS pendant with fall detection, 24/7 monitoring | Digital Fall Alarm: set-up £69.99, £28.49 a month (ex VAT); Taking Care Anywhere: £31.49 a month (ex VAT). The vendor says the GPS pendant "may not detect every fall as it is designed to detect hard, unobstructed falls" | <https://www.taking.care/products/taking-care-anywhere-alarm>; <https://campaign.taking.care/Equipsme> | Unverified |
| **Age UK personal alarms** (supplied by Taking Care) | Digital Personal Alarm, Digital Fall Alarm, Personal Alarm Watch | Set-up £69.99 (£83.99 inc. VAT); the Personal Alarm Watch: set-up £99.99, £26.49 a month (ex VAT). Other Age UK figures seen (£4 and £5 a month) look like add-on prices and are not reliable | <https://www.ageuk.org.uk/products/independent-living/personal-alarms-gg/>; <https://personalalarms.ageuk.org.uk/pages/personal-alarm-watch> | Unverified |
| **Tunstall** | Equipment maker and service provider: Lifeline Smart Hub home unit linking up to 50 sensors, iVi pendant, Vibby and Lifeline fall detectors, a device-management platform; widely used by council services | Sold mainly to councils and housing providers; no consumer price seen | <https://uk.tunstall.com/siteassets/uk/product-data--solution-sheets/lifeline-smart-hub-datasheet.pdf>; <https://www.tunstall.co.uk/products/wearables/lifeline-fall-detector/> | Unverified |
| **Chubb Community Care** | Telecare monitoring 24/7; Care System for sheltered housing and care homes; IP nurse call ("Care Call"); "Cloud Care Control" cloud ARC platform | No price seen | <https://chubbfs.com/uk-en/industries/telecare/our-solutions/> | Unverified |
| **Appello** | Digital telecare and monitoring, mainly for housing providers; audio and video communication | No price seen; product detail not found | Slough council TEC page; CB Insights profile | Unverified; thin evidence |
| **Council lifeline services** (example) | Council-run monitored alarms | Harrow: £295 a year (Standard) or £495 a year (Premium), ex VAT | <https://www.harrow.gov.uk/communityalarm> | Unverified |
| **Telecare24** (example private provider) | Digital pendant alarm | From £19.00 a month or £180 a year, ex VAT | <https://www.telecare24.co.uk/products/> | Unverified |
| Which? guide (context) | — | Pendant alarms "around £19 to £27 a month", plus set-up fees for fall-detector monitoring of £30 to £50 | <https://www.which.co.uk/later-life-care/home-care/technology-to-keep-you-safe/personal-alarms-azbn15v6qmgq> | Unverified |

### 1.2 Consumer smartwatches with fall detection

| Product | What it does (as described by the maker) | Price | Source | Status |
| --- | --- | --- | --- | --- |
| **Apple Watch** (Fall Detection) | After a hard fall: taps the wrist, sounds an alarm, shows an alert; if the wearer has been immobile for about a minute, a countdown runs and the watch calls the emergency services, then messages emergency contacts with the location. Apple says it "cannot detect all falls"; needs a cellular, Wi-Fi Calling or satellite connection; only for people 18 or over; off if wrist detection is off | Watch price not collected (consumer device; varies by model) | <https://support.apple.com/en-ie/guide/watch/apd34c409704>; <https://support.apple.com/en-lamr/HT208944> | Unverified (Apple's site could not be opened) |
| **Samsung Galaxy Watch** (Hard fall detection) | Off by default; after a detected fall, alerts for 60 seconds, then sends an SOS to up to four emergency contacts; on some models can call the emergency services. Samsung says it cannot detect all falls and is not a medical device; needs a network and a paired phone | Not collected | <https://www.samsung.com/uk/support/mobile-devices/how-to-set-up-fall-detection-on-the-galaxy-watch/> | Unverified |

These matter twice: they compete for the same wrist, and IRA CARE's watch app is built for
Wear OS (Galaxy Watch 4 and later), so a Galaxy Watch is also IRA CARE's most likely first
device (`docs/PRODUCT_SPEC.md`, section 6). IRA CARE on Apple Watch is a plan only
(`docs/APPLE_WATCH_PLAN.md`).

### 1.3 Specialist wearables

| Product | What it does (as described) | Published price seen | Source | Status |
| --- | --- | --- | --- | --- |
| **MindMe** (GPS alarm) | GPS locator alarm with 24-hour monitoring; accuracy "about 10 metres"; fall detection not confirmed | £85 set-up and £17.70 a month on a Taking Care reseller page that said the price applied to orders before June 2023 | <https://taking.care/products/mindme-gps-alarm> | Unverified and probably out of date |
| **Buddi Connect** | Wristband with automatic fall detection and panic buttons, an app for family; optional 24/7 support | £99 plus £1.99 a week for the app service; 24/7 support an extra £2 a week | <https://buddi.co.uk/products/buddi-connect> | Unverified |
| **Doro Watch** | Activity tracker (sleep, heart rate, blood oxygen, workouts) with Doro's Response button that alerts a chosen relative; needs a Doro 8100 phone within about 10 m. No fall detection found | £120 at UK launch (2 September 2022) | <https://doro.com/en-gb/news/Doro-launches-new-tech> | Unverified; Response Premium by Doro adds an ARC fallback (price not seen) |
| **Anchor** | Not found. No current Anchor (Anchor Hanover) wearable or telecare product page was returned | — | — | Not researched further |

### 1.4 Care-management platforms

These do not watch the older person; they run the care provider's work: rostering, visit
records, care plans, medication administration records (eMAR) and audit. Most providers already
use one (83.6 % of provider locations have a digital social care record, `MARKET_EVIDENCE.md`
T7).

| Product | What it does (as described) | Published price seen | Source | Status |
| --- | --- | --- | --- | --- |
| **Birdie** | Homecare platform: eMAR with PRN protocols, clinical observations, rostering with drag-and-drop scheduling and travel time, a carer app, care notes, body maps, audit | Not published; reported to be quoted on the hours scheduled | <https://birdie.care/pricing> | Unverified |
| **Access Care Planning** (The Access Group) | Digital care plans, eMAR, GP Connect, records on web and mobile including offline; rostering is a separate product | No reliable figure (a buyer's guide row mixed it with another product's price) | Birdie buyer's guide (competitor material) | Unverified |
| **Nourish** | Cloud care-management software: plan, record and coordinate care; separate rostering ("Empower") and e-learning | Not seen | <https://nourishcare.com/product> | Unverified |

### 1.5 Companions and voice assistants

| Product | What it does (as described) | Published price seen | Source | Status |
| --- | --- | --- | --- | --- |
| **ElliQ** (Intuition Robotics) | Tabletop companion device that starts conversations, gives reminders and sends updates to caregivers; leased with a subscription; United States market | US$49.99 a month (annual plan) or US$59.99 a month (monthly), plus a one-time enrolment fee reported as US$249.99 | <https://elliq.com/products/annual-subscription> | Unverified; no UK availability found |
| **Amazon Alexa Together** | Was a US caregiver subscription (US$19.99 a month) with an urgent-response line and activity alerts. Amazon's notice updated 21 May 2025 says it is no longer available and points to "Alexa Emergency Assist" | — | <https://www.aboutamazon.com/news/devices/alexa-together-launches-to-help-customers-remotely-care-for-loved-ones> | Unverified; Alexa-based care schemes run by UK councils were not researched |

### 1.6 Passive home sensors

| Product | What it does (as described) | Published price seen | Source | Status |
| --- | --- | --- | --- | --- |
| **Lilli** | Non-wearable sensors (no cameras or microphones) that learn daily routines (moving, eating, sleeping, bathroom use, temperature) and flag changes; deployed through councils (reported in 12 councils, including Nottingham, Medway and Reading) | No price seen | <https://www.bromley.gov.uk/help-adults/adult-social-care-use-intelligent-lilli> | Unverified; outcome claims are council or vendor figures |

Howz, radar fall sensors (for example Vayyar) and acoustic monitoring in care homes were
searched for but nothing usable was returned; they are not covered.

## 2. Feature comparison with IRA CARE as it is today

Competitor cells summarise the material above (unverified). "—" means the material read did not
show the feature; it does not prove the feature is absent.

| Capability | IRA CARE status | Evidence in this repository | Monitored alarms (1.1) | Smartwatches (1.2) | Specialist wearables (1.3) | Care platforms (1.4) | Companions (1.5) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Help button and spoken "help" | **Implemented** (simulated in demos) | `supracloud-jarvis/ira/tests/tenancy/test_care_sos_db.py` | Yes, the core product | Yes (SOS) | Yes | — | Alexa: voice call for help (former product) |
| A trained human answers 24/7 | **Not built.** No ARC contract; a webhook adapter exists and logs only; no ARC protocol such as SCAIP or BS 8521 is implemented; no staff | `supracloud-jarvis/ira/care/providers.py`; `docs/evidence/DEPENDENCY_REGISTER.md` (group K) | Yes | No (calls 999 or contacts) | Optional (Buddi, MindMe) | — | ElliQ: no |
| Automatic fall detection | **Implemented** in the watch app (model v3 plus a rule backstop); about 78 % of simulated falls caught on held-out young volunteers; about 10 false alarms an hour on that dataset's dense activity; never measured on older adults, a real home or a physical watch | `docs/ai/fall/MODEL_CARD_V3.md`; `docs/compliance/CLAIMS_REGISTER.md` | Optional add-on | Yes, built in | Buddi yes; Taking Care Anywhere yes | — | — |
| Calls the emergency services automatically | **Not built, by design.** IRA never dials 999; the last tier tells people to call 999 | `docs/PRODUCT_SPEC.md`, section 5 | Operator decides | Apple yes; Samsung on some models | — | — | — |
| Proactive check-ins by voice through the day | **Implemented** (backend and watch app, worded by Gemma 4 inside fixed rules, or by templates with no model); speech on a real watch untested | `docs/evidence/TECHNICAL_ENDORSEMENT_REPORT.md`, section 1; `docs/evidence/demo/REAL_MODEL_RUN.md` | — | — | — | — | ElliQ: proactive conversation (tabletop, not worn) |
| "No answer and no movement" escalation | **Implemented**; sensitivity in real homes unmeasured | `supracloud-jarvis/ira/care/monitoring.py`; `docs/compliance/CLAIMS_REGISTER.md` | — | Apple: immobility after a fall only | — | — | — |
| Asks the person first, then escalates in tiers (family, carer, care organisation) until someone acknowledges | **Implemented**; message delivery **simulated** (log-only providers; a Twilio adapter exists, off, never used live) | `supracloud-jarvis/ira/care/escalation.py`; `docs/progress/GO_NO_GO.md` | ARC calls a responder list | Countdown, then 999 or contacts | Family app alerts (Buddi) | — | Alexa Together: caregiver alerts (former) |
| Heart rate outside a carer-set range | **Implemented**; accuracy on any watch and on older adults unmeasured | `docs/compliance/CLAIMS_REGISTER.md` (heart_rate) | — | Not compared | Doro Watch tracks heart rate | — | — |
| Watch off, battery low, sync lost, sensor permission off | **Implemented** | `docs/PRODUCT_SPEC.md`, section 4 | Device fault alerts (not compared) | — | — | — | — |
| Location with a help request | **Implemented** with consent; indoor accuracy unmeasured | `docs/compliance/CLAIMS_REGISTER.md` (location) | GPS models | Yes | MindMe, Taking Care Anywhere | — | — |
| Independent of the home landline | **By design** (watch and phone data); never tested on a real network | `docs/PRODUCT_SPEC.md`, section 6 | Mobile and GPS units yes; home units depend on the line or a SIM | Cellular models | Yes | — | Needs home broadband |
| Family phone app | **Implemented** (Android release build in CI; no iOS build; not in any store) | `docs/progress/GO_NO_GO.md` | Varies | Companion phone apps | Buddi yes | Some offer family portals (not compared) | ElliQ and Alexa yes |
| Care organisation dashboard for many people | **Implemented** | `dashboard/` | ARC operator systems (not for care providers) | — | — | Yes, the core product | — |
| Medication reminders, confirmed only on a clear yes | **Implemented** (self-reported by the person; not an eMAR) | `docs/compliance/CLAIMS_REGISTER.md` | — | Reminders (not compared) | — | eMAR (carer records administration) | ElliQ reminders |
| Planned carer visits and care plans shown to the wearer | **Implemented** | `supracloud-jarvis/ira/tests/tenancy/test_care_visits_db.py`; `test_care_plans_db.py` | — | — | — | Visits and plans are their core data | — |
| Rostering, eMAR, invoicing, payroll | **Not built** | — | — | — | — | Yes | — |
| Integration with care-record platforms or GP Connect | **Not built** | — | Some ARC platforms integrate with council systems (not verified) | — | — | Yes (Access: GP Connect) | — |
| Consent-controlled memory; transcripts private until consent | **Implemented** | `supracloud-jarvis/ira/tests/tenancy/test_care_memory_db.py` | Not applicable | — | — | — | Not compared |
| Works with no language model | **Implemented** (rules-only profile) | `deploy/compose.nomodel.yml`; `docs/MODEL_CHOICE.md` | Not applicable | Not applicable | Not applicable | Not applicable | — |
| Non-wearable, passive monitoring | **Not built** (IRA needs the watch to be worn) | — | Optional home sensors | No | No | No | Lilli: yes |
| Own hardware | **Not built**: a design, a 3D model and a request-for-quotation draft only | `docs/hardware/BLUEPRINT.md`; `docs/hardware/EVT_DVT_PVT_ROADMAP.md` | Mature devices | Mature devices | Mature devices | Not applicable | ElliQ: own device |
| Battery life on a real day | **Unmeasured** | `docs/evidence/DEPENDENCY_REGISTER.md` (group A) | Long (pendants) | About a day or more (not verified) | Varies | Not applicable | Mains powered |
| Regulatory position and quality certification | Not a medical device by intended purpose, **unconfirmed** by any adviser; no Clinical Safety Officer review; no TEC quality certification | `docs/compliance/INTENDED_PURPOSE.md`; `docs/progress/GO_NO_GO.md` | Established services (TSA certification common; not verified per provider) | Samsung: "not a medical device" | Not checked | DSCR assured suppliers | Not checked |
| Real users, track record, published price | **None** | `docs/evidence/ASSUMPTIONS_REGISTER.md`, section 1 | Yes | Yes | Yes | Yes | Yes |

## 3. Where IRA CARE might differ

These are design differences that the repository can show working in software. None is proved
with users, and none is claimed to be unique: the search was limited, vendor pages could not be
read, and a product doing the same may exist.

1. **It starts the conversation.** Alarms and smartwatches wait for a button press or an
   impact. IRA asks the person how they are through the day and treats silence plus stillness as
   a reason to act. Among the products read, only ElliQ (a US tabletop device, not worn) is
   described as proactive, and none of the UK alarm products is.
2. **Ask first, then a human chain.** Every automated signal asks the wearer before telling
   anyone, then goes to family, carer and care organisation in order until someone acknowledges,
   and "I'm on my way" is shown on the wrist. Smartwatches go from a countdown to 999 or a
   contact list; alarm services route through an ARC operator.
3. **One record for the wearer, the family and the care provider**, including planned visits,
   care plans and medication prompts, so the watch can say "your carer is due at 10". The alarm
   products read do not describe this; care platforms hold the data but not the wearable.
4. **Rules the language model cannot override.** The product works with no model, and the model
   only words questions and replies inside deterministic safety rules. This is a design
   property, not a customer benefit anyone has confirmed.
5. **Runs on a commercial Wear OS watch** with no landline, which is relevant while the January
   2027 switchover forces analogue alarm replacement (`MARKET_EVIDENCE.md`, T2 and T3). It has
   never run on one.

## 4. Where competitors are stronger

- **A trained human answers, around the clock.** This is what councils and families buy in a
  monitored alarm. IRA CARE has no ARC, no staff and no protocol connection to one.
- **Proven devices.** Pendants run for long periods on a battery, are simple to use and have
  years of field use. IRA CARE has not run on any physical device and its battery cost is
  unmeasured.
- **Fall detection at scale.** Apple and Samsung ship fall detection to millions of people,
  and telecare fall detectors have long field use. IRA CARE's detector is trained on young
  volunteers' simulated falls and catches about 78 % of them in that data.
- **Automatic emergency calls.** Apple Watch calls 999 itself. IRA CARE deliberately does not.
- **Price, simplicity and channels.** Alarm services publish monthly prices, sell through Age
  UK, councils and housing providers, and are understood by buyers. IRA CARE has no price, no
  channel and no customer.
- **Provider workflow.** Birdie, Access and Nourish already hold the care provider's rota,
  records and eMAR, and most providers have one. IRA CARE would have to sit beside them, and it
  has no integration.
- **Regulatory and quality standing.** Established providers operate inside known quality
  schemes and procurement frameworks. IRA CARE has no clinical safety sign-off, no regulatory
  view and no certification.

## 5. What would change this picture

- Reading each vendor's own pages (prices, fall-detection limits, ARC response standards) and the
  TSA's quality framework at source.
- The interviews in `VALIDATION_INTERVIEWS.md`: whether buyers value proactive check-ins at all,
  and whether care providers would act on IRA's tier-2 alerts.
- Any measured IRA CARE result on a physical watch with older adults.

## Sources that could not be reached

None of these could be opened from the build environment on 26 November 2026; every price and
feature taken from them is unverified: `careline.co.uk`, `taking.care`, `www.ageuk.org.uk`,
`personalalarms.ageuk.org.uk`, `www.tunstall.co.uk`, `uk.tunstall.com`, `chubbfs.com`,
`buddi.co.uk`, `doro.com`, `support.apple.com`, `www.samsung.com`, `birdie.care`,
`nourishcare.com`, `elliq.com`, `www.aboutamazon.com`, `www.which.co.uk`,
`www.telecare24.co.uk`, council sites (`www.harrow.gov.uk`, `www.bromley.gov.uk`) and the
government Digital Marketplace (`www.applytosupply.digitalmarketplace.service.gov.uk`, where
G-Cloud listings often publish prices).
