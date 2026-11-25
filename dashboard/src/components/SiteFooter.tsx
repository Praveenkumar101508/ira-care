'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

/** A quiet footer on every dashboard page: where the open-source licences are. */
export function SiteFooter() {
  const pathname = usePathname();
  // The browser watch is a watch: nothing around it (as for the top bar).
  if (pathname?.startsWith('/watch')) return null;
  return (
    <footer className="site-footer muted">
      <Link href="/licences">Open-source licences</Link>
    </footer>
  );
}
