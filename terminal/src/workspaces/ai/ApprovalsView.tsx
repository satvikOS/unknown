/**
 * Approval queue (CAP-AGT-04): the owner approves or rejects the exact content shown. The decision is sent with the
 * content hash displayed here; the server refuses it if the stored content differs, and audits it. A decision only
 * records the owner's review: it never places an order, starts a session, moves money or writes to the core.
 */
import { useState } from 'react';
import { exactJson } from '../../api/nsStrings';
import { describeError } from '../../api/client';
import { Badge, ShortId, StatusPill, Timestamp } from '../../ui/primitives';
import { QueryError, WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { Segmented } from '../operations/parts';
import { usePluginPost, usePluginQuery } from '../plugin-api/hooks';
import { parseApprovals, type Approval } from './model';

const STATES = ['PENDING', 'APPROVED', 'REJECTED', 'EXPIRED'] as const;
type StateFilter = (typeof STATES)[number] | 'ALL';
const tone = (s: string) => (s === 'PENDING' ? 'warn' : s === 'APPROVED' ? 'ok' : s === 'REJECTED' ? 'bad' : 'neutral');

function ApprovalCard({ a, onDone }: { a: Approval; onDone: () => void }) {
  const post = usePluginPost();
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const decide = async (decision: 'approve' | 'reject') => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await post(`/api/ai/approvals/${encodeURIComponent(a.approval_id)}/decide`, { decision, content_hash: a.content_hash, reason });
      onDone();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <article className="ai-card" aria-label={a.title}>
      <header className="ai-inline">
        <StatusPill label={a.state} tone={tone(a.state)} compact />
        <strong>{a.title}</strong>
        <Badge tone="neutral">{a.kind}</Badge>
      </header>
      <p className="muted">
        From {a.source} <ShortId value={a.source_id} head={14} /> · queued <Timestamp ns={a.created_ns} /> · expires <Timestamp ns={a.expires_ns} />
      </p>
      <p>
        Content hash <code className="ai-hash">{a.content_hash}</code>
      </p>
      <details>
        <summary>Exact content under review</summary>
        <pre className="ai-pre">{exactJson(a.content, 2)}</pre>
      </details>
      {a.state === 'PENDING' ? (
        <div className="ai-inline">
          <input
            aria-label="Reason (optional)"
            className="ai-reason"
            value={reason}
            maxLength={500}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Reason (optional)"
          />
          <button type="button" className="btn small primary" disabled={busy} onClick={() => decide('approve')}>
            Approve this content
          </button>
          <button type="button" className="btn small" disabled={busy} onClick={() => decide('reject')}>
            Reject
          </button>
        </div>
      ) : (
        <p className="muted">
          {a.state.toLowerCase()} by {a.decided_by ?? 'the system'} <Timestamp ns={a.decided_ns} />
          {a.decision_reason ? `: ${a.decision_reason}` : ''}
        </p>
      )}
      {error ? (
        <p className="ai-bad" role="alert">
          {error}
        </p>
      ) : null}
    </article>
  );
}

export function ApprovalsView() {
  const [filter, setFilter] = useState<StateFilter>('PENDING');
  const q = usePluginQuery<unknown>('/api/ai/approvals', { state: filter === 'ALL' ? undefined : filter, limit: 200 }, { pollMs: 10_000 });
  const rows = q.data === undefined ? null : parseApprovals(q.data);
  return (
    <WorkspaceFrame
      title="AI approval queue"
      description="Proposals that need the owner's review, each bound to the hash of its exact content."
      queries={[q]}
    >
      <div className="callout ai-gap" role="note" style={{ ['--tone' as string]: 'var(--info)' }}>
        Approving records your review of this exact content (its hash is checked and the decision is audited). It does not place an order, start a
        session, move money or change policy; nothing reads an approval to act.
      </div>
      <div className="ai-toolbar">
        <Segmented
          label="Filter proposals by state"
          options={[
            { value: 'ALL' as StateFilter, label: 'All' },
            ...STATES.map((s) => ({
              value: s as StateFilter,
              label: s.charAt(0) + s.slice(1).toLowerCase(),
            })),
          ]}
          value={filter}
          onChange={setFilter}
        />
      </div>
      <QueryError what="The approval queue" error={q.error} />
      {q.data !== undefined && rows === null ? (
        <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)' }}>
          The approval queue response did not have the expected shape; nothing is shown rather than a guess.
        </div>
      ) : null}
      {rows && rows.length === 0 ? <p className="muted">No {filter === 'ALL' ? '' : `${filter.toLowerCase()} `}proposals.</p> : null}
      <div className="ai-stack">
        {rows?.map((a) => (
          <ApprovalCard key={a.approval_id} a={a} onDone={() => q.refetch?.()} />
        ))}
      </div>
    </WorkspaceFrame>
  );
}
