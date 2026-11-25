import { NextResponse, type NextRequest } from 'next/server';
import { demoPagesEnabled, isDemoPath } from './lib/demoPages';
import { buildContentSecurityPolicy, makeNonce } from './lib/securityHeaders';

/** Read on each call, not at import: Next inlines NEXT_PUBLIC_ values at build time anyway. */
function apiBaseUrl(): string {
  return process.env.NEXT_PUBLIC_CARE_API_BASE_URL ?? 'http://localhost:8000';
}

/**
 * Every page gets a Content-Security-Policy with a nonce of its own. The policy goes on the
 * request too: Next.js reads the nonce from it and puts it on the scripts it writes into the
 * page, and the root layout reads it (x-nonce), which also makes every page render per request.
 *
 * Demonstration pages answer 404 unless this is a demonstration build (lib/demoPages.ts).
 */
export function middleware(request: NextRequest) {
  if (isDemoPath(request.nextUrl.pathname) && !demoPagesEnabled()) {
    return new NextResponse('Not found', { status: 404 });
  }
  const nonce = makeNonce();
  // Behind the reverse proxy (deploy/Caddyfile) the page is HTTPS though Next.js hears HTTP.
  const secure = request.nextUrl.protocol === 'https:' || request.headers.get('x-forwarded-proto') === 'https';
  const csp = buildContentSecurityPolicy({ nonce, dev: process.env.NODE_ENV === 'development', apiBaseUrl: apiBaseUrl(), secure });
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set('x-nonce', nonce);
  requestHeaders.set('Content-Security-Policy', csp);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set('Content-Security-Policy', csp);
  return response;
}

/** Everything but the build's static files (scripts and style sheets carry no policy of their own). */
export const config = { matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'] };
