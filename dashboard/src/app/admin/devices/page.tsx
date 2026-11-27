'use client';

import { useEffect, useState } from 'react';
import { DeviceAdminScreen, NotDeploymentAdmin } from '@/components/DeviceAdmin';
import { useApi } from '@/components/useApi';
import { ApiError } from '@/lib/api';
import { cacheKey, rememberAdmin, type AdminMe } from '@/lib/deviceAdmin';

/** Deployment administration (D-085, D-089). The server is asked first whether this account
 *  holds the grant; without it the page refuses and shows nothing about watches or actions. */
export default function DeviceAdminPage() {
  const { api, session, ready } = useApi();
  const [me, setMe] = useState<AdminMe | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!api || !session) return;
    let gone = false;
    api.deploymentAdminMe()
      .then((m) => { if (gone) return; rememberAdmin(cacheKey(session.baseUrl, session.accessToken), !!m.deployment_admin); setMe(m); setError(''); })
      .catch((e) => {
        if (gone) return;
        if (e instanceof ApiError && e.status === 401) { window.location.href = '/login'; return; }
        // Not knowing is treated as not an administrator: nothing is shown.
        if (e instanceof ApiError && (e.status === 403 || e.status === 404)) { setMe({ deployment_admin: false, auth_age_seconds: null, step_up_required: false, max_age_seconds: 0, mfa_enrolled: false }); return; }
        setError(e instanceof ApiError ? `Could not check whether you are a deployment administrator: ${e.message}` : 'Could not check whether you are a deployment administrator.');
      });
    return () => { gone = true; };
  }, [api, session]);
  if (!ready || !api || !session) return null;
  if (error) return <p className="error" role="alert" data-testid="me-error">{error}</p>;
  if (!me) return <p className="muted" aria-live="polite">Checking who you are…</p>;
  if (!me.deployment_admin) return <NotDeploymentAdmin />;
  return <DeviceAdminScreen api={api} session={session} me={me} onMe={setMe} />;
}
