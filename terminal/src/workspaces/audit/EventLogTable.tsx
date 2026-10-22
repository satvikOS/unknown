/**
 * Event-log table: semantic <table> in seq order (aria-sort on Seq), roving row focus (arrows,
 * Home/End), Enter/Space expands a row to show its info JSON, ids, times and hashes. Row expansion
 * uses a disclosure button with aria-expanded/aria-controls.
 */
import { useRef, useState, type KeyboardEvent } from 'react';
import { withoutNsStrings } from '../../api/nsStrings';
import type { EventRecord } from '../../api/types';
import { formatTimestampNs } from '../../format/time';
import { Timestamp } from '../../ui/primitives';
import { WorkspaceLink } from '../../ui/WorkspaceFrame';
import type { EventFilters } from './eventLog';

function shortHash(h: string): string {
  return h.length > 14 ? `${h.slice(0, 12)}…` : h;
}

export interface EventLogTableProps {
  rows: readonly EventRecord[];
  expanded: ReadonlySet<number>;
  onToggle: (seq: number) => void;
  onFilter: (f: EventFilters) => void;
  caption: string;
}

const COLS = 8;

export function EventLogTable({ rows, expanded, onToggle, onFilter, caption }: EventLogTableProps) {
  const [active, setActive] = useState(0);
  const bodyRef = useRef<HTMLTableSectionElement>(null);
  const activeIndex = Math.min(active, Math.max(0, rows.length - 1));

  const focusRow = (i: number) => {
    const next = Math.max(0, Math.min(rows.length - 1, i));
    setActive(next);
    const seq = rows[next]?.seq;
    if (seq !== undefined) bodyRef.current?.querySelector<HTMLTableRowElement>(`tr[data-seq="${seq}"]`)?.focus();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTableSectionElement>) => {
    if (!(e.target instanceof HTMLTableRowElement) || !e.target.dataset.seq) return;
    switch (e.key) {
      case 'ArrowDown':
        focusRow(activeIndex + 1);
        break;
      case 'ArrowUp':
        focusRow(activeIndex - 1);
        break;
      case 'Home':
        focusRow(0);
        break;
      case 'End':
        focusRow(rows.length - 1);
        break;
      case 'Enter':
      case ' ': {
        const r = rows[activeIndex];
        if (r) onToggle(r.seq);
        break;
      }
      default:
        return;
    }
    e.preventDefault();
  };

  return (
    <table className="data">
      <caption className="sr-only">
        {caption}. Use arrow keys to move between events and Enter to expand one.
      </caption>
      <thead>
        <tr>
          <th scope="col" className="expander">
            <span className="th-inner">
              <span className="sr-only">Expand</span>
            </span>
          </th>
          <th scope="col" className="right" aria-sort="ascending">
            <span className="th-inner">
              Seq <span aria-hidden="true">▲</span>
            </span>
          </th>
          <th scope="col">
            <span className="th-inner">Recorded (UTC)</span>
          </th>
          <th scope="col">
            <span className="th-inner">Type</span>
          </th>
          <th scope="col">
            <span className="th-inner">Principal</span>
          </th>
          <th scope="col">
            <span className="th-inner">Session / order / instrument</span>
          </th>
          <th scope="col">
            <span className="th-inner">Changed</span>
          </th>
          <th scope="col">
            <span className="th-inner">Chain hash</span>
          </th>
        </tr>
      </thead>
      <tbody ref={bodyRef} onKeyDown={onKeyDown}>
        {rows.length === 0 ? (
          <tr>
            <td colSpan={COLS} className="empty">
              No events in this window.
            </td>
          </tr>
        ) : null}
        {rows.flatMap((r, i) => {
          const open = expanded.has(r.seq);
          const detailId = `ev-detail-${r.seq}`;
          const out = [
            <tr
              key={r.seq}
              data-seq={r.seq}
              tabIndex={i === activeIndex ? 0 : -1}
              data-active={i === activeIndex ? 'true' : undefined}
              onFocus={() => setActive(i)}
            >
              <td className="expander">
                <button
                  type="button"
                  className="expand-btn"
                  tabIndex={-1}
                  aria-expanded={open}
                  aria-controls={open ? detailId : undefined}
                  aria-label={`${open ? 'Collapse' : 'Expand'} event ${r.seq}`}
                  onClick={() => onToggle(r.seq)}
                >
                  {open ? '−' : '+'}
                </button>
              </td>
              <td className="right num">{r.seq}</td>
              <td>
                <Timestamp ns={r.recorded_time_ns} withMs />
              </td>
              <td>
                <code className="mono">{r.type}</code>
              </td>
              <td className="mono">{r.principal_id}</td>
              <td className="wrap">
                <span className="cmd-id">
                  {[r.session_id, r.order_id, r.instrument_id].filter(Boolean).join(' / ') || <span className="muted">account</span>}
                </span>
              </td>
              <td className="wrap">
                <span className="muted">{r.changed_sections.length ? r.changed_sections.join(', ') : 'none'}</span>
              </td>
              <td>
                <span className="hash" title={r.chain_hash}>
                  {shortHash(r.chain_hash)}
                </span>
              </td>
            </tr>,
          ];
          if (open) {
            out.push(
              <tr key={`${r.seq}-detail`} id={detailId} className="detail-row">
                <td colSpan={COLS}>
                  <EventDetail r={r} onFilter={onFilter} />
                </td>
              </tr>,
            );
          }
          return out;
        })}
      </tbody>
    </table>
  );
}

