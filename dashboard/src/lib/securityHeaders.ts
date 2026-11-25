/**
 * Headers on every dashboard response.
 *
 * The session token lives in sessionStorage, so nothing may frame the page (clickjacking),
 * guess content types, leak the URL to other sites, load plugins or re-point relative URLs.
 * The browser watch speaks and listens, so the microphone stays allowed for this origin only.
 *
 * Not here yet: a script-src policy. Next.js injects inline scripts, so a strict one needs
 * per-request nonces, and the API origin is set per deployment; see docs/security/REVIEW.md.
 */
export const CONTENT_SECURITY_POLICY = [
  "frame-ancestors 'none'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join('; ');

export const SECURITY_HEADERS: { key: string; value: string }[] = [
  { key: 'Content-Security-Policy', value: CONTENT_SECURITY_POLICY },
  { key: 'X-Frame-Options', value: 'DENY' },
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  { key: 'Referrer-Policy', value: 'no-referrer' },
  { key: 'Permissions-Policy', value: 'camera=(), geolocation=(), payment=(), usb=(), microphone=(self)' },
  { key: 'Strict-Transport-Security', value: 'max-age=31536000; includeSubDomains' },
];
