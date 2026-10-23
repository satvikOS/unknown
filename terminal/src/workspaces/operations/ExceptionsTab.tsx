/**
 * Operations > Exceptions: open or all cases, case detail and the step-up `resolve_exception`
 * command (acknowledge with a note; for CASH_MISMATCH, record an owner-attributed external cash flow).
 * Nothing changes locally: the case shows RESOLVED only after the stream reports the command's seq.
 */
import { useEffect, useId, useMemo, useState, type ReactNode } from 'react';
import { nsStrOf } from '../../api/nsStrings';
import { defaultCommandId } from '../../api/client';
import type { CommandPayloads, ExceptionCase } from '../../api/types';
import { CommandStatus } from '../../controls/Controls';
import { useQuery, useSendCommand, useTrackedCommand } from '../../state/react';
import { DataTable, type Column } from '../../ui/DataTable';
import { Age, Badge, Money, ShortId, StatusPill, Timestamp } from '../../ui/primitives';
import { severityTone } from '../../ui/tones';
import { QueryError, WorkspaceFrame, WorkspaceLink } from '../../ui/WorkspaceFrame';
import {
  SEVERITY_RANK,
  SHORTFALL_SOURCES,
  allowedActions,
  describeDetail,
  reportedShortfall,
  suggestedCashFlow,
  validateResolve,
  type ResolveAction,
  type ShortfallSource,
} from './model';
import { DetailFields, InlineFields, Segmented, isBusy, TabPanel } from './parts';

type Scope = 'OPEN' | 'ALL';

const ACTION_TEXT: Record<ResolveAction, { label: string; hint: string }> = {
  acknowledge: {
    label: 'Acknowledge',
    hint: 'Closes the case with your note. Nothing is booked; use it when the cause is understood and already accounted for.',
  },
  record_external_cash_flow: {
    label: 'Record external cash flow',
    hint: 'Books a deposit or withdrawal that happened outside the system to the “outside” owner, then requires a fresh clean reconciliation.',
  },
  cover_shortfall: {
    label: 'Cover the shortfall',
    hint: 'Funds the owner that went negative from unallocated, buffer or outside cash you choose (an owner transfer), then closes the case.',
  },
};

const SOURCE_TEXT: Record<ShortfallSource, string> = {
  unallocated: 'unallocated (authorized system capital not in a session)',
  buffer: 'buffer (operational buffer)',
  outside: 'outside (cash not authorized for trading)',
};

function statusTone(c: ExceptionCase) {
  return c.status === 'OPEN' ? (c.blocking ? 'bad' : 'warn') : 'neutral';
}

const columns: Column<ExceptionCase>[] = [
  { id: 'case', header: 'Case', sortValue: (e) => e.case_id, cell: (e) => <ShortId value={e.case_id} head={16} /> },
  { id: 'kind', header: 'Kind', sortValue: (e) => e.kind, cell: (e) => <code className="mono">{e.kind}</code> },
  {
    id: 'sev',
    header: 'Severity',
    sortKind: 'number',
    sortValue: (e) => SEVERITY_RANK[e.severity] ?? -1,
    cell: (e) => <StatusPill label={e.severity} tone={severityTone(e.severity)} compact />,
  },
  {
    id: 'blocking',
    header: 'Blocks entries',
    sortValue: (e) => e.blocking,
    cell: (e) => <StatusPill label={e.blocking ? 'blocking' : 'non-blocking'} tone={e.blocking ? 'bad' : 'neutral'} compact />,
  },
  { id: 'status', header: 'Status', sortValue: (e) => e.status, cell: (e) => <StatusPill label={e.status} tone={statusTone(e)} compact /> },
  {
    id: 'opened',
    header: 'Opened',
    sortKind: 'number',
    sortValue: (e) => e.opened_at_ns,
    cell: (e) => (
      <span>
        <Timestamp ns={e.opened_at_ns} nsStr={nsStrOf(e, 'opened_at_ns')} /> <span className="muted">(<Age sinceNs={e.opened_at_ns} /> ago)</span>
      </span>
    ),
  },
  {
    id: 'ref',
    header: 'Session / order',
    cell: (e) =>
      e.session_id || e.order_id ? (
        <span className="cmd-id">
          {e.session_id ? (
            <WorkspaceLink to="sessions" rest={[e.session_id]}>
              {e.session_id}
            </WorkspaceLink>
          ) : null}
          {e.session_id && e.order_id ? ' / ' : null}
          {e.order_id ? (
            <WorkspaceLink to="orders" rest={[e.order_id]}>
              {e.order_id}
            </WorkspaceLink>
          ) : null}
        </span>
      ) : (
        <span className="muted">account</span>
      ),
  },
  { id: 'detail', header: 'Detail', wrap: true, cell: (e) => <InlineFields fields={describeDetail(e.detail, { kind: e.kind })} max={4} /> },
  {
    id: 'resolution',
    header: 'Resolution',
    wrap: true,
    cell: (e) =>
      e.resolution ? (
        <span>
          {e.resolution.action ?? 'resolved'} by {e.resolution.by} <Timestamp ns={e.resolution.at_ns} nsStr={nsStrOf(e.resolution, 'at_ns')} />
        </span>
      ) : (
        <span className="muted">open</span>
      ),
  },
];

