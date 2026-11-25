import type { Metadata } from 'next';
import { Licences } from '@/components/Licences';
import { readNotices } from '@/lib/notices';

export const metadata: Metadata = { title: 'Open-source licences · IRA CARE dashboard' };

/** Open-source licences: this dashboard's third-party notices. Nobody needs to be signed in to read them. */
export default function LicencesPage() {
  return <Licences text={readNotices()} />;
}
