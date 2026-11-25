/**
 * The browser watch (/watch), its presenter controls (/presenter) and the old simulator address
 * (/simulator) are demonstration tools: the browser watch acts with a real device's pairing code
 * or device token. They exist only in a build made for a demonstration
 * (NEXT_PUBLIC_IRA_DEMO_PAGES=1, set by scripts/demo/apps.sh); every other build, the production
 * image included, answers 404 for them (src/middleware.ts).
 */
export const DEMO_PATHS = ['/watch', '/presenter', '/simulator'] as const;

export function isDemoPath(pathname: string): boolean {
  return DEMO_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

/** Read at build time: Next inlines NEXT_PUBLIC_ values, so a production build cannot turn it on later. */
export function demoPagesEnabled(value: string | undefined = process.env.NEXT_PUBLIC_IRA_DEMO_PAGES): boolean {
  return value === '1';
}
