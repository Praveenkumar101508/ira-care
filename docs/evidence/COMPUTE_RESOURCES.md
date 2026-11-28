# Computing resources for the remaining verification (26 November 2026)

Researched on 26 November 2026, to find free or low-cost ways to clear the blockers that need
more computing power than the build container has (4 cores, 15 GB RAM, about 20 GB free disk, no
`/dev/kvm`). Nothing here was signed up for, paid for or activated.

Each figure carries a tag:
- **[V]**: read on the official page that day.
- **[S]**: taken from a search-engine extract of the official page named; the page itself could
  not be opened from the build environment.
- **[NV]**: not verified (a third-party source or recall).

Check the [S] and [NV] items before relying on them.

## What each job needs

| Job | Needs | Requirement |
|---|---|---|
| Build the IRA CARE OS image | x86-64 Linux, about 400 GB free disk, 64 GB RAM [S: source.android.com requirements] | E2.h |
| Boot the image (Cuttlefish) | x86-64 Linux with KVM [S: Cuttlefish get-started] | E2.i, E2.l, E2.n, E4.a–c |
| Android and Wear OS emulator runs | x86-64 Linux with KVM | D1.i, K.g |
| iOS simulator build | macOS with Xcode; a free Apple account is enough for the simulator [S] | D2.h (build part) |
| 24-hour wall-clock soak | Any host that stays up 24 h; about 4 cores, 8–16 GB | G2.e |
| Occasional GPU evaluation | A T4-class GPU for a few hours | none outstanding |

## GitHub Actions

**Update, 26 November 2026 (later): the owner made the repository public (Apache 2.0).** Standard GitHub-hosted runners are free for public repositories [V] <https://docs.github.com/en/billing/concepts/product-billing/github-actions>, including macOS. Larger runners are still charged. Everything below about private-repository allowances applies only to the period before the change, and to any private repository.

### Before the change (private repository, personal account)

- **Included usage:** now given as money, not minutes: **Free US$12 a month, Pro US$18 a month**. [V] <https://docs.github.com/en/billing/concepts/product-billing/github-actions>
- **Rates:** Linux 2-core US$0.006/min; macOS US$0.062/min [V] <https://docs.github.com/en/billing/reference/actions-runner-pricing>. So Free buys about 2,000 Linux minutes or about 193 macOS minutes a month.
- **Private-repository runners:** Linux 2 vCPU / 8 GB / 14 GB SSD; macOS arm64 3 vCPU / 7 GB. [V] <https://docs.github.com/en/actions/reference/runners/github-hosted-runners>
- **Emulators:** Linux runners support hardware acceleration for Android tools [V], after a udev rule for `/dev/kvm` [S].
- **Limits:** 6 hours per job on hosted runners; 5 days per job on a self-hosted runner. [V] <https://docs.github.com/en/actions/reference/limits>
- **Charges:** with no payment method, usage is blocked once the included amount is used [V]. With a payment method, spending is limited to US$0 until a budget is set [S]. User-level budgets always hard-stop [V].
- **This month's usage:** the jobs of this repository's November runs add up to about **6,100 Linux minutes** (counted from the job timings, 26 November). That is about **US$37** at the published rate, above both allowances. CI kept running, so either a budget or payment method is in place, or usage was not blocked as described. **The founders should check Settings → Billing**. No extra workflow was started this round for this reason: `emulator.yml` and `ios-simulator.yml` are manual-only.
- **Fit:**
  - emulators: yes;
  - iOS simulator build: yes, within the macOS allowance;
  - OS build: no (disk and RAM too small);
  - 24-hour soak: no on hosted runners (6-hour cap), but possible on a self-hosted runner on a founder machine.

## Kaggle and Colab

- **Kaggle:** about 4 CPU cores, about 30 GB RAM, about 30 GPU hours a week, 12-hour sessions, no KVM [NV].
  - **Terms:** Kaggle's terms describe the services as for "your own internal, personal, non-commercial use" [S] <https://www.kaggle.com/terms>.
  - This round's fine-tune (#111) and earlier training runs used Kaggle notebooks. **The founders should read the terms and decide** whether further company work should run there. The trained artefacts that ship (the fall model, the classifier heads) do not depend on Kaggle staying available.
- **Google Colab (free):** at most 12 hours per session, which can end without warning [S] <https://research.google.com/colaboratory/faq.html>. Short GPU tests only.

## University of Essex

- **HPC:** open to staff, students, and external collaborators working with University staff on research [S] <https://www.essex.ac.uk/staff/it-services/hpc>. There is no route for alumni or startups on their own; a staff sponsor is needed. Batch HPC normally offers no root, Docker or KVM [NV].
- **Essex Startups:** free 1:1 advice, hot-desking at Studio X, and programmes such as *evolve* (2023 terms). No cloud credits or compute were found [S]. Contact: **startups@essex.ac.uk** [S].
- **Done:** an enquiry is drafted, unsent, in the founders' Gmail. It asks about a staff sponsor for HPC, a KVM machine, a 24-hour host, and partner cloud credits.

## Startup credits and free tiers (founders' sign-up needed; none activated)