/** Columns kept beside an open case (the detail panel shows the rest). */
const COMPACT_COLUMNS = new Set(['case', 'kind', 'sev', 'blocking', 'status', 'opened']);

export function ExceptionsTab({
  bar,
  caseId,
  navigate,
}: {
  bar: ReactNode;
  caseId: string | null;
  navigate: (workspace: string, ...rest: string[]) => void;
}) {
  const [scope, setScope] = useState<Scope>('OPEN');
  const list = useQuery('/api/exceptions', { status: scope });
  // The detail panel reads the full list so a case stays visible after it is resolved.
  const all = useQuery(caseId ? '/api/exceptions' : null, { status: 'ALL' });
  const [commandByCase, setCommandByCase] = useState<Record<string, string>>({});
  const selected = caseId ? (all.data ?? list.data ?? []).find((c) => c.case_id === caseId) : undefined;
  const rows = list.data ?? [];

  return (
    <WorkspaceFrame
      title="Operations"
      description="Exception cases opened by the core. Resolving one needs a note and a recent passphrase entry; nothing changes until the core records it."
      queries={caseId ? [list, all] : [list]}
    >
      {bar}
      <TabPanel idBase="ops" current="exceptions">
      <QueryError what="Exceptions" error={list.error ?? all.error} />
      <div className="ops-toolbar">
        <Segmented
          label="Which cases"
          value={scope}
          onChange={setScope}
          options={[
            { value: 'OPEN', label: 'Open' },
            { value: 'ALL', label: 'All (open and resolved)' },
          ]}
        />
        <span className="ops-hint">
          {list.data ? `${rows.length} case${rows.length === 1 ? '' : 's'}${scope === 'OPEN' ? ' open' : ''}` : 'Loading…'}
        </span>
      </div>
      {caseId && all.data && !selected ? (
        <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--warn)', marginBottom: 10 }}>
          No exception case <code className="mono">{caseId}</code> for this account. <a href="#/operations/exceptions">Show all cases</a>
        </div>
      ) : null}
      <div className={selected ? 'ops-split' : undefined}>
        <section className="panel">
          <div className="panel-body flush">
            <DataTable
              caption={scope === 'OPEN' ? 'Open exception cases' : 'All exception cases'}
              captionHidden
              columns={selected ? columns.filter((col) => COMPACT_COLUMNS.has(col.id)) : columns}
              rows={rows}
              rowKey={(e) => e.case_id}
              initialSort={{ columnId: 'opened', direction: 'descending' }}
              onRowActivate={(e) => navigate('operations', 'exceptions', e.case_id)}
              activateLabel="open the case"
              emptyText={list.status === 'loading' ? 'Loading…' : scope === 'OPEN' ? 'No open exceptions.' : 'No exception cases recorded.'}
            />
          </div>
        </section>
        {selected ? (
          <CaseDetail
            key={selected.case_id}
            c={selected}
            commandId={commandByCase[selected.case_id] ?? null}
            onCommand={(id) => setCommandByCase((m) => ({ ...m, [selected.case_id]: id }))}
            onClose={() => navigate('operations', 'exceptions')}
          />
        ) : null}
      </div>
      {!selected ? <p className="ops-hint">Press Enter on a focused row (or double-click it) to open the case and resolve it.</p> : null}
      </TabPanel>
    </WorkspaceFrame>
  );
}

