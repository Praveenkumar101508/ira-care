# Tunnel for the watch device test server

Checked on 28 November 2026 for `.github/workflows/watch-device-test.yml`. The need: an HTTPS
address a real watch can reach, for a server on a GitHub-hosted runner, for up to 5 hours, at £0,
with no account and no card.

## Chosen: Cloudflare Quick Tunnels (TryCloudflare)

`cloudflared tunnel --url http://127.0.0.1:8080` gives a random `https://….trycloudflare.com`
address. No Cloudflare account, no domain, no card.

What Cloudflare's own pages say, and what that means here:

| Term or limit | Source | Effect on this test |
|---|---|---|
| Meant for testing and development, not production; no SLA or uptime guarantee | Cloudflare Tunnel docs, Quick Tunnels page | Acceptable: a time-limited test with fictional data. Never used for real people. |
| Governed by Cloudflare's Website and Online Services Terms of Use (no unlawful or prohibited use) | cloudflare.com/policies/terms | Acceptable: no other terms apply to an accountless quick tunnel that we found. |
| Up to 200 in-flight requests; more get HTTP 429 | Quick Tunnels page | One watch and one dashboard: far below. The watch treats 429 as "try again" (`CareApi.isRetryable`). |
| Server-Sent Events not supported (the edge buffers text/event-stream) | Quick Tunnels page | The watch's SOS stream is application/x-ndjson, not SSE, with a heartbeat every 30 s; if the edge holds it back, "[Name] is on the way" can be late. Help itself is acknowledged by the Help request's own answer. Step 7 of the guide reports it either way. |
| The address changes every time | Quick Tunnels page | Each run prints its new address; the watch's test server is typed again. |
| Anyone with the address can reach the service (an optional e-mail check exists since 2 October 2026) | Tunnel changelog | Not used: the watch cannot answer an e-mail challenge. The address is public in the run summary; the dashboard needs the password from the repository secret, the watch API needs a one-time pairing code or a device token. |

The client is the pinned `cloudflared` release 2026.8.1, checked against the SHA-256 digest
GitHub records for the release asset before it runs.

## Considered and not used

| Option | Why not |
|---|---|
| ngrok, Tailscale Funnel, a named Cloudflare Tunnel | Need an account. |
| localtunnel (loca.lt) | Shows a reminder page to non-browser clients unless a bypass header is sent; the watch app sends no such header. |
| bore, plain TCP forwarders | No HTTPS. The release and test builds refuse cleartext to anything but the emulator. |
| localhost.run, serveo, pinggy free tiers | SSH-based; their current terms could not be read from the build environment on this date. Next-best if Cloudflare's quick tunnels stop: check the chosen one's terms first, then change only the tunnel step. |
