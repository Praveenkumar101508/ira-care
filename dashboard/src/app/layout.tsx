import type { Metadata } from 'next';
import { headers } from 'next/headers';
import './globals.css';
import { TopBar } from '@/components/TopBar';

export const metadata: Metadata = { title: 'IRA CARE dashboard', description: 'Carer dashboard for IRA CARE' };

/**
 * Reading the request headers makes every page render per request, which the Content-Security-
 * Policy needs: Next.js can only put this response's nonce (set by src/middleware.ts) on its
 * scripts when the page is rendered for that response, not prerendered at build time.
 */
export default async function RootLayout({ children }: { children: React.ReactNode }) {
  await headers();
  return (
    <html lang="en-GB">
      <body>
        <a href="#main" className="skip-link">Skip to content</a>
        <TopBar />
        <main id="main">{children}</main>
      </body>
    </html>
  );
}