function CaseDetail({
  c,
  commandId,
  onCommand,
  onClose,
}: {
  c: ExceptionCase;
  commandId: string | null;
  onCommand: (id: string) => void;
  onClose: () => void;
}) {
  const fields = useMemo(() => describeDetail(c.detail, { kind: c.kind }), [c.detail, c.kind]);
  // Opening a case (Enter on a row, or a link) moves focus to it; the list keeps its place.
  useEffect(() => {
    document.getElementById('case-title')?.focus();
  }, []);
  return (
    <section className="panel" aria-labelledby="case-title">
      <header>
        <h2 id="case-title" tabIndex={-1}>
          <code className="mono">{c.kind}</code>
        </h2>
        <button type="button" className="btn small" onClick={onClose}>
          Close
        </button>
      </header>
      <div className="panel-body" style={{ display: 'grid', gap: 12 }}>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <StatusPill label={c.status} tone={statusTone(c)} />
          <StatusPill label={c.severity} tone={severityTone(c.severity)} compact />
          {c.status === 'OPEN' ? (
            <StatusPill label={c.blocking ? 'blocks new entries' : 'does not block entries'} tone={c.blocking ? 'bad' : 'neutral'} compact />
          ) : (
            <span className="ops-hint">{c.blocking ? 'blocked new entries while open' : 'did not block entries'}</span>
          )}
        </div>
        <dl className="kv">
          <dt>Case</dt>
          <dd className="cmd-id">{c.case_id}</dd>
          <dt>Key</dt>
          <dd className="cmd-id">{c.key}</dd>
          <dt>Opened</dt>
          <dd>
            <Timestamp ns={c.opened_at_ns} nsStr={nsStrOf(c, 'opened_at_ns')} withMs /> <span className="muted">(<Age sinceNs={c.opened_at_ns} /> ago)</span>
          </dd>
          <dt>Session</dt>
          <dd>
            {c.session_id ? (
              <WorkspaceLink to="sessions" rest={[c.session_id]}>
                <span className="cmd-id">{c.session_id}</span>
              </WorkspaceLink>
            ) : (
              <span className="muted">none (account level)</span>
            )}
          </dd>
          <dt>Order</dt>
          <dd>
            {c.order_id ? (
              <WorkspaceLink to="orders" rest={[c.order_id]}>
                <span className="cmd-id">{c.order_id}</span>
              </WorkspaceLink>
            ) : (
              <span className="muted">none</span>
            )}
          </dd>
          {c.owner_hint ? (
            <>
              <dt>Owner hint</dt>
              <dd className="cmd-id">{c.owner_hint}</dd>
            </>
          ) : null}
        </dl>
        <div>
          <h3 className="micro" style={{ margin: '0 0 6px' }}>
            Detail recorded by the core
          </h3>
          <DetailFields fields={fields} />
        </div>
        {c.resolution ? (
          <div className="callout" style={{ ['--tone' as string]: 'var(--ok)' }}>
            <strong>Resolved</strong> by {c.resolution.by} at <Timestamp ns={c.resolution.at_ns} nsStr={nsStrOf(c.resolution, 'at_ns')} withMs />
            {c.resolution.action ? (
              <>
                {' '}
                with <code className="mono">{c.resolution.action}</code>
              </>
            ) : null}
            <div style={{ marginTop: 4, whiteSpace: 'pre-wrap' }}>{c.resolution.note}</div>
          </div>
        ) : null}
        {c.status === 'OPEN' || commandId ? <ResolveForm c={c} commandId={commandId} onCommand={onCommand} /> : null}
        <div>
          <span className="ops-hint">
            <WorkspaceLink to="audit" rest={['events', 'type=ExceptionResolved']}>
              Exception resolutions in the audit log
            </WorkspaceLink>
          </span>
        </div>
      </div>
    </section>
  );
}

