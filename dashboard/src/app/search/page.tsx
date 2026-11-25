'use client';

import { useState } from 'react';
import { SearchResults } from '@/components/Team';
import { useApi } from '@/components/useApi';
import { ApiError } from '@/lib/api';
import { searchable } from '@/lib/team';
import type { SearchHit } from '@/lib/types';

export default function SearchPage() {
  const { api, ready } = useApi();
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [error, setError] = useState('');
  if (!ready) return null;
  const run = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!api || !searchable(q)) { setError('Type at least two letters.'); return; }
    try { setHits(await api.searchAll(q.trim())); setError(''); }
    catch (err) { setError(err instanceof ApiError ? err.message : 'Could not search.'); }
  };
  return (
    <>
      <h2>Search</h2>
      <p className="muted">People by name, visits by purpose or carer, alerts by kind, among the people you look after. What anyone said is searched only on a person&apos;s own record, where consent applies.</p>
      <form role="search" onSubmit={run} className="row">
        <label htmlFor="q">Search for</label>
        <input id="q" value={q} maxLength={80} onChange={(e) => setQ(e.target.value)} />
        <button type="submit">Search</button>
      </form>
      {error ? <p className="error">{error}</p> : null}
      {hits ? <SearchResults hits={hits} /> : null}
    </>
  );
}
