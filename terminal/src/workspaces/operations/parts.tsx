/**
 * Small building blocks shared by the Operations, Audit and Instruments workspaces: route-backed
 * tabs (ARIA tablist with arrow keys), a compact command status, a segmented toggle and renderers
 * for typed record details.
 */
import { useEffect, useRef, type KeyboardEvent, type ReactNode } from 'react';
import type { TrackedCommand } from '../../state/commands';
import { useRuntime, useTrackedCommand } from '../../state/react';
import { Money, Qty, ShortId, StatusPill, Timestamp, type Tone } from '../../ui/primitives';
import { WorkspaceLink } from '../../ui/WorkspaceFrame';
import type { DetailField } from './model';
import './ops.css';

// ------------------------------------------------------------------ tabs

export interface TabSpec<T extends string> {
  id: T;
  label: string;
  count?: number | null;
  countTone?: Tone;
}

/**
 * Tabs whose selection is the route (bookmarkable). Arrow keys, Home and End move between tabs and
 * select them (automatic activation); the panel is labelled by the selected tab.
 */
/**
 * Each tab renders its own frame, so selecting a tab remounts the tablist. The tablist that the user
 * operated records itself here and the remounted one takes focus back (otherwise focus drops to body).
 */
let restoreTabFocus: string | null = null;

export function RouteTabs<T extends string>({
  idBase,
  label,
  tabs,
  current,
  onSelect,
}: {
  idBase: string;
  label: string;
  tabs: readonly TabSpec<T>[];
  current: T;
  onSelect: (id: T) => void;
}) {
  const refs = useRef(new Map<T, HTMLButtonElement>());
  useEffect(() => {
    if (restoreTabFocus !== idBase) return;
    restoreTabFocus = null;
    refs.current.get(current)?.focus();
  }, [idBase, current]);
  const select = (id: T) => {
    if (id === current) return;
    restoreTabFocus = idBase;
    onSelect(id);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = tabs.findIndex((t) => t.id === current);
    let next = -1;
    if (e.key === 'ArrowRight') next = (i + 1) % tabs.length;
    else if (e.key === 'ArrowLeft') next = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = tabs.length - 1;
    else return;
    e.preventDefault();
    const t = tabs[next];
    if (!t) return;
    refs.current.get(t.id)?.focus();
    select(t.id);
  };
  return (
    <div className="ops-tabs" role="tablist" aria-label={label} onKeyDown={onKeyDown}>
      {tabs.map((t) => {
        const selected = t.id === current;
        return (
          <button
            key={t.id}
            ref={(el) => {
              if (el) refs.current.set(t.id, el);
              else refs.current.delete(t.id);
            }}
            type="button"
            role="tab"
            id={`${idBase}-tab-${t.id}`}
            aria-selected={selected}
            aria-controls={`${idBase}-panel`}
            tabIndex={selected ? 0 : -1}
            className="ops-tab"
            onClick={() => select(t.id)}
          >
            {t.label}
            {t.count !== undefined && t.count !== null ? (
              <span className={`ops-count tone-${t.countTone ?? 'neutral'}`}>
                {t.count}
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({ idBase, current, children }: { idBase: string; current: string; children: ReactNode }) {
  return (
    <div role="tabpanel" id={`${idBase}-panel`} aria-labelledby={`${idBase}-tab-${current}`}>
      {children}
    </div>
  );
}

// ------------------------------------------------------------------ segmented toggle

export function Segmented<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: readonly { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="ops-segment" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} type="button" aria-pressed={o.value === value} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

// ------------------------------------------------------------------ command status

export function commandTone(status: TrackedCommand['status']): Tone {
  switch (status) {
    case 'applied':
      return 'ok';
    case 'pending':
    case 'sending':
      return 'info';
    case 'cancelled':
      return 'warn';
    default:
      return 'bad';
  }
}

export function commandText(t: TrackedCommand): string {
  switch (t.status) {
    case 'sending':
      return 'sending';
    case 'pending':
      return `recorded; waiting for seq ${t.awaitSeq ?? '?'}`;
    case 'applied':
      return t.awaitSeq !== null ? `applied at seq ${t.awaitSeq}` : 'applied (stored result)';
    case 'rejected':
      return `rejected: ${t.rejection?.code ?? 'unknown code'}`;
    case 'failed':
      return t.outcomeUnknown ? 'outcome unknown' : 'refused by the API';
    case 'cancelled':
      return 'cancelled (not sent)';
  }
}

/** One-line command status for table cells: pill, durable command id, same-id retry when unknown. */
export function InlineCommandStatus({ commandId }: { commandId: string | null | undefined }) {
  const { commands } = useRuntime();
  const t = useTrackedCommand(commandId);
  if (!commandId || !t) return null;
  return (
    <span className="ops-cmd" aria-live="polite">
      <StatusPill label={commandText(t)} tone={commandTone(t.status)} detail={t.error ?? undefined} compact />
      <ShortId value={t.commandId} head={18} />
      {t.status === 'failed' && t.outcomeUnknown ? (
        <button type="button" className="btn small" onClick={() => void commands.retry(t.commandId)}>
          Retry same id
        </button>
      ) : null}
    </span>
  );
}

/** True while a command is in flight (no second send for the same record). */
export function isBusy(t: TrackedCommand | undefined): boolean {
  return t?.status === 'sending' || t?.status === 'pending';
}

// ------------------------------------------------------------------ record details

function FieldValue({ f }: { f: DetailField }) {
  switch (f.kind) {
    case 'money':
      return <Money amount={f.value} currency={f.currency} />;
    case 'quantity':
      return <Qty value={f.value} />;
    case 'decimal':
      return (
        <span className="num" title="The record does not state a currency or unit for this value">
          {f.value} <span className="muted">(unit not stated)</span>
        </span>
      );
    case 'id':
      return f.workspace ? (
        <WorkspaceLink to={f.workspace} rest={[f.value]}>
          <span className="cmd-id">{f.value}</span>
        </WorkspaceLink>
      ) : (
        <span className="cmd-id">{f.value}</span>
      );
    case 'time':
      return <Timestamp ns={f.value} withMs />;
    case 'text':
      return <span style={{ overflowWrap: 'anywhere' }}>{f.value}</span>;
    case 'number':
      return <span className="num">{f.value}</span>;
    case 'bool':
      return <span>{f.value ? 'yes' : 'no'}</span>;
    case 'none':
      return <span className="muted">none</span>;
    case 'json':
      return <pre className="ops-json">{JSON.stringify(f.value, null, 2)}</pre>;
  }
}

export function DetailFields({ fields, empty = 'No detail recorded.' }: { fields: readonly DetailField[]; empty?: string }) {
  if (!fields.length) return <p className="muted">{empty}</p>;
  return (
    <dl className="kv">
      {fields.map((f) => (
        <div key={f.key} style={{ display: 'contents' }}>
          <dt>{f.label}</dt>
          <dd>
            <FieldValue f={f} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** Compact one-line rendering for table cells (nested values summarized as JSON). */
export function InlineFields({ fields, max = 6 }: { fields: readonly DetailField[]; max?: number }) {
  if (!fields.length) return <span className="muted">no detail</span>;
  const shown = fields.slice(0, max);
  return (
    <span className="ops-inline-fields">
      {shown.map((f) => (
        <span key={f.key}>
          <span className="k">{f.label}</span>
          {f.kind === 'json' ? <span className="cmd-id">{JSON.stringify(f.value).slice(0, 120)}</span> : <FieldValue f={f} />}
        </span>
      ))}
      {fields.length > max ? <span className="muted">+{fields.length - max} more</span> : null}
    </span>
  );
}