| Programme | What it gives | Eligibility | Card / charges | KVM (nested) | Useful for |
|---|---|---|---|---|---|
| Google for Startups Cloud, Start tier | Up to US$2,000 for 1 year [S] <https://cloud.google.com/startup/pre-funded> | Founded within 2 years, working MVP, company-domain email matching the website [S] | Billing account; usage beyond credits is billed; set budget alerts | Yes, on Intel N2/N4/C3 (not E2 or AMD) [S] | OS build and boot; emulators; soak |
| AWS Activate Founders | US$1,000 [S] <https://aws.amazon.com/startups/credits> | Unfunded, company website, new to Activate [S] | Card; overage billed | Yes, on C7i/M7i/R7i and newer since 2026 [S] | OS build and boot; emulators; soak |
| Microsoft for Startups | Up to US$5,000 after business verification [V] <https://www.microsoft.com/en-us/startups> | Early-stage software startup | Azure subscription [NV] | Dv3/Ev3 and later Intel [S] | OS build and boot; soak |
| GCP Free Trial | US$300 / 90 days [S] | New customers | Card; no charges until upgraded [S] | 8 vCPU cap [NV] | Emulators; soak |
| Oracle Always Free | Reportedly cut to 2 OCPU / 12 GB ARM in 2026 [NV] | Anyone | Card for verification [NV] | ARM; not verified | Soak, if the stack runs on arm64 |
| Codemagic (personal) | 500 macOS M2 minutes a month [S] <https://docs.codemagic.io/billing/pricing/> | Personal or hobby use [S] | Free minutes kept with billing on | — | iOS simulator build (check the commercial-use wording) |

**Cost of an OS build host from credits** (third-party prices [NV]):
- GCP n2-highmem-8 (8 vCPU, 64 GB, Intel), London: about US$0.68 an hour. US$2,000 of Start credits covers about 2,900 hours if the machine is stopped between builds.
- AWS m7i.4xlarge (16 vCPU, 64 GiB): about US$0.93 an hour in London.
- Add a 500 GB disk in either case.

## Recommended route, cheapest first

1. **Emulators:** run `.github/workflows/emulator.yml` (manual) on GitHub's standard Linux runner. Free now that the repository is public.
2. **iOS simulator build:** run `.github/workflows/ios-simulator.yml` (manual) on the standard macOS runner. Free now that the repository is public.
3. **24-hour soak:** a founder laptop or desktop left on for a day, following `docs/evidence/soak/HOW_TO_RUN_24H.md`. Alternatively, a few dollars of GCP or AWS credits on a 4 vCPU instance.
4. **OS build and boot:** apply for Google for Startups Start (or AWS Activate), then use an Intel 64 GB / 500 GB VM with nested virtualisation, stopped between builds. Ask Essex Startups about a sponsor and partner credits first.
5. **GPU evaluation:** nothing is outstanding.

## Round 10 (27 November 2026): what each remaining job can run on, at no cost

**Owner restriction: the owner's personal Mac is never used** (too little storage). Read from the
build machine on 27 November 2026; only GitHub's documentation was reachable from it, so every
other row is unverified here and must be checked in a browser before anyone signs up. Nothing has
been signed up to, accepted or paid for.

| Job | Needs | Free option | Verified limits | Status |
|---|---|---|---|---|
| A. 24-hour soak | one Linux process held for 24 h | (1) a GitHub self-hosted runner on a server someone lends (job up to 5 days); (2) a university or sponsor's Linux VM | GitHub-hosted job limit **6 hours**, self-hosted **5 days**, workflow run **35 days** (<https://docs.github.com/en/actions/reference/limits>, read today) | not available yet; meanwhile `.github/workflows/soak.yml` runs **5.5 hours** on a free standard runner |
| B. AOSP build | about 64 GB RAM, 400 GB disk, x86-64, KVM | Essex research computing (asked); a sponsor's workstation | a standard runner (14 GB SSD, 7-16 GB RAM, earlier rows) cannot hold an AOSP tree | not available |
| C. GPU deployment check | an NVIDIA GPU of 16 GB or more, commercial use allowed | Kaggle's T4 was used for measurement (its terms limit commercial use: earlier rows) | not re-checked today: the provider pages are unreachable from here | measured once on Kaggle (D-074); a commercially usable GPU not available |
| D. Hosted staging and a representative load test | a server that stays up, a domain, HTTPS | a sponsor's VM; cloud credits programmes (earlier rows, founders' sign-up) | unverified today | not available |
| E. Recorded-dataset AI evaluation | a few GB of disk, CPU | **the build machine**: BITS-2 and UCI HAR were fetched, checksummed, evaluated and deleted here on 27 November (D-110) | done | done for BITS-2 and UCI HAR; Common Voice waits for the owner's account decision |

What to verify for any provider before use (the owner's checklist): the continuous-runtime limit;
whether the free tier needs a card and what happens at its limit (a hard stop, not a charge); the
commercial-use terms; disk and memory; whether nested virtualisation or KVM is available (for the
OS and the emulators); and that billing alerts or a spending cap of zero can be set.
