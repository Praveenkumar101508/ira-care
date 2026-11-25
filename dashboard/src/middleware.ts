import { NextResponse, type NextRequest } from 'next/server';
import { demoPagesEnabled, isDemoPath } from './lib/demoPages';

/** Demonstration pages answer 404 unless this is a demonstration build (lib/demoPages.ts). */
export function middleware(request: NextRequest) {
  if (isDemoPath(request.nextUrl.pathname) && !demoPagesEnabled()) {
    return new NextResponse('Not found', { status: 404 });
  }
  return NextResponse.next();
}

export const config = { matcher: ['/watch/:path*', '/presenter/:path*', '/simulator/:path*'] };
