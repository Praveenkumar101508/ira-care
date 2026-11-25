/**
 * Headers on every dashboard response.
 *
 * The session token lives in sessionStorage, so nothing may frame the page (clickjacking),
 * guess content types, leak the URL to other sites, load plugins or re-point relative URLs.
 * The browser watch speaks and listens, so the microphone stays allowed for this origin only.
 *
 * The Content-Security-Policy is not in this static list: it carries a fresh nonce for every
 * request, so `src/middleware.ts` builds it with `buildContentSecurityPolicy` below and Next.js
 * puts the same nonce on its own scripts. See docs/security/REVIEW.md (S6).
 */
export const SECURITY_HEADERS: { key: string; value: string }[] = [
  { key: 'X-Frame-Options', value: 'DENY' },
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  { key: 'Referrer-Policy', value: 'no-referrer' },
  { key: 'Permissions-Policy', value: 'camera=(), geolocation=(), payment=(), usb=(), microphone=(self)' },
  { key: 'Strict-Transport-Security', value: 'max-age=31536000; includeSubDomains' },
];

export interface CspOptions {
  /** A fresh, unguessable value for this one response (base64). */
  nonce: string;
  /** `next dev` only: React's development build needs eval to rebuild call stacks. */
  dev: boolean;
  /**
   * Where the browser reaches the care API (NEXT_PUBLIC_CARE_API_BASE_URL, inlined at build).
   * Empty means the API is behind the same origin (the production image's default).
   */
  apiBaseUrl: string | undefined;
  /** The page itself reached the browser over HTTPS (directly, or through the reverse proxy). */
  secure?: boolean;
}

/** The origin of the configured API (`https://api.example` from `https://api.example/x/`), or null. */
export function apiOrigin(apiBaseUrl: string | undefined): string | null {
  if (!apiBaseUrl) return null;
  try {
    const u = new URL(apiBaseUrl);
    if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;
    return u.origin;
  } catch {
    return null;
  }
}

/** A nonce: 128 random bits, base64. Web Crypto, so it works in the edge middleware. */
export function makeNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

/**
 * The page's Content-Security-Policy.
 *
 * script-src  Only scripts carrying this response's nonce run; 'strict-dynamic' lets those load
 *             the app's own chunks. No 'unsafe-inline' anywhere, and 'unsafe-eval' only under
 *             `next dev`. 'self' and https: are ignored by browsers that understand
 *             'strict-dynamic'; they only keep very old browsers working.
 * style-src   Style sheets from this origin, or an inline <style> carrying the nonce. Inline
 *             style *attributes* (React's `style={{...}}`, about sixty in the app) need
 *             style-src-attr 'unsafe-inline'. An attribute cannot run script; what is left is
 *             restyling an injected element, which an HTML injection allows anyway. Under
 *             `next dev` only, <style> elements are inline too (the error overlay needs it).
 * connect-src This origin and the configured care API only: an injected script cannot post the
 *             session token anywhere else. (So in a built dashboard the "Care server" field under
 *             Advanced can reach only the configured API.)
 * img-src     The app loads no images beyond its own origin (the browser's favicon request).
 */
export function buildContentSecurityPolicy({ nonce, dev, apiBaseUrl, secure = false }: CspOptions): string {
  if (!/^[A-Za-z0-9+/=_-]{16,}$/.test(nonce)) throw new Error('A CSP nonce must be at least 16 base64 characters.');
  const api = apiOrigin(apiBaseUrl);
  const connect = ["'self'", ...(api ? [api] : [])];
  const scripts = [`'nonce-${nonce}'`, "'strict-dynamic'", "'self'", 'https:', ...(dev ? ["'unsafe-eval'"] : [])];
  // `next dev` writes its error overlay's <style> elements without the nonce; a nonce in the list
  // would make the browser ignore 'unsafe-inline', so dev gets 'unsafe-inline' alone. Never built.
  const styles = dev ? "'self' 'unsafe-inline'" : `'self' 'nonce-${nonce}'`;
  const directives = [
    "default-src 'self'",
    `script-src ${scripts.join(' ')}`,
    `style-src ${styles}`,
    `style-src-elem ${styles}`,
    "style-src-attr 'unsafe-inline'",
    "img-src 'self'",
    "font-src 'self'",
    `connect-src ${connect.join(' ')}`,
    "media-src 'none'",
    "worker-src 'none'",
    "manifest-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-src 'none'",
    "frame-ancestors 'none'",
  ];
  // Only for a page served over HTTPS. On a plain-HTTP local or demonstration server it would
  // rewrite the app's own script addresses to https and break the page.
  if (!dev && secure) directives.push('upgrade-insecure-requests');
  return directives.join('; ');
}
