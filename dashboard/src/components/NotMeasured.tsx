'use client';

import { useEffect, useState } from 'react';
import type { Api } from '@/lib/api';
import type { Capability } from '@/lib/types';

/**
 * What the watch does not measure, said plainly with the reason (register D4). Nothing here ever
 * shows a value: a number IRA cannot measure would be worse than none.
 */
export function NotMeasured({ api }: { api: Pick<Api, 'capabilities'> }) {
  const [items, setItems] = useState<Capability[] | null>(null);
  useEffect(() => {
    let live = true;
    api.capabilities().then((c) => { if (live) setItems(c.filter((x) => x.status === 'not_available')); }).catch(() => { if (live) setItems(null); });
    return () => { live = false; };
  }, [api]);
  if (!items || items.length === 0) return null;
  return (
    <div className="card" data-testid="not-measured">
      <strong>Not measured by IRA CARE</strong>
      <ul>
        {items.map((c) => <li key={c.key} data-testid={`not-measured-${c.key}`}><strong>{c.label}</strong>: not available. <span className="muted">{c.reason}</span></li>)}
      </ul>
    </div>
  );
}
