'use client';

import { useEffect, useState } from 'react';
import { useApi } from '@/components/useApi';
import { ApiError } from '@/lib/api';
import type { PlanSummary } from '@/lib/types';

/** The plan, its limits and usage. Prices are deliberately unset until the owner sets them; no payment provider is on by default. */
export default function BillingPage() {
  const { api, ready } = useApi();
  const [plan, setPlan] = useState<PlanSummary | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!api) return;
    api.plan().then(setPlan).catch((e) => setError(e instanceof ApiError ? e.message : 'Could not load.'));
  }, [api]);
  if (!ready) return null;
  return (
    <>
      <h2>Plan and billing</h2>
      {error ? <p className="error">{error}</p> : null}
      {plan ? (
        <>
          <div className="card">
            <strong>Your plan:</strong> {plan.subscription ? `${plan.subscription.display_name} (${plan.subscription.status}${plan.subscription.current_period_end ? `, until ${plan.subscription.current_period_end.slice(0, 10)}` : ''})` : 'none recorded'}
            <div className="muted">People: {plan.usage.care_people}{plan.limits.care_people_max != null ? ` of ${plan.limits.care_people_max}` : ''} · circle members: {plan.usage.care_circle_members}{plan.limits.care_circle_members_max != null ? ` (up to ${plan.limits.care_circle_members_max} per person)` : ''}</div>
            <div className="muted">Payment provider: {plan.payment_provider === 'off' ? 'off (no card is ever taken on this deployment)' : plan.payment_provider}</div>
          </div>
          <div className="card">
            <strong>Plans</strong>
            <table><thead><tr><th>Plan</th><th>What it allows</th><th>Price</th></tr></thead><tbody>
              {plan.plans.map((p) => <tr key={p.plan_id}><td>{p.display_name}</td><td>{p.description}</td><td className="muted">not set</td></tr>)}
            </tbody></table>
            <p className="muted">{plan.note}</p>
          </div>
        </>
      ) : null}
    </>
  );
}
