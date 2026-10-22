/** Small building blocks shared by the sessions list, detail and dialogs. */
import { useRef, type ReactNode } from 'react';
import type { Session } from '../../api/types';
import { routeHref } from '../../state/router';
import { Modal } from '../../ui/Modal';
import { Money, StatusPill, type Tone } from '../../ui/primitives';
import { pctText } from './format';
import type { FeasibilityNote } from './mandateInput';
import { describeSessionBlocker, explainFunding } from './model';

/** Session ids are time-ordered (ses-<uuid v7>): the head distinguishes sessions in a table. */
export function shortSessionId(id: string, head = 18): string {
  return id.length > head + 2 ? `${id.slice(0, head)}…` : id;
}

/** Link to a session's page (always valid: this workspace exists). `short` abbreviates the id; the full id is the tooltip. */
export function SessionLink({ id, short, children }: { id: string; short?: boolean; children?: ReactNode }) {
  return (
    <a href={routeHref('sessions', id)} className="cmd-id" title={id}>
      {children ?? (short ? shortSessionId(id) : id)}
    </a>
  );
}

/** Percent from the core's exact percent string; text, never colour, carries the value. */
export function Pct({ value, title }: { value: string | null; title?: string }) {
  if (value === null) return <span className="muted">unavailable</span>;
  return (
    <span className="num" title={title ?? `${value}%`}>
      {pctText(value)}
    </span>
  );
}

/** Compact funding status for tables: status text, reason and shortfall. */
export function FundingCell({ session }: { session: Pick<Session, 'funding' | 'state' | 'currency'> }) {
  const f = explainFunding(session.funding, session.state);
  return (
    <span>
      <StatusPill label={f.label} tone={f.tone} detail={f.detail} compact />
      {f.shortfall ? (
        <span className="muted">
          {' '}
          short <Money amount={f.shortfall} currency={session.currency} />
        </span>
      ) : null}
    </span>
  );
}

/** Full funding explanation: the exact cause, what resolves it, and the core's own note. */
export function FundingExplanationBlock({ session, extra }: { session: Pick<Session, 'funding' | 'state' | 'currency'>; extra?: ReactNode }) {
  const f = explainFunding(session.funding, session.state);
  return (
    <div className="ses-reason" style={{ ['--tone' as string]: `var(--${f.tone})` }}>
      <span>
        <StatusPill label={f.title} tone={f.tone} />
      </span>
      {f.code ? (
        <span>
          Reason code <code>{f.code}</code>
        </span>
      ) : null}
      <span>{f.detail}</span>
      {f.shortfall ? (
        <span>
          Shortfall: <Money amount={f.shortfall} currency={session.currency} /> <span className="muted">(seed minus unallocated available cash after the operational buffer)</span>
        </span>
      ) : null}
      {f.resolution ? <span className="muted">To resolve: {f.resolution}</span> : null}
      {extra}
      {f.note ? <span className="muted">Core note: “{f.note}”</span> : null}
    </div>
  );
}

export function BlockerList({ codes, label }: { codes: readonly string[]; label: string }) {
  if (!codes.length) return null;
  return (
    <div className="ses-reasons" role="list" aria-label={label}>
      {codes.map((code) => {
        const b = describeSessionBlocker(code);
        return (
          <div className="ses-reason" role="listitem" key={code} style={{ ['--tone' as string]: 'var(--bad)' }}>
            <strong>
              {b.title}
              {b.instrument ? <span className="muted"> · {b.instrument}</span> : null}
            </strong>
            <code>{code}</code>
            <span className="muted">{b.resolution}</span>
          </div>
        );
      })}
    </div>
  );
}

const NOTE_GLYPH: Record<Tone, string> = { ok: '●', warn: '▲', bad: '■', info: '◆', neutral: '○' };

export function NoteList({ notes, label }: { notes: readonly FeasibilityNote[]; label: string }) {
  if (!notes.length) return null;
  return (
    <ul className="ses-notes" aria-label={label}>
      {notes.map((n) => (
        <li key={n.text}>
          <span aria-hidden="true" style={{ color: `var(--${n.tone})` }}>
            {NOTE_GLYPH[n.tone]}
          </span>
          <span>
            <span className="sr-only">{n.tone === 'bad' ? 'Problem: ' : n.tone === 'warn' ? 'Attention: ' : 'Note: '}</span>
            {n.text}
          </span>
        </li>
      ))}
    </ul>
  );
}

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  /** What the command does. */
  effect: string;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
  children?: ReactNode;
}

/** Confirmation for consequential session commands. Cancel has the initial focus. */
export function ConfirmDialog({ open, title, effect, confirmLabel, danger, busy, onConfirm, onClose, children }: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  return (
    <Modal open={open} onClose={onClose} labelledBy="ses-confirm-title" describedBy="ses-confirm-effect" initialFocus={cancelRef} className="ses-dialog">
      <header>
        <h2 id="ses-confirm-title">{title}</h2>
      </header>
      <div className="modal-body">
        <p id="ses-confirm-effect" style={{ marginTop: 0 }}>
          {effect}
        </p>
        {children}
        <p className="muted" style={{ fontSize: 'var(--fs-sm)', marginBottom: 0 }}>
          The command is sent with a durable id. The page changes only when the core’s recorded state arrives; nothing is shown as done in advance.
        </p>
      </div>
      <div className="modal-actions">
        <button ref={cancelRef} type="button" className="btn" onClick={onClose}>
          Cancel
        </button>
        <button type="button" className={`btn ${danger ? 'ses-danger' : 'primary'}`} onClick={onConfirm} disabled={busy}>
          {busy ? 'Sending…' : confirmLabel}
        </button>
      </div>
    </Modal>
  );
}
