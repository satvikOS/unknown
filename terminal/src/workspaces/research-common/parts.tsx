/**
 * Building blocks shared by the Company, Macro and Research workspaces: panels, the "unavailable" notice
 * (the API's reason verbatim, plus the command that would populate the source), status tags, unit-aware
 * values, instants and a generic JSON tree for nested records (specs, manifests, evidence payloads).
 */
import { useState, type ReactNode } from 'react';
import { describeError } from '../../api/client';
import { formatDecimal, isDecimal } from '../../format/decimal';
import { Badge, Money, ShortId, StatusPill, Timestamp, type Tone } from '../../ui/primitives';
import { extractCommands, isCurrencyCode, isoToMs, populateCommands, ratioAsPercent } from './format';
import './research.css';

export function Section({
  title,
  actions,
  note,
  children,
  flush,
  id,
  className,
}: {
  title: ReactNode;
  actions?: ReactNode;
  note?: ReactNode;
  children: ReactNode;
  flush?: boolean;
  id?: string;
  className?: string;
}) {
  return (
    <section className={`panel rx-section${className ? ` ${className}` : ''}`} aria-labelledby={id}>
      <header>
        <h2 id={id}>{title}</h2>
        {actions ? <div className="rx-actions">{actions}</div> : null}
      </header>
      <div className={`panel-body${flush ? ' flush' : ''}`}>{children}</div>
      {note ? <div className="panel-note">{note}</div> : null}
    </section>
  );
}

/** Commands named by an unavailable reason, shown as copyable code. */
export function Commands({ commands }: { commands: readonly string[] }) {
  if (!commands.length) return null;
  return (
    <div className="rx-commands">
      <span className="micro">To populate, run</span>
      {commands.map((c) => (
        <code key={c} className="mono rx-command">
          {c}
        </code>
      ))}
    </div>
  );
}

/**
 * The API said a source is unavailable: its reason verbatim, and the command it names. Never placeholder data.
 * `commands` are the documented commands that populate this source (see POPULATE in format.ts); they are listed
 * after the ones the reason names, for reasons that name none or only part of one.
 */
export function Unavailable({
  what,
  reason,
  status = 'UNAVAILABLE',
  commands,
}: {
  what: string;
  reason: string | null | undefined;
  status?: string;
  commands?: readonly string[];
}) {
  const text = reason ?? 'no reason given by the API';
  return (
    <div className="callout rx-unavailable" role="status" style={{ ['--tone' as string]: 'var(--warn)' }}>
      <div>
        <StatusPill label={status} tone={statusTone(status) === 'ok' ? 'warn' : statusTone(status)} compact /> <strong>{what}</strong>
      </div>
      <p className="rx-reason">{text}</p>
      <Commands commands={populateCommands(text, commands)} />
    </div>
  );
}