function ResolveForm({ c, commandId, onCommand }: { c: ExceptionCase; commandId: string | null; onCommand: (id: string) => void }) {
  const send = useSendCommand();
  const tracked = useTrackedCommand(commandId);
  const busy = isBusy(tracked);
  const suggestion = suggestedCashFlow(c);
  const actions = allowedActions(c);
  const shortfall = reportedShortfall(c);
  const [action, setAction] = useState<ResolveAction>(actions[0] ?? 'acknowledge');
  const [fromOwner, setFromOwner] = useState<ShortfallSource | ''>('');
  const buckets = useQuery(action === 'cover_shortfall' ? '/api/buckets' : null);
  const [note, setNote] = useState('');
  const [currency, setCurrency] = useState(typeof c.detail?.currency === 'string' ? c.detail.currency : '');
  const [amount, setAmount] = useState('');
  const [touched, setTouched] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const id = useId();
  const check = validateResolve(c, { action, note, currency, amount, fromOwner });
  const show = (k: keyof typeof check.errors) => (touched ? check.errors[k] : undefined);
  const cashFlow = action === 'record_external_cash_flow';
  /** Actions that move money get a review step before sending. */
  const movesMoney = cashFlow || action === 'cover_shortfall';

  const submit = async () => {
    setTouched(true);
    if (!check.ok || !check.payload || busy) return;
    if (movesMoney && !reviewing) {
      setReviewing(true);
      return;
    }
    setReviewing(false);
    const payload: CommandPayloads['resolve_exception'] = check.payload;
    // The durable id is chosen first so the form shows it (and stays disabled) from the first moment,
    // including while a passphrase prompt is open.
    const commandId = defaultCommandId('resolve_exception');
    onCommand(commandId);
    await send('resolve_exception', payload, {
      label: cashFlow ? `Record cash flow (${c.kind})` : action === 'cover_shortfall' ? `Cover shortfall (${c.kind})` : `Resolve ${c.kind}`,
      commandId,
    });
  };

  if (c.status !== 'OPEN') {
    return <CommandStatus commandId={commandId} />;
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
      aria-labelledby={`${id}-h`}
      noValidate
    >
      <h3 id={`${id}-h`} className="micro" style={{ margin: '0 0 4px' }}>
        Resolve this case
      </h3>
      <fieldset className="ops-radio" disabled={busy}>
        <legend>Action</legend>
        {actions.map((a) => (
          <label key={a}>
            <input
              type="radio"
              name={`${id}-action`}
              value={a}
              checked={action === a}
              onChange={() => {
                setAction(a);
                setReviewing(false);
              }}
            />
            <span>{ACTION_TEXT[a].label}</span>
            <span className="hint">{ACTION_TEXT[a].hint}</span>
          </label>
        ))}
        {c.kind !== 'CASH_MISMATCH' && c.kind !== 'OVERCOMMITTED' ? (
          <span className="ops-hint">Recording an external cash flow applies to CASH_MISMATCH cases; covering a shortfall to OVERCOMMITTED cases.</span>
        ) : null}
      </fieldset>
      {show('action') ? <div className="form-error">{show('action')}</div> : null}

      {action === 'cover_shortfall' ? (
        <fieldset className="ops-radio" disabled={busy} aria-describedby={show('fromOwner') ? `${id}-src-err` : undefined}>
          <legend>
            Fund from{' '}
            {shortfall ? (
              <span>
                (reported shortfall <Money amount={shortfall.amount} currency={shortfall.currency} />)
              </span>
            ) : null}
          </legend>
          {SHORTFALL_SOURCES.map((src) => {
            const b = (buckets.data ?? []).find((x) => x.owner === src && (!shortfall || x.currency === shortfall.currency));
            return (
              <label key={src}>
                <input
                  type="radio"
                  name={`${id}-src`}
                  value={src}
                  checked={fromOwner === src}
                  onChange={() => {
                    setFromOwner(src);
                    setReviewing(false);
                  }}
                />
                <span>{SOURCE_TEXT[src]}</span>
                <span className="hint">
                  {b ? (
                    <>
                      available <Money amount={b.available_cash} currency={b.currency} kind="available" />
                    </>
                  ) : buckets.data ? (
                    'no balance recorded'
                  ) : (
                    'loading availability…'
                  )}
                </span>
              </label>
            );
          })}
          <div id={`${id}-src-err`} className="form-error">
            {show('fromOwner') ?? ''}
          </div>
        </fieldset>
      ) : null}

      {cashFlow ? (
        <div className="ops-row-2">
          <div className="field">
            <label htmlFor={`${id}-ccy`}>Currency</label>
            <input
              id={`${id}-ccy`}
              className="input"
              value={currency}
              maxLength={3}
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
              aria-invalid={!!show('currency')}
              aria-describedby={show('currency') ? `${id}-ccy-err` : undefined}
              onChange={(e) => {
                setCurrency(e.target.value.toUpperCase());
                setReviewing(false);
              }}
            />
            <div id={`${id}-ccy-err`} className="form-error">
              {show('currency') ?? ''}
            </div>
          </div>
          <div className="field">
            <label htmlFor={`${id}-amt`}>Signed amount (+ arrived, − left the account)</label>
            <input
              id={`${id}-amt`}
              className="input mono"
              value={amount}
              inputMode="decimal"
              autoComplete="off"
              spellCheck={false}
              placeholder="e.g. 12.34 or -5.00"
              disabled={busy}
              aria-invalid={!!show('amount')}
              aria-describedby={`${id}-amt-hint${show('amount') ? ` ${id}-amt-err` : ''}`}
              onChange={(e) => {
                setAmount(e.target.value);
                setReviewing(false);
              }}
            />
            <div id={`${id}-amt-hint`} className="ops-hint">
              Exact decimal; up to 6 decimal places; no thousands separators.
              {suggestion ? (
                <>
                  {' '}
                  The core reported an unexplained difference of <Money amount={suggestion.amount} currency={suggestion.currency} signed />.{' '}
                  <button
                    type="button"
                    className="btn small"
                    disabled={busy}
                    onClick={() => {
                      setAmount(suggestion.amount);
                      setCurrency(suggestion.currency);
                      setReviewing(false);
                    }}
                  >
                    Use this amount
                  </button>
                </>
              ) : null}
            </div>
            <div id={`${id}-amt-err`} className="form-error">
              {show('amount') ?? ''}
            </div>
          </div>
        </div>
      ) : null}

      <div className="field">
        <label htmlFor={`${id}-note`}>
          Note <Badge>required</Badge>
        </label>
        <textarea
          id={`${id}-note`}
          className="input ops-textarea"
          value={note}
          required
          aria-required="true"
          disabled={busy}
          aria-invalid={!!show('note')}
          aria-describedby={show('note') ? `${id}-note-err` : undefined}
          placeholder="What you checked (statement, broker record) and why this resolves the case"
          onChange={(e) => {
            setNote(e.target.value);
            setReviewing(false);
          }}
        />
        <div id={`${id}-note-err`} className="form-error">
          {show('note') ?? ''}
        </div>
      </div>

      <div className="callout" style={{ ['--tone' as string]: movesMoney ? 'var(--warn)' : 'var(--info)' }} aria-live="polite">
        <strong>{reviewing ? 'Review before recording: ' : 'Effect: '}</strong>
        {check.effect}
        {cashFlow && check.ok && check.payload && 'amount' in check.payload && check.payload.amount ? (
          <div style={{ marginTop: 4 }}>
            Amount: <Money amount={check.payload.amount} currency={check.payload.currency ?? currency} signed />
          </div>
        ) : null}
      </div>

      <div className="ops-actions">
        <button type="submit" className="btn primary" disabled={busy}>
          {busy
            ? 'Sending…'
            : cashFlow
              ? reviewing
                ? 'Confirm: record and resolve'
                : 'Review cash flow'
              : action === 'cover_shortfall'
                ? reviewing
                  ? 'Confirm: cover and resolve'
                  : 'Review transfer'
                : 'Resolve case'}
        </button>
        {reviewing ? (
          <button type="button" className="btn" onClick={() => setReviewing(false)}>
            Edit
          </button>
        ) : null}
        <span className="ops-hint">Needs a passphrase entry from the last 15 minutes; you will be asked if it is older.</span>
      </div>
      {commandId ? (
        <div style={{ marginTop: 10 }}>
          <CommandStatus commandId={commandId} />
        </div>
      ) : null}
    </form>
  );
}
