/** Field-by-field comparison of two published policies (#/policy/compare/<base>/<other>). */
import { useId, useState } from 'react';
import type { PolicyRecord } from '../../api/types';
import { ShortId } from '../../ui/primitives';
import { routeHref } from '../../state/router';
import { differences, diffBodies, summarize } from './diff';
import { defaultComparison, newestFirst, policyByHash } from './model';
import { DiffTable } from './PolicyParts';
import { ViewHeading, type PolicyContext } from './shared';

function optionLabel(p: PolicyRecord, activeHash: string | null | undefined): string {
  return `${p.name} · ${p.policy_hash.slice(0, 12)}${p.policy_hash === activeHash ? ' · active' : ''}`;
}

export function PolicyCompare({ ctx, a, b }: { ctx: PolicyContext; a: string | null; b: string | null }) {
  const baseId = useId();
  const otherId = useId();
  const toggleId = useId();
  const [showSame, setShowSame] = useState(false);
  const policies = newestFirst(ctx.policies ?? []);
  const activeHash = ctx.active?.policy_hash;
  const fallback = defaultComparison(policies, activeHash);
  const aHash = a ?? fallback?.a ?? null;
  const bHash = b ?? fallback?.b ?? null;
  const pa = policyByHash(policies, aHash);
  const pb = policyByHash(policies, bHash);
  const rows = pa && pb ? diffBodies(pa.body, pb.body) : [];
  const shown = showSame ? rows : differences(rows);
  const s = summarize(rows);

  const go = (nextA: string | null, nextB: string | null) => {
    if (nextA && nextB) ctx.navigate('policy', 'compare', nextA, nextB);
  };

  return (
    <section className="panel" aria-labelledby="policy-view-title">
      <ViewHeading ctx={ctx}>Compare two policies</ViewHeading>
      <div className="panel-body">
        {ctx.policies === undefined ? (
          <p className="muted">Loading…</p>
        ) : policies.length < 2 ? (
          <p className="muted">
            Only {policies.length === 1 ? 'one policy is' : 'no policies are'} published. <a href={routeHref('policy', 'draft')}>Publish a new version</a> to
            compare it with the active one.
          </p>
        ) : (
          <>
            <div className="rk-actions" style={{ marginBottom: 10 }}>
              <label htmlFor={baseId}>Base</label>
              <select id={baseId} className="rk-select" value={aHash ?? ''} onChange={(e) => go(e.target.value, bHash)}>
                {policies.map((p) => (
                  <option key={p.policy_hash} value={p.policy_hash}>
                    {optionLabel(p, activeHash)}
                  </option>
                ))}
              </select>
              <label htmlFor={otherId}>Compared with</label>
              <select id={otherId} className="rk-select" value={bHash ?? ''} onChange={(e) => go(aHash, e.target.value)}>
                {policies.map((p) => (
                  <option key={p.policy_hash} value={p.policy_hash}>
                    {optionLabel(p, activeHash)}
                  </option>
                ))}
              </select>
              <button type="button" className="btn small" onClick={() => go(bHash, aHash)} disabled={!aHash || !bHash}>
                Swap
              </button>
              <label htmlFor={toggleId} style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
                <input id={toggleId} type="checkbox" checked={showSame} onChange={(e) => setShowSame(e.target.checked)} />
                Show unchanged fields
              </label>
            </div>
            {(a && !pa) || (b && !pb) ? (
              <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--warn)', marginBottom: 10 }}>
                A policy in this link is not published: {[a && !pa ? a : null, b && !pb ? b : null].filter(Boolean).join(', ')}.
              </div>
            ) : null}
            {pa && pb ? (
              <>
                <p style={{ margin: '0 0 8px' }} aria-live="polite">
                  <strong>{s.changed}</strong> changed, <strong>{s.added}</strong> added, <strong>{s.removed}</strong> removed, {s.same} unchanged.{' '}
                  <span className="muted">
                    Base <a href={routeHref('policy', pa.policy_hash)}>{pa.name}</a> <ShortId value={pa.policy_hash} head={10} />; compared{' '}
                    <a href={routeHref('policy', pb.policy_hash)}>{pb.name}</a> <ShortId value={pb.policy_hash} head={10} />.
                  </span>
                </p>
                {pa.policy_hash === pb.policy_hash ? <p className="muted">Both sides are the same policy.</p> : null}
              </>
            ) : null}
          </>
        )}
      </div>
      {pa && pb ? (
        <div className="panel-body flush">
          <DiffTable
            rows={shown}
            currencyA={pa.body.currency}
            currencyB={pb.body.currency}
            baseLabel="Base"
            compareLabel="Compared with"
            caption={`Field-by-field comparison of ${pa.name} and ${pb.name}`}
            emptyText="The two bodies are identical."
          />
        </div>
      ) : null}
    </section>
  );
}
