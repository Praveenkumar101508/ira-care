/** Where the browser reaches the care API. Inlined at build time; empty means the same origin. */
export const DEFAULT_BASE_URL = process.env.NEXT_PUBLIC_CARE_API_BASE_URL ?? 'http://localhost:8000';