/** A failed request: the server's message verbatim (and any command it names). */
export function ErrorNotice({ what, error }: { what: string; error: unknown }) {
  if (!error) return null;
  const msg = describeError(error);
  return (
    <div className="callout rx-error" role="alert" style={{ ['--tone' as string]: 'var(--bad)' }}>
      <strong>{what} could not be loaded.</strong> <span className="rx-reason">{msg}</span>
      <Commands commands={extractCommands(msg)} />
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return <p className="muted">Loading {what}…</p>;
}

const STATUS_TONE: Record<string, Tone> = {
  OK: 'ok',
  PASS: 'ok',
  SUCCEEDED: 'ok',
  GRANTED: 'ok',
  INGESTED: 'ok',
  RESTATED: 'info',
  STALE: 'warn',
  PARTIAL: 'warn',
  INCOMPLETE: 'warn',
  MISSING: 'warn',
  UNAVAILABLE: 'warn',
  NOT_AVAILABLE: 'warn',
  NOT_AVAILABLE_AT_AS_OF: 'warn',
  NOT_REPORTED: 'warn',
  NOT_DERIVABLE: 'warn',
  MISSING_INPUT: 'warn',
  NOT_CONNECTED: 'neutral',
  NOT_LICENSED: 'neutral',
  NOT_APPLICABLE: 'neutral',
  NO_PRICE: 'neutral',
  NM: 'neutral',
  SUPPRESSED: 'neutral',
  SEE_PEERS: 'neutral',
  PENDING: 'info',
  RUNNING: 'info',
  ERROR: 'bad',
  FAIL: 'bad',
  FAILED: 'bad',
  REFUSED: 'bad',
  ABORTED: 'bad',
  ARTIFACT_MISSING: 'bad',
  DENIED: 'bad',
};

export function statusTone(status: string | null | undefined): Tone {
  return (status && STATUS_TONE[status]) || 'neutral';
}

/** A status word from the API in text, glyph and colour. */
export function StatusTag({ status, detail }: { status: string | null | undefined; detail?: string }) {
  return <StatusPill label={status ?? 'UNKNOWN'} tone={statusTone(status)} detail={detail} compact />;
}

/** An API instant ("2026-10-22T16:17:52.134721000Z") as a <time>; the raw text stays in the tooltip. */
export function IsoTime({ iso, never = 'none' }: { iso: string | null | undefined; never?: string }) {
  const ms = isoToMs(iso);
  if (ms === null) return iso ? <span className="mono">{iso}</span> : <span className="muted">{never}</span>;
  return (
    <span title={iso ?? undefined}>
      <Timestamp ms={ms} />
    </span>
  );
}

/**
 * A decimal string with its unit. A currency unit renders as Money; "ratio" also shows the exact percentage;
 * anything else is the exact number followed by the unit as written by the API.
 */
export function UnitValue({
  value,
  unit,
  missing = 'no value',
  compact,
}: {
  value: string | null | undefined;
  unit: string | null | undefined;
  missing?: string;
  /** Tables: a ratio shows only its exact percentage (the ratio stays in the tooltip). */
  compact?: boolean;
}) {
  if (value === null || value === undefined) {
    return (
      <span className="muted" title={missing}>
        —<span className="sr-only"> {missing}</span>
      </span>
    );
  }
  if (unit && isCurrencyCode(unit)) return <Money amount={value} currency={unit} />;
  if (!isDecimal(value)) return <span className="mono">{value}</span>;
  const pct = unit === 'ratio' ? ratioAsPercent(value) : null;
  if (compact && pct) {
    return (
      <span className="num" title={`${value} ratio`}>
        {pct}
      </span>
    );
  }
  return (
    <span className="num" title={`${value} ${unit ?? ''}`.trim()}>
      {formatDecimal(value)}
      {unit ? <span className="muted"> {unit}</span> : null}
      {pct ? <span className="muted"> ({pct})</span> : null}
    </span>
  );
}

/** A rate the API writes as a decimal fraction ("0.0825"): the exact value and its exact percentage (8.25 %). */
export function RateValue({ value }: { value: string | null | undefined }) {
  if (value === null || value === undefined) return <span className="muted">—</span>;
  const pct = isDecimal(value) ? ratioAsPercent(value) : null;
  return (
    <span className="num" title={`${value} as a decimal fraction`}>
      {value}
      {pct ? <span className="muted"> ({pct})</span> : null}
    </span>
  );
}

export function Flags({ flags }: { flags: readonly string[] | null | undefined }) {
  if (!flags || !flags.length) return null;
  return (
    <span className="rx-flags">
      {flags.map((f) => (
        <Badge key={f} tone={f.startsWith('stale') || f.startsWith('missing') ? 'warn' : 'neutral'}>
          {f}
        </Badge>
      ))}
    </span>
  );
}

export function KV({ rows }: { rows: readonly (readonly [ReactNode, ReactNode] | null | false)[] }) {
  return (
    <dl className="kv">
      {rows.filter((r): r is readonly [ReactNode, ReactNode] => !!r).map(([k, v], i) => (
        <div key={i} className="rx-kv-row">
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Hash({ value, head = 12 }: { value: string | null | undefined; head?: number }) {
  return <ShortId value={value} head={head} />;
}

/**
 * External link (SEC EDGAR and the like): opens in a new tab, never sends the referrer. Without a link (the API
 * sent null) the text is shown alone; a link that is not https is shown as text, never followed.
 */
export function ExtLink({ href, children }: { href: string | null | undefined; children: ReactNode }) {
  if (!href) return <span>{children}</span>;
  if (!/^https:\/\//.test(href)) return <span className="mono">{href}</span>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}

// ------------------------------------------------------------------ JSON tree

const MAX_ITEMS = 40;

function Scalar({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="muted">null</span>;
  if (typeof value === 'boolean') return <span className="mono">{value ? 'true' : 'false'}</span>;
  if (typeof value === 'number') return <span className="num">{String(value)}</span>;
  return <span className="mono rx-str">{String(value)}</span>;
}

function isScalar(v: unknown): boolean {
  return v === null || typeof v !== 'object';
}

/**
 * Nested API records rendered as nested definition lists. Values are shown exactly as received (decimal strings
 * stay strings); long arrays show the first items and a button for the rest.
 */
export function JsonTree({ value, depth = 0, label }: { value: unknown; depth?: number; label?: string }) {
  const [all, setAll] = useState(false);
  if (isScalar(value)) return <Scalar value={value} />;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="muted">[] (empty)</span>;
    if (value.every(isScalar)) {
      const shown = all ? value : value.slice(0, MAX_ITEMS);
      return (
        <span className="rx-inline-list">
          {shown.map((v, i) => (
            <span key={i}>
              <Scalar value={v} />
              {i < shown.length - 1 ? ', ' : ''}
            </span>
          ))}
          {value.length > shown.length ? (
            <button type="button" className="btn small ghost" onClick={() => setAll(true)}>
              show all {value.length}
            </button>
          ) : null}
        </span>
      );
    }
    const shown = all ? value : value.slice(0, MAX_ITEMS);
    return (
      <ol className="rx-tree-list" start={0} aria-label={label}>
        {shown.map((v, i) => (
          <li key={i}>
            <JsonTree value={v} depth={depth + 1} />
          </li>
        ))}
        {value.length > shown.length ? (
          <li>
            <button type="button" className="btn small" onClick={() => setAll(true)}>
              Show all {value.length} items
            </button>
          </li>
        ) : null}
      </ol>
    );
  }
  const entries = Object.entries(value as Record<string, unknown>);
  if (!entries.length) return <span className="muted">{'{}'} (empty)</span>;
  return (
    <dl className="rx-tree" aria-label={label}>
      {entries.map(([k, v]) =>
        isScalar(v) || (Array.isArray(v) && v.every(isScalar)) ? (
          <div key={k} className="rx-tree-row">
            <dt className="mono">{k}</dt>
            <dd>
              <JsonTree value={v} depth={depth + 1} />
            </dd>
          </div>
        ) : (
          <div key={k} className="rx-tree-row rx-tree-nested">
            <details open={depth < 1}>
              <summary className="mono">
                {k} <span className="muted">{Array.isArray(v) ? `[${v.length}]` : `{${Object.keys(v as object).length}}`}</span>
              </summary>
              <JsonTree value={v} depth={depth + 1} />
            </details>
          </div>
        ),
      )}
    </dl>
  );
}