function EventDetail({ r, onFilter }: { r: EventRecord; onFilter: (f: EventFilters) => void }) {
  return (
    <div className="audit-detail">
      <div>
        <h3 className="micro" style={{ margin: '0 0 4px' }}>
          Info (event payload summary)
        </h3>
        <pre className="ops-json">{JSON.stringify(withoutNsStrings(r.info), null, 2)}</pre>
        <p className="ops-hint" style={{ margin: '6px 0 0' }}>
          Changed sections: {r.changed_sections.length ? r.changed_sections.join(', ') : 'none'}
        </p>
      </div>
      <dl className="kv">
        <dt>Event id</dt>
        <dd className="cmd-id">{r.event_id}</dd>
        <dt>Command</dt>
        <dd className="cmd-id">{r.command_id}</dd>
        <dt>Correlation</dt>
        <dd className="cmd-id">{r.correlation_id}</dd>
        <dt>Principal</dt>
        <dd className="mono">{r.principal_id}</dd>
        <dt>Account</dt>
        <dd className="mono">{r.account_key ?? <span className="muted">none</span>}</dd>
        {r.session_id ? (
          <>
            <dt>Session</dt>
            <dd>
              <WorkspaceLink to="sessions" rest={[r.session_id]}>
                <span className="cmd-id">{r.session_id}</span>
              </WorkspaceLink>{' '}
              <button type="button" className="btn small" onClick={() => onFilter({ session: r.session_id ?? undefined })}>
                Only this session
              </button>
            </dd>
          </>
        ) : null}
        {r.order_id ? (
          <>
            <dt>Order</dt>
            <dd>
              <WorkspaceLink to="orders" rest={[r.order_id]}>
                <span className="cmd-id">{r.order_id}</span>
              </WorkspaceLink>{' '}
              <button type="button" className="btn small" onClick={() => onFilter({ order: r.order_id ?? undefined })}>
                Only this order
              </button>
            </dd>
          </>
        ) : null}
        {r.instrument_id ? (
          <>
            <dt>Instrument</dt>
            <dd>
              <WorkspaceLink to="instruments" rest={[r.instrument_id]}>
                <span className="cmd-id">{r.instrument_id}</span>
              </WorkspaceLink>
            </dd>
          </>
        ) : null}
        <dt>Type</dt>
        <dd>
          <code className="mono">{r.type}</code>{' '}
          <button type="button" className="btn small" onClick={() => onFilter({ type: r.type })}>
            Only this type
          </button>
        </dd>
        <dt>Event time</dt>
        <dd>{r.event_time_ns ? <span className="num">{formatTimestampNs(r.event_time_ns, 'utc', true)}</span> : <span className="muted">not stated</span>}</dd>
        <dt>Received</dt>
        <dd className="num">{formatTimestampNs(r.receipt_time_ns, 'utc', true)}</dd>
        <dt>Recorded</dt>
        <dd className="num">{formatTimestampNs(r.recorded_time_ns, 'utc', true)}</dd>
        <dt>Writer epoch</dt>
        <dd className="num">
          {r.writer_epoch} <span className="muted">· schema v{r.schema_version}</span>
        </dd>
        <dt>Payload hash</dt>
        <dd className="hash" style={{ overflowWrap: 'anywhere' }}>
          {r.payload_hash}
        </dd>
        <dt>Chain hash</dt>
        <dd className="hash" style={{ overflowWrap: 'anywhere' }}>
          {r.chain_hash}
        </dd>
        <dt>Integrity</dt>
        <dd className="ops-hint">
          The chain hash covers this event’s columns and the previous event’s chain hash. Verify recomputes every link on the server.
        </dd>
      </dl>
    </div>
  );
}
