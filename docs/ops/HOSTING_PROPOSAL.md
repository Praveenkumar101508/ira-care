# IRA CARE: hosting proposal and cost model (requirement G4.b)

Written 25 November 2026. A proposal for the owner to decide on, not a purchase. Nothing has been
rented or contracted (G4.d needs the owner's approval to spend).

## What was measured

From the containerised stack run on 25 November 2026 (`docs/evidence/staging/containers.json`,
`docs/evidence/staging/CONTAINERS.md`):

- **The stack:** PostgreSQL, Redis, the API, the worker, the dashboard, Caddy for HTTPS, and
  the backup service. It ran on one machine with 4 CPUs and 15 GB of memory, which it shared
  with other builds.
- **Load test:** 10, 25 and 50 simulated watches sending readings and Help through Caddy gave
  118 to 132 requests a second with no errors. The 95th-percentile report times were 142 ms,
  312 ms and 730 ms.
- **Bottleneck:** the API ran at 83–95 % of **one** core (one server process) and the database at
  about 60 %. The single API process, not the machine, set the limit.
- **Images:** the API image is 1.55 GB and the dashboard image 0.35 GB.
- **Excluded:** the conversation model (Ollama) was left out. With it the machine needs about
  10 GB more for the models, or a GPU profile (`deploy/compose.gpu.yml`).

## What one watch asks of the server

- Readings go up every 60 seconds by default (`reading_upload_interval_s`, the carer's setting).
  A background sync runs every 15 minutes (`watch/.../SyncWorker.kt`).
- Help, falls and answers are sent at once.
- So a watch makes roughly **one to three requests a minute** in ordinary use. The figure is an
  estimate from the code's intervals, not a measurement of real wear.
- At the measured 118 requests a second for one API process, and keeping half of it in reserve
  for bursts and the worker, one process carries in the order of **a thousand watches**.
- This is an extrapolation from a single-machine baseline. The capacity test on a representative
  server (G2.f) is still blocked, and the proposal does not depend on the estimate being right:
  the pilot tier is far below it.

## Proposed tiers

| Tier | People | Shape | Why |
| --- | --- | --- | --- |
| Pilot | up to 50 | One UK-region VM, 4 vCPU and 16 GB, running the compose stack as tested; 100 GB disk; encrypted backups to an S3-compatible bucket in a UK region (`deploy/offsite.sh`); a domain with Caddy's certificate | It is the configuration measured, with every drill passed on it. |
| Growth | up to about 1,000 | Two VMs behind the provider's load balancer, running the API, worker and dashboard; a managed PostgreSQL (2 vCPU, 8 GB) with point-in-time recovery; the same off-site backups | Removes the single machine as a single point of failure; the managed database takes over patching and failover. |

The conversation model is an option in both tiers: off (the product works without it), on the
VM's CPU (slow), or a GPU machine priced separately.

**Region and data:** all data stays in a UK region (DPIA section 6). The provider must offer a
UK region for compute, the database and object storage.

## Cost model

`scripts/hosting_cost.py` computes the monthly cost of a tier from a prices file:

```
python3 scripts/hosting_cost.py docs/ops/hosting_prices.json --tier pilot
```

The sizing is fixed in the script; the prices are inputs, each with its source URL and the date it
was read. Monthly cost counts 730 hours a month for hourly items. A missing price withholds the
total rather than guessing it.

**Prices are not filled in.** On 25 November 2026 the build environment could not reach any
provider's published prices: the price pages of DigitalOcean, Hetzner, OVHcloud and AWS, and the
Azure retail prices API, were all refused by the network. `docs/ops/hosting_prices.json` therefore
holds the items with empty prices. Filling them from the chosen provider's UK list prices, with the
date read, is a ten-minute task for the owner and produces the cost per month and per person.

## Assumptions, stated

- Ordinary wear produces one to three requests a minute per watch (from the code's intervals).
- No conversation model in the base cost.
- Backups: 14 days kept locally; the off-site bucket's lifecycle is set by the operator.
- Egress: alert messages, dashboard and phone traffic only; video and audio are never served.
- SMS and call costs are per message and per minute with the provider (G3.e), not part of hosting.
