/** Research job list: every job in the durable job store, newest first, with its state and outcome. */
import { useEffect, useState } from 'react';
import { nsStrOf } from '../../api/nsStrings';
import { routeHref } from '../../state/router';
import { DataTable, type Column } from '../../ui/DataTable';
import { Badge, ShortId, StatusPill, Timestamp } from '../../ui/primitives';
import { QueryError, WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { Segmented } from '../operations/parts';
import { usePluginQuery } from '../plugin-api/hooks';
import type { WorkspaceProps } from '../types';
import { JOB_STATES, NEW_SEGMENT, deliveryTone, needsPolling, parseJobList, shortQuestion, stateTone, type JobSummary } from './model';
import './jobs.css';
import { NoValue } from '../../ui/unavailable';

type Filter = 'ALL' | (typeof JOB_STATES)[number];
const FILTERS: readonly { value: Filter; label: string }[] = [{ value: 'ALL', label: 'All' }, ...JOB_STATES.map((s) => ({ value: s, label: s.charAt(0) + s.slice(1).toLowerCase() }))];

export const ACTIVE_POLL_MS = 3000;
export const IDLE_POLL_MS = 15_000;

function ProposalCell({ j }: { j: JobSummary }) {
  if (j.proposal_id) {
    return (
      <span className="rj-inline">
        <Badge tone="info" title="Recorded by the core as an advisory proposal; it cannot reserve, order or change policy">
          advisory
        </Badge>
        {j.abstain ? <span className="muted">abstained</span> : <span className="muted">hypothesis</span>}
        <ShortId value={j.proposal_id} head={12} />
      </span>
    );
  }
  if (j.abstain !== null) return <span className="muted">{j.abstain ? 'abstained (not delivered)' : 'answer (not delivered)'}</span>;
  return <span className="muted">none</span>;
}

export function JobListView({ navigate }: Pick<WorkspaceProps, 'navigate'>) {
  const [filter, setFilter] = useState<Filter>('ALL');
  const [active, setActive] = useState(false);
  const q = usePluginQuery<unknown>('/api/research/jobs', { state: filter === 'ALL' ? undefined : filter, limit: 200 }, { pollMs: active ? ACTIVE_POLL_MS : IDLE_POLL_MS });
  const rows = q.data === undefined ? null : parseJobList(q.data);
  const nowActive = rows ? needsPolling(rows) : false;
  // The job store is outside the core event log, so stream version bumps do not cover it: poll faster while a job can change.
  useEffect(() => setActive(nowActive), [nowActive]);

  const columns: Column<JobSummary>[] = [
    { id: 'submitted', header: 'Submitted', cell: (j) => <Timestamp ns={j.submitted_ns} nsStr={nsStrOf(j, 'submitted_ns')} />, sortValue: (j) => j.submitted_ns ?? 0, sortKind: 'number' },
    {
      id: 'state',
      header: 'State',
      cell: (j) => (
        <span className="rj-inline">
          <StatusPill label={j.state} tone={stateTone(j.state)} compact />
          {j.cancel_requested && j.state === 'RUNNING' ? <Badge tone="warn">cancel requested</Badge> : null}
        </span>
      ),
      sortValue: (j) => j.state,
    },
    { id: 'question', header: 'Question', cell: (j) => <a href={routeHref('research-jobs', j.job_id)}>{shortQuestion(j.question)}</a>, wrap: true, sortValue: (j) => j.question ?? '' },
    { id: 'role', header: 'Role', cell: (j) => j.role, sortValue: (j) => j.role },
    { id: 'outcome', header: 'Outcome', description: 'Fine-grained runner or worker outcome', cell: (j) => j.outcome || <NoValue reason="NOT_REPORTED" />, sortValue: (j) => j.outcome },
    { id: 'proposal', header: 'Proposal', description: 'Advisory proposal recorded by the core, if any', cell: (j) => <ProposalCell j={j} /> },
    {
      id: 'delivery',
      header: 'Delivery',
      description: 'Proposal delivery to the core (NONE when there is nothing to deliver)',
      cell: (j) => <StatusPill label={j.delivery} tone={deliveryTone(j.delivery)} compact />,
      sortValue: (j) => j.delivery,
    },
    { id: 'finished', header: 'Finished', cell: (j) => <Timestamp ns={j.finished_ns} nsStr={nsStrOf(j, 'finished_ns')} reason="NOT_REPORTED" />, sortValue: (j) => j.finished_ns ?? 0, sortKind: 'number' },
    { id: 'id', header: 'Job id', cell: (j) => <ShortId value={j.job_id} head={14} /> },
  ];

  return (
    <WorkspaceFrame
      title="Research jobs"
      description="Questions for the bounded research agents (ADR-0016): queued here, run by the model worker under the owner's spend caps."
      queries={[q]}
      actions={
        <a className="btn small primary" href={routeHref('research-jobs', NEW_SEGMENT)}>
          Ask a question
        </a>
      }
    >
      <div className="callout rj-gap" role="note" style={{ ['--tone' as string]: 'var(--info)' }}>
        A job runs only in the owner's model worker, which is the only process that holds a provider key. A job may spend money up to its spend cap. Its
        result is an <strong>advisory proposal</strong>: model text that the core records but that cannot reserve, order or change policy. It is not
        advice.
      </div>
      <div className="rj-toolbar">
        <Segmented label="Filter jobs by state" options={FILTERS} value={filter} onChange={setFilter} />
        <span className="spacer" />
        <span className="rj-hint">
          {nowActive ? `A job is queued, running or delivering: refreshing every ${ACTIVE_POLL_MS / 1000} s.` : `Refreshing every ${IDLE_POLL_MS / 1000} s.`}
        </span>
      </div>
      <QueryError what="Research jobs" error={q.error} />
      {q.data !== undefined && rows === null ? (
        <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)' }}>
          The job list response did not have the expected shape (<code>jobs</code> array); nothing is shown rather than a guess.
        </div>
      ) : null}
      {rows ? (
        <DataTable
          caption="Research jobs, newest first"
          columns={columns}
          rows={rows}
          rowKey={(j) => j.job_id}
          initialSort={{ columnId: 'submitted', direction: 'descending' }}
          onRowActivate={(j) => navigate('research-jobs', j.job_id)}
          activateLabel="open"
          emptyText={filter === 'ALL' ? 'No research jobs yet. Ask a question to queue one.' : `No ${filter.toLowerCase()} jobs.`}
        />
      ) : q.status === 'loading' ? (
        <p className="muted">Loading research jobs…</p>
      ) : null}
    </WorkspaceFrame>
  );
}
