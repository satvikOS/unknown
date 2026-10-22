/** Audit > Verify: runs GET /api/audit/verify on request and shows every problem it reports. */
import type { ReactNode } from 'react';
import type { AuditVerify } from '../../api/types';
import { describeError } from '../../api/client';
import { useStream } from '../../state/react';
import { Age, StatusPill } from '../../ui/primitives';
import { WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { TabPanel } from '../operations/parts';

export type VerifyState =
  | { status: 'idle' }
  | { status: 'running'; startedAtMs: number }
  | { status: 'done'; result: AuditVerify; finishedAtMs: number; tookMs: number }
  | { status: 'error'; error: unknown; finishedAtMs: number };

function ProblemList({ title, items, ok }: { title: string; items: readonly string[]; ok: string }) {
  const id = `vp-${title.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
  return (
    <section className="panel" aria-labelledby={id}>
      <header>
        <h2 id={id}>{title}</h2>
        <StatusPill label={items.length ? `${items.length} problem${items.length === 1 ? '' : 's'}` : 'none'} tone={items.length ? 'bad' : 'ok'} compact />
      </header>
      <div className="panel-body">
        {items.length ? (
          <ol className="ops-json" style={{ paddingLeft: 30, maxHeight: 420 }}>
            {items.map((p, i) => (
              <li key={i}>{p}</li>
            ))}
          </ol>
        ) : (
          <p style={{ margin: 0 }}>{ok}</p>
        )}
      </div>
    </section>
  );
}

export function VerifyTab({ bar, state, run }: { bar: ReactNode; state: VerifyState; run: () => void }) {
  const stream = useStream();
  const running = state.status === 'running';
  const since = state.status === 'done' && stream.seq !== null ? Math.max(0, stream.seq - state.result.latest_seq) : null;
  return (
    <WorkspaceFrame title="Audit" description="The hash-chained event log, the double-entry journal and a full verification of both. Read-only.">
      {bar}
      <TabPanel idBase="audit" current="verify">
        <section className="panel" aria-labelledby="verify-h" style={{ marginBottom: 12 }}>
          <header>
            <h2 id="verify-h">Verify the event chain and the ledger</h2>
            {state.status === 'done' ? (
              <StatusPill
                label={state.result.chain_problems.length + state.result.ledger_problems.length ? 'problems found' : 'verified: no problems'}
                tone={state.result.chain_problems.length + state.result.ledger_problems.length ? 'bad' : 'ok'}
              />
            ) : state.status === 'error' ? (
              <StatusPill label="verification failed to run" tone="bad" />
            ) : running ? (
              <StatusPill label="verifying" tone="info" />
            ) : (
              <StatusPill label="not run in this view" tone="neutral" />
            )}
          </header>
          <div className="panel-body" style={{ display: 'grid', gap: 10 }}>
            <p style={{ margin: 0, maxWidth: '100ch' }}>
              The API recomputes, over the whole database: every event’s payload hash, its link to the previous event and its chain hash; every
              journal entry’s balance per currency; materialized balances against the sum of postings; owner transfers per account code;
              inventory cost against open lots; and non-negative available cash and unreserved quantity for system owners not covered by an open
              exception. It reads the database only; nothing is changed.
            </p>
            <div className="ops-actions" style={{ marginTop: 0 }}>
              <button type="button" className="btn primary" onClick={run} disabled={running}>
                {running ? 'Verifying…' : state.status === 'idle' ? 'Verify now' : 'Verify again'}
              </button>
              {state.status === 'done' ? (
                <span className="ops-hint" aria-live="polite">
                  Verified through seq <span className="num">{state.result.latest_seq}</span>, <Age ageMs={0} observedAtMs={state.finishedAtMs} /> ago (took{' '}
                  {Math.round(state.tookMs).toLocaleString('en-US')} ms).
                  {since ? ` ${since.toLocaleString('en-US')} event${since === 1 ? '' : 's'} recorded since: verify again to include them.` : ''}
                </span>
              ) : null}
            </div>
            {state.status === 'error' ? (
              <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)' }}>
                <strong>Verification did not complete.</strong> {describeError(state.error)}
              </div>
            ) : null}
          </div>
        </section>
        {state.status === 'done' ? (
          <div className="grid grid-2">
            <ProblemList title="Event chain" items={state.result.chain_problems} ok="Every payload hash and chain link recomputes exactly." />
            <ProblemList title="Ledger" items={state.result.ledger_problems} ok="Every ledger invariant holds." />
          </div>
        ) : null}
      </TabPanel>
    </WorkspaceFrame>
  );
}
