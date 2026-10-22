/**
 * Audit (book Ch. 22 workspace list; ADR-0013): the event log, the journal and verification.
 * Routes: `#/audit/events/type=…/session=…/order=…/after=N`, `#/audit/journal/owner=…`, `#/audit/verify`.
 */
import { useCallback, useRef, useState } from 'react';
import { useRuntime } from '../../state/react';
import type { WorkspaceProps } from '../types';
import { RouteTabs, type TabSpec } from '../operations/parts';
import { parseAuditRoute, type AuditTab } from './eventLog';
import { EventsTab } from './EventsTab';
import { JournalTab } from './JournalTab';
import { VerifyTab, type VerifyState } from './VerifyTab';

const TABS: TabSpec<AuditTab>[] = [
  { id: 'events', label: 'Event log' },
  { id: 'journal', label: 'Journal' },
  { id: 'verify', label: 'Verify' },
];

export function AuditWorkspace({ route, navigate }: WorkspaceProps) {
  const r = parseAuditRoute(route.rest);
  const { client } = useRuntime();
  const [verify, setVerify] = useState<VerifyState>({ status: 'idle' });
  const runId = useRef(0);

  const run = useCallback(async () => {
    const id = ++runId.current;
    const t0 = performance.now();
    setVerify({ status: 'running', startedAtMs: Date.now() });
    try {
      const result = await client.get('/api/audit/verify');
      if (id === runId.current) setVerify({ status: 'done', result, finishedAtMs: Date.now(), tookMs: performance.now() - t0 });
    } catch (error) {
      if (id === runId.current) setVerify({ status: 'error', error, finishedAtMs: Date.now() });
    }
  }, [client]);

  const tabs = TABS.map((t) =>
    t.id === 'verify' && verify.status === 'done'
      ? {
          ...t,
          count: verify.result.chain_problems.length + verify.result.ledger_problems.length,
          countTone: verify.result.chain_problems.length + verify.result.ledger_problems.length ? ('bad' as const) : ('ok' as const),
        }
      : t,
  );
  const bar = <RouteTabs idBase="audit" label="Audit sections" tabs={tabs} current={r.tab} onSelect={(t) => navigate('audit', t)} />;
  const verifyAction = (
    <button
      type="button"
      className="btn small"
      disabled={verify.status === 'running'}
      onClick={() => {
        void run();
        navigate('audit', 'verify');
      }}
    >
      Verify chain and ledger
    </button>
  );

  switch (r.tab) {
    case 'journal':
      return <JournalTab bar={bar} owner={r.owner} navigate={navigate} actions={verifyAction} />;
    case 'verify':
      return <VerifyTab bar={bar} state={verify} run={() => void run()} />;
    default:
      return <EventsTab bar={bar} route={r} navigate={navigate} verifyAction={verifyAction} />;
  }
}
