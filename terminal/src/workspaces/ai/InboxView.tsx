/** Notes inbox: every advisory note, newest first, filtered by process and issuer; asks for a move explanation. */
import { useState, type FormEvent } from 'react';
import { describeError } from '../../api/client';
import { routeHref } from '../../state/router';
import { DataTable, type Column } from '../../ui/DataTable';
import { useLinkedSecurity } from '../../ui/link';
import { Badge, StatusPill, Timestamp } from '../../ui/primitives';
import { NoValue } from '../../ui/unavailable';
import { QueryError, WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { Segmented } from '../operations/parts';
import { usePluginPost, usePluginQuery } from '../plugin-api/hooks';
import type { WorkspaceProps } from '../types';
import { PROCESSES, PROCESS_LABEL, STATUS_LABEL, inboxRoute, parseNoteList, statusTone, type NoteSummary, type ProcessId } from './model';

type Filter = 'all' | ProcessId;
const FILTERS: readonly { value: Filter; label: string }[] = [
  { value: 'all', label: 'All' },
  ...PROCESSES.map((p) => ({ value: p, label: PROCESS_LABEL[p] })),
];

function ExplainForm() {
  const linked = useLinkedSecurity();
  const post = usePluginPost();
  const [symbol, setSymbol] = useState('');
  const [days, setDays] = useState('5');
  const [msg, setMsg] = useState<{ tone: 'ok' | 'bad'; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const effective = (symbol.trim() || (linked ?? '').split('.')[0] || '').toUpperCase();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || !effective) return;
    setBusy(true);
    setMsg(null);
    try {
      const res = await post<{ run_id?: string }>('/api/ai/explain', {
        symbol: effective,
        window_days: Number(days),
      });
      setMsg({
        tone: 'ok',
        text: `Queued ${res.run_id ?? 'a run'}: the model worker writes the note when it runs (spend capped per request).`,
      });
    } catch (err) {
      setMsg({ tone: 'bad', text: describeError(err) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="ai-explain" onSubmit={submit} aria-label="Explain a price move">
      <label>
        Security
        <input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder={linked ? `${linked} (linked)` : 'e.g. AAPL'} maxLength={10} />
      </label>
      <label>
        Window
        <select value={days} onChange={(e) => setDays(e.target.value)}>
          {['1', '3', '5', '10', '20'].map((d) => (
            <option key={d} value={d}>
              {d} days
            </option>
          ))}
        </select>
      </label>
      <button className="btn small" type="submit" disabled={busy || !effective}>
        Explain move
      </button>
      {msg ? (
        <span className={msg.tone === 'ok' ? 'ai-ok' : 'ai-bad'} role="status">
          {msg.text}
        </span>
      ) : null}
    </form>
  );
}

export function InboxView({ process, symbol, navigate }: { process: ProcessId | null; symbol: string | null } & Pick<WorkspaceProps, 'navigate'>) {
  const [issuer, setIssuer] = useState(symbol ?? '');
  const q = usePluginQuery<unknown>('/api/ai/notes', { process: process ?? undefined, symbol: symbol ?? undefined, limit: 200 }, { pollMs: 15_000 });
  const rows = q.data === undefined ? null : parseNoteList(q.data);
  const columns: Column<NoteSummary>[] = [
    {
      id: 'created',
      header: 'Created',
      cell: (n) => <Timestamp ns={n.created_ns} nsStr={n.created_ns_str} />,
      sortValue: (n) => n.created_ns ?? 0,
      sortKind: 'number',
    },
    {
      id: 'process',
      header: 'Process',
      cell: (n) => PROCESS_LABEL[n.process as ProcessId] ?? n.process,
      sortValue: (n) => n.process,
    },
    {
      id: 'title',
      header: 'Note',
      cell: (n) => <a href={routeHref('ai', 'note', n.note_id)}>{n.title}</a>,
      wrap: true,
      sortValue: (n) => n.title,
    },
    {
      id: 'status',
      header: 'Status',
      cell: (n) => (
        <span className="ai-inline">
          <StatusPill label={STATUS_LABEL[n.status] ?? n.status} tone={statusTone(n.status)} compact />
          {n.label?.kind === 'AI' ? (
            <Badge tone="info" title="Model-written, advisory; every line cites its source or is flagged">
              AI
            </Badge>
          ) : null}
        </span>
      ),
      sortValue: (n) => n.status,
    },
    {
      id: 'lines',
      header: 'Lines',
      description: 'Lines written by the model; flagged lines are not verified against a cited source',
      cell: (n) =>
        n.lines ? (
          <span>
            {n.lines}
            {n.flagged ? <Badge tone="warn">{`${n.flagged} flagged`}</Badge> : null}
          </span>
        ) : (
          <NoValue reason="NOT_APPLICABLE" detail="no model lines" />
        ),
      sortValue: (n) => n.lines,
      sortKind: 'number',
    },
    {
      id: 'symbols',
      header: 'Issuers',
      cell: (n) => (n.symbols.length ? n.symbols.join(' ') : <NoValue reason="NOT_APPLICABLE" />),
      wrap: true,
    },
  ];
  return (
    <WorkspaceFrame
      title="AI notes"
      description="Advisory notes from the AI processes (morning brief, filing summaries, move explainer). Each line cites the stored filing, release or price bars it came from; anything unverified is flagged."
      queries={[q]}
    >
      <div className="callout ai-gap" role="note" style={{ ['--tone' as string]: 'var(--info)' }}>
        AI notes are advisory and labelled AI. They cannot place, change or cancel orders, start sessions or move money, and they are not investment
        advice. Price data is never sent to a model; where no real data exists the note says so.
      </div>
      <div className="ai-toolbar">
        <Segmented
          label="Filter notes by process"
          options={FILTERS}
          value={process ?? 'all'}
          onChange={(v) => navigate('ai', ...inboxRoute(v === 'all' ? null : v, symbol))}
        />
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const s = issuer.trim().toUpperCase();
            navigate('ai', ...inboxRoute(process, /^[A-Z][A-Z0-9.-]{0,9}$/.test(s) ? s : null));
          }}
        >
          <input
            className="ai-issuer"
            aria-label="Filter by ticker"
            value={issuer}
            onChange={(e) => setIssuer(e.target.value)}
            placeholder="Ticker filter"
          />
        </form>
        <span className="spacer" />
        <ExplainForm />
      </div>
      <QueryError what="AI notes" error={q.error} />
      {q.data !== undefined && rows === null ? (
        <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)' }}>
          The notes response did not have the expected shape; nothing is shown rather than a guess.
        </div>
      ) : null}
      {rows ? (
        <DataTable
          caption="AI notes, newest first"
          columns={columns}
          rows={rows}
          rowKey={(n) => n.note_id}
          initialSort={{ columnId: 'created', direction: 'descending' }}
          onRowActivate={(n) => navigate('ai', 'note', n.note_id)}
          activateLabel="open"
          emptyText="No AI notes yet. The model worker writes them when a process is switched on in ai_processes.json."
        />
      ) : q.status === 'loading' ? (
        <p className="muted">Loading AI notes…</p>
      ) : null}
    </WorkspaceFrame>
  );
}
