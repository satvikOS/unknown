/** Shared pieces of the Portfolio workspace: lineage, fixture and quote notes, labels, scope select. */
import type { ReactNode } from 'react';
import { useStream, type QueryResult } from '../../state/react';
import { WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { Badge, FixtureBadge, StatusPill, Timestamp } from '../../ui/primitives';
import { usePluginQuery } from '../plugin-api/hooks';
import { arr, isRec, num, recs, str, whatIfStale, type Rec, type ScopeOption } from './model';

export interface FormulaDef {
  id: string;
  definition: string;
  units: string;
  book: string;
}

/** GET /api/portfolio/formulas, keyed by `id@version`. */
export function useFormulas(): Map<string, FormulaDef> {
  const q = usePluginQuery<unknown>('/api/portfolio/formulas');
  const map = new Map<string, FormulaDef>();
  if (isRec(q.data)) {
    for (const f of recs(q.data.formulas)) {
      const id = str(f.id);
      if (id) map.set(id, { id, definition: str(f.definition) ?? '', units: str(f.units) ?? '', book: str(f.book) ?? '' });
    }
  }
  return map;
}

/** Renders `code` spans for text the backend wraps in backticks (for example the snapshot CLI command). */
export function RichReason({ text }: { text: string }) {
  const parts = text.split('`');
  return (
    <>
      {parts.map((p, i) =>
        i % 2 ? (
          <code key={i} className="pf-code">
            {p}
          </code>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </>
  );
}

export function Unavailable({ what, reason }: { what: string; reason: string }) {
  return (
    <div className="callout pf-gap" role="status" style={{ ['--tone' as string]: 'var(--warn)' }}>
      <strong>{what} unavailable.</strong> <RichReason text={reason} />
    </div>
  );
}

/** The FIXTURE label for results computed from fixture quotes or instruments. */
export function FixtureNote({ body }: { body: Rec | null }) {
  if (!body || body.fixture_data !== true) return null;
  return (
    <div className="pf-note">
      <FixtureBadge /> <span>{str(body.fixture_note) ?? 'Computed from FIXTURE data: test data, not market data.'}</span>
    </div>
  );
}

/** When the core was unreachable, analytics still answer, without quotes, and say so. */
export function QuoteSnapshotNote({ body }: { body: Rec | null }) {
  const qs = body && isRec(body.quote_snapshot) ? body.quote_snapshot : null;
  const why = qs ? str(qs.unavailable_reason) : null;
  if (!why) return null;
  return (
    <div className="callout pf-gap" role="status" style={{ ['--tone' as string]: 'var(--warn)' }}>
      <strong>No quote snapshot.</strong> {why}. Positions are unpriced and never valued at zero.
    </div>
  );
}

/** A HYPOTHETICAL or PROPOSAL label exactly as the backend states it. */
export function ResultLabel({ kind, text }: { kind: 'hypothetical' | 'proposal'; text: string | null }) {
  return (
    <div className="callout pf-gap pf-label" role="note" style={{ ['--tone' as string]: 'var(--info)' }}>
      <Badge tone="info">{kind === 'hypothetical' ? 'HYPOTHETICAL' : 'PROPOSAL ONLY'}</Badge>{' '}
      <span>
        {text ??
          (kind === 'hypothetical'
            ? 'Computed on a private copy of the core: it reserves nothing, places nothing and authorizes nothing.'
            : 'Not an order: convert through the what-if service and the independent risk gate.')}
      </span>
    </div>
  );
}

function SourceDates({ dates }: { dates: Rec }) {
  const entries = Object.entries(dates).filter(([, v]) => v !== null && v !== undefined);
  if (!entries.length) return <span className="muted">none</span>;
  return (
    <>
      {entries.map(([k, v], i) => (
        <span key={k}>
          {i ? ' · ' : ''}
          {k}: {k.endsWith('_ns') && typeof v === 'number' ? <Timestamp ns={v} /> : String(v)}
        </span>
      ))}
    </>
  );
}

/** The provenance block every analytics result carries (formula ids, units, source dates, missing, notes). */
export function Lineage({ body, formulas }: { body: Rec | null; formulas: Map<string, FormulaDef> }) {
  const lin = body && isRec(body.lineage) ? body.lineage : null;
  if (!lin) return null;
  const ids = arr(lin.formulas).filter((x): x is string => typeof x === 'string');
  const missing = recs(lin.missing);
  const notes = arr(lin.notes).filter((x): x is string => typeof x === 'string');
  return (
    <details className="pf-lineage">
      <summary>
        Lineage: {ids.length} formula{ids.length === 1 ? '' : 's'}
        {missing.length ? `, ${missing.length} missing input${missing.length === 1 ? '' : 's'}` : ''}
      </summary>
      <dl className="kv">
        <dt>Formulas</dt>
        <dd>
          <ul className="pf-list">
            {ids.map((id) => {
              const f = formulas.get(id);
              return (
                <li key={id}>
                  <code className="pf-code">{id}</code>
                  {f ? <span className="pf-hint"> {f.definition}</span> : null}
                </li>
              );
            })}
          </ul>
        </dd>
        <dt>Units</dt>
        <dd>{str(lin.units) ?? '—'}</dd>
        <dt>Source dates</dt>
        <dd>{isRec(lin.source_dates) ? <SourceDates dates={lin.source_dates} /> : '—'}</dd>
        {missing.length ? (
          <>
            <dt>Missing</dt>
            <dd>
              <ul className="pf-list">
                {missing.map((m, i) => (
                  <li key={i}>
                    {str(m.instrument_id) ?? str(m.date) ?? str(m.day) ?? ''} {str(m.reason) ?? JSON.stringify(m)}
                  </li>
                ))}
              </ul>
            </dd>
          </>
        ) : null}
        {notes.length ? (
          <>
            <dt>Notes</dt>
            <dd>
              <ul className="pf-list">
                {notes.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            </dd>
          </>
        ) : null}
      </dl>
    </details>
  );
}

export function ScopeSelect({ id, value, options, onChange, label = 'Scope' }: { id: string; value: string; options: readonly ScopeOption[]; onChange: (v: string) => void; label?: string }) {
  const known = options.some((o) => o.value === value);
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <select id={id} className="input" value={value} onChange={(e) => onChange(e.target.value)}>
        {known ? null : <option value={value}>{value}</option>}
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

/** The workspace frame for one Portfolio tab: title, the tab bar, provenance of the tab's own queries. */
export function PortfolioFrame({ tab, bar, queries = [], children }: { tab: string; bar: ReactNode; queries?: readonly QueryResult<unknown>[]; children: ReactNode }) {
  return (
    <WorkspaceFrame
      title="Portfolio"
      description="Holdings by owner, P&L, returns, exposure, VaR, stress, what-if and allocation proposals. Read-only: nothing here reserves, orders or changes policy."
      queries={queries}
    >
      {bar}
      <div role="tabpanel" id="pf-panel" aria-labelledby={`pf-tab-${tab}`}>
        {children}
      </div>
    </WorkspaceFrame>
  );
}

export function Toolbar({ children }: { children: ReactNode }) {
  return <div className="pf-toolbar">{children}</div>;
}

/**
 * A result computed on a snapshot of the core (what-if, stress) is stale once the core records any later
 * event (whatif.is_stale). It is not recomputed silently: the owner re-runs it.
 */
export function SnapshotStale({ seq, onRerun, busy }: { seq: number | null; onRerun: () => void; busy?: boolean }) {
  const stream = useStream();
  const stale = whatIfStale(seq, stream.seq);
  if (stale === null) return null;
  if (!stale) {
    return (
      <div className="pf-note">
        <StatusPill label="current" tone="ok" compact /> computed on core seq {seq}, which is still the latest event.
      </div>
    );
  }
  return (
    <div className="callout pf-gap" role="status" style={{ ['--tone' as string]: 'var(--warn)' }}>
      <span className="pf-label">
        <strong>Stale.</strong> Computed on core seq {seq}; the core has recorded events since (now seq {stream.seq}). Re-run for current figures.
        <button type="button" className="btn small" onClick={onRerun} disabled={busy}>
          {busy ? 'Running…' : 'Re-run'}
        </button>
      </span>
    </div>
  );
}

/** "as of" for analytics computed at a wall-clock instant on a core sequence. */
export function AsOf({ body }: { body: Rec | null }) {
  if (!body) return null;
  const at = num(body.as_of_ns);
  const seq = num(body.core_seq) ?? num(body.snapshot_seq);
  return (
    <span className="pf-hint">
      computed <Timestamp ns={at} never="—" />
      {seq !== null ? ` on core seq ${seq}` : ''}
    </span>
  );
}
