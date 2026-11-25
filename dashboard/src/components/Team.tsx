'use client';

import Link from 'next/link';
import { hitHref, KIND_WORDS, byDayPlan, needsAttention } from '@/lib/team';
import type { Caseload, PlannedVisit, SearchHit, StaffMember } from '@/lib/types';

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }) + ' UTC' : '';
}

/** Search results: each one a link to the person's own record. */
export function SearchResults({ hits }: { hits: SearchHit[] }) {
  if (hits.length === 0) return <p className="muted" data-testid="no-results">Nothing found among the people you look after.</p>;
  return (
    <ul className="plain" data-testid="search-results">
      {hits.map((h) => (
        <li key={`${h.kind}-${h.id}`} className="card">
          <strong>{KIND_WORDS[h.kind]}</strong> · <Link href={hitHref(h)}>{h.person_name}</Link>
          {h.kind !== 'person' ? <span> · {h.text}{h.at ? ` · ${when(h.at)}` : ''}</span> : null}
        </li>
      ))}
    </ul>
  );
}

/** Each carer and the people they cover, with the next visit. Managers may name coordinators. */
export function CaseloadList({ caseload, staff, canDesignate, busy, onCoordinator }: {
  caseload: Caseload; staff: StaffMember[]; canDesignate: boolean; busy?: boolean;
  onCoordinator?: (userId: string, on: boolean) => void;
}) {
  if (caseload.carers.length === 0) return <p className="muted" data-testid="no-caseload">No carers with an account are in a care circle you can see.</p>;
  const isCoordinator = (userId: string) => staff.find((s) => s.user_id === userId)?.coordinator ?? false;
  return (
    <div data-testid="caseload">
      {caseload.carers.map((c) => (
        <section key={c.user_id} className="card" aria-label={`Caseload of ${c.name}`}>
          <h3>{c.name}{isCoordinator(c.user_id) ? <span className="muted"> · coordinator</span> : null}</h3>
          <ul className="plain">
            {c.people.map((p) => (
              <li key={p.member_id}><Link href={`/people/${encodeURIComponent(p.person_id)}`}>{p.name}</Link> · {p.next_visit ? `next visit ${when(p.next_visit)}` : 'no visit planned'}</li>
            ))}
          </ul>
          {canDesignate && onCoordinator ? (
            <label><input type="checkbox" disabled={busy} checked={isCoordinator(c.user_id)}
              onChange={(e) => onCoordinator(c.user_id, e.target.checked)} /> Coordinator: plans visits for everyone and sees every caseload</label>
          ) : null}
        </section>
      ))}
    </div>
  );
}

/** The coordinator's planning view: visits by day, problems first called out. */
export function VisitPlan({ visits }: { visits: PlannedVisit[] }) {
  if (visits.length === 0) return <p className="muted" data-testid="no-visits">No visits planned in the next seven days.</p>;
  return (
    <div data-testid="visit-plan">
      {byDayPlan(visits).map((g) => (
        <section key={g.day}>
          <h3>{new Date(`${g.day}T00:00:00Z`).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC' })}</h3>
          <ul className="plain">
            {g.visits.map((v) => {
              const flag = needsAttention(v);
              return (
                <li key={v.id} className={flag ? 'card error' : 'card'}>
                  {when(v.planned_start)} · <Link href={`/people/${encodeURIComponent(v.person_id)}?tab=care`}>{v.person_name}</Link>
                  {' · '}{v.purpose || 'Visit'} · {v.carer_name || 'no carer named'}{flag ? <strong> · {flag}</strong> : null}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}
