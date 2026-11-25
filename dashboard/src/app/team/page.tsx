'use client';

import { useCallback, useEffect, useState } from 'react';
import { CaseloadList } from '@/components/Team';
import { useApi } from '@/components/useApi';
import { ApiError } from '@/lib/api';
import { may } from '@/lib/carePlan';
import type { Caseload, Permissions, StaffMember } from '@/lib/types';

export default function TeamPage() {
  const { api, ready } = useApi();
  const [perms, setPerms] = useState<Permissions | null>(null);
  const [caseload, setCaseload] = useState<Caseload | null>(null);
  const [staff, setStaff] = useState<StaffMember[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const load = useCallback(async () => {
    if (!api) return;
    try {
      const p = await api.permissions();
      setPerms(p);
      setCaseload(await api.caseload());
      setStaff(may(p, 'designate_coordinators') ? await api.staff() : []);
      setError('');
    } catch (e) { setError(e instanceof ApiError ? e.message : 'Could not load.'); }
  }, [api]);
  useEffect(() => { void load(); }, [load]);
  if (!ready) return null;
  const onCoordinator = async (userId: string, on: boolean) => {
    if (!api) return;
    setBusy(true);
    try { await api.setCoordinator(userId, on); await load(); }
    catch (e) { setError(e instanceof ApiError ? e.message : 'Could not change.'); }
    finally { setBusy(false); }
  };
  return (
    <>
      <h2>{caseload?.everyone ? 'Caseloads' : 'My caseload'}</h2>
      {perms ? <p className="muted">{perms.label}: {perms.who}</p> : null}
      {error ? <p className="error">{error}</p> : null}
      {caseload ? <CaseloadList caseload={caseload} staff={staff} canDesignate={may(perms, 'designate_coordinators')} busy={busy} onCoordinator={onCoordinator} /> : null}
    </>
  );
}
