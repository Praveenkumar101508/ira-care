import type { Metadata } from 'next';
import './globals.css';
import { TopBar } from '@/components/TopBar';

export const metadata: Metadata = { title: 'IRA CARE dashboard', description: 'Carer dashboard for IRA CARE' };

export default function RootLayout({ children }: { children: React.ReactNode }) {
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
