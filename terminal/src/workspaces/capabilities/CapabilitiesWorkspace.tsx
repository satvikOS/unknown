/**
 * Capabilities (ADR-0013): the only place unbuilt capabilities appear, with their register status.
 * Reads GET /api/capabilities; when the register is not published it says so. `#/capabilities/<id>`
 * opens one entry with its full record (workflow, acceptance criteria, sources, implementation gaps).
 */
import { useMemo, useState, type ReactNode } from 'react';
import { useQuery } from '../../state/react';
import { routeHref } from '../../state/router';
import { DataTable, type Column } from '../../ui/DataTable';
import { Badge, StatusPill } from '../../ui/primitives';
import { registerStatusTone } from '../../ui/tones';
import { QueryError, WorkspaceFrame } from '../../ui/WorkspaceFrame';
import type { WorkspaceProps } from '../types';
import { countBy, normalizeRegister, type CapabilityRow } from './normalize';
import { NoValue } from '../../ui/unavailable';

const SAFE_URL = /^https?:\/\/[^\s]+$/i;

function humanKey(k: string): string {
  return k.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
}

/** Read-only renderer for register values (strings, lists, nested records). Links only http(s). */
function Value({ value, depth = 0 }: { value: unknown; depth?: number }): ReactNode {
  if (value === null || value === undefined || value === '') return <span className="muted">none</span>;
  if (typeof value === 'string') {
    if (SAFE_URL.test(value)) {
      return (
        <a href={value} target="_blank" rel="noreferrer noopener" className="cmd-id">
          {value}
        </a>
      );
    }
    return <span style={{ whiteSpace: 'pre-wrap' }}>{value}</span>;
  }
  if (typeof value === 'number' || typeof value === 'boolean') return <span className="num">{String(value)}</span>;
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="muted">none</span>;
    return (
      <ul style={{ margin: 0, paddingLeft: 18, display: 'grid', gap: 4 }}>
        {value.map((v, i) => (
          <li key={i}>
            <Value value={v} depth={depth + 1} />
          </li>
        ))}
      </ul>
    );
  }
  if (typeof value === 'object') {
    if (depth > 4) return <span className="cmd-id">{JSON.stringify(value)}</span>;
    return (
      <dl className="kv">
        {Object.entries(value as Record<string, unknown>).map(([k, v]) => (
          <div key={k} style={{ display: 'contents' }}>
            <dt>{humanKey(k)}</dt>
            <dd>
              <Value value={v} depth={depth + 1} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <span>{String(value)}</span>;
}

const DETAIL_ORDER = [
  'user_workflow',
  'behavior',
  'inputs',
  'outputs',
  'acceptance_criteria',
  'degraded_behavior',
  'implementation',
  'reference_scope',
  'lawful_implementation_path',
  'unavailable_or_restricted',
  'rights',
  'datasets',
  'analytics_definitions',
  'dependencies',
  'parity_dimensions',
  'verification_procedure',
  'open_questions',
  'source_verification',
];
const HEADER_KEYS = new Set(['id', 'title', 'area', 'priority', 'slug']);

function CapabilityDetail({ row, onClose }: { row: CapabilityRow; onClose: () => void }) {
  const keys = [
    ...DETAIL_ORDER.filter((k) => k in row.raw),
    ...Object.keys(row.raw).filter((k) => !DETAIL_ORDER.includes(k) && !HEADER_KEYS.has(k)),
  ];
  return (
    <section className="panel" aria-labelledby="cap-detail-title">
      <header>
        <h2 id="cap-detail-title" tabIndex={-1}>
          <span className="cmd-id">{row.id}</span> {row.title}
        </h2>
        <button type="button" className="btn small" onClick={onClose}>
          Back to list
        </button>
      </header>
      <div className="panel-body" style={{ display: 'grid', gap: 14 }}>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          {row.status ? <StatusPill label={row.status} tone={registerStatusTone(row.status)} /> : <StatusPill label="status not stated" tone="neutral" />}
          {row.area ? <Badge>{row.area}</Badge> : null}
          {row.priority ? <Badge tone="info">{row.priority}</Badge> : null}
          {row.verifiedOn ? <span className="muted">sources verified {row.verifiedOn}</span> : null}
        </div>
        {keys.map((k) => (
          <div key={k}>
            <h3 className="micro" style={{ margin: '0 0 4px' }}>
              {humanKey(k)}
            </h3>
            <Value value={row.raw[k]} />
          </div>
        ))}
      </div>
    </section>
  );
}

const columns: Column<CapabilityRow>[] = [
  {
    id: 'id',
    header: 'ID',
    sortValue: (r) => r.id,
    cell: (r) => (
      <a href={routeHref('capabilities', r.id)} className="cmd-id" tabIndex={-1}>
        {r.id}
      </a>
    ),
  },
  { id: 'title', header: 'Capability', wrap: true, sortValue: (r) => r.title, cell: (r) => r.title || <span className="muted">untitled</span> },
  { id: 'area', header: 'Area', sortValue: (r) => r.area, cell: (r) => r.area ?? <NoValue reason="NOT_REPORTED" /> },
  { id: 'priority', header: 'Priority', sortValue: (r) => r.priority, cell: (r) => r.priority ?? <NoValue reason="NOT_REPORTED" /> },
  {
    id: 'status',
    header: 'Implementation status',
    sortValue: (r) => r.status,
    cell: (r) => (r.status ? <StatusPill label={r.status} tone={registerStatusTone(r.status)} compact /> : <span className="muted">not stated</span>),
  },
  {
    id: 'gaps',
    header: 'Gaps',
    numeric: true,
    sortKind: 'number',
    sortValue: (r) => r.gaps.length,
    cell: (r) => <span title={r.gaps.join('\n')}>{r.gaps.length}</span>,
  },
  { id: 'verified', header: 'Sources verified', sortValue: (r) => r.verifiedOn, cell: (r) => r.verifiedOn ?? <span className="muted">undated</span> },
  {
    id: 'products',
    header: 'Reference scope',
    wrap: true,
    cell: (r) => (r.products.length ? <span className="muted">{r.products.join(' · ')}</span> : <NoValue reason="NOT_REPORTED" />),
  },
];

export function CapabilitiesWorkspace({ route, navigate }: WorkspaceProps) {
  // A file in the repository, not core state: revalidated (ETag) when the page opens or refreshes, never per seq.
  const caps = useQuery('/api/capabilities', undefined, { seqDriven: false });
  const [text, setText] = useState('');
  const [area, setArea] = useState('');
  const [status, setStatus] = useState('');
  const [priority, setPriority] = useState('');
  const reg = useMemo(() => normalizeRegister(caps.data), [caps.data]);
  const rows = useMemo(() => {
    const q = text.trim().toLowerCase();
    return reg.rows.filter(
      (r) =>
        (!area || r.area === area) &&
        (!status || (r.status ?? 'unstated') === status) &&
        (!priority || r.priority === priority) &&
        (!q || [r.id, r.title, r.area, r.status, ...r.products].join(' ').toLowerCase().includes(q)),
    );
  }, [reg.rows, text, area, status, priority]);
  const byStatus = useMemo(() => countBy(reg.rows, (r) => r.status), [reg.rows]);
  const areas = useMemo(() => countBy(reg.rows, (r) => r.area).map(([a]) => a).sort(), [reg.rows]);
  const priorities = useMemo(() => countBy(reg.rows, (r) => r.priority).map(([p]) => p).sort(), [reg.rows]);
  const selectedId = route.rest[0];
  const selected = selectedId ? reg.rows.find((r) => r.id === selectedId) : undefined;

  return (
    <WorkspaceFrame
      title="Capabilities"
      description="Capabilities that are not yet built appear only here, with their register status and sources. Navigation shows only implemented workspaces."
      queries={[caps]}
    >
      <QueryError what="The capability register" error={caps.error} />
      {caps.data && !reg.published ? (
        <div className="callout" role="status" style={{ ['--tone' as string]: 'var(--info)' }}>
          <strong>The capability register is not yet published.</strong>
          <div className="muted" style={{ marginTop: 4 }}>
            {reg.note ? `API: “${reg.note}”. ` : ''}When <code className="mono">docs/capabilities/register.json</code> is published, every capability is listed
            here with its status and dated sources.
          </div>
        </div>
      ) : null}
      {selectedId && reg.published && !selected ? (
        <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--warn)' }}>
          No capability with id <code className="mono">{selectedId}</code> in the register.{' '}
          <a href="#/capabilities">Show all</a>
        </div>
      ) : null}
      {selected ? <CapabilityDetail row={selected} onClose={() => navigate('capabilities')} /> : null}
      {reg.published && !selected ? (
        <>
          {reg.description ? (
            <p className="muted" style={{ maxWidth: '90ch', marginTop: 0 }}>
              {reg.description}
            </p>
          ) : null}
          <div className="grid grid-4" style={{ marginBottom: 12 }}>
            <section className="panel" aria-labelledby="cap-status">
              <header>
                <h2 id="cap-status">By implementation status</h2>
                <span className="num muted">{reg.rows.length}</span>
              </header>
              <div className="panel-body">
                <dl className="kv">
                  {byStatus.map(([s, n]) => (
                    <div key={s} style={{ display: 'contents' }}>
                      <dt>
                        <StatusPill label={s} tone={registerStatusTone(s)} compact />
                      </dt>
                      <dd className="num">{n}</dd>
                    </div>
                  ))}
                </dl>
                {reg.statusVocabulary.length ? (
                  <p className="muted" style={{ fontSize: 'var(--fs-sm)', marginBottom: 0 }}>
                    Vocabulary: {reg.statusVocabulary.join(' → ')}
                  </p>
                ) : null}
              </div>
            </section>
            {reg.areas.length ? (
              <section className="panel" aria-labelledby="cap-areas" style={{ gridColumn: 'span 2' }}>
                <header>
                  <h2 id="cap-areas">Source verification by area</h2>
                </header>
                <div className="panel-body flush">
                  <DataTable
                    caption="Source verification by area"
                    captionHidden
                    maxHeight="220px"
                    columns={[
                      { id: 'area', header: 'Area', sortValue: (a) => a.area, cell: (a) => a.area },
                      { id: 'on', header: 'Verified on', sortValue: (a) => a.verifiedOn, cell: (a) => a.verifiedOn ?? <NoValue reason="NOT_REPORTED" /> },
                      { id: 'checked', header: 'URLs checked', numeric: true, sortKind: 'number', sortValue: (a) => a.urlsChecked, cell: (a) => a.urlsChecked ?? <NoValue reason="NOT_REPORTED" /> },
                      { id: 'supported', header: 'URLs supporting', numeric: true, sortKind: 'number', sortValue: (a) => a.urlsSupported, cell: (a) => a.urlsSupported ?? <NoValue reason="NOT_REPORTED" /> },
                    ]}
                    rows={reg.areas}
                    rowKey={(a) => a.area}
                  />
                </div>
              </section>
            ) : null}
          </div>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'end' }}>
            <div className="field" style={{ minWidth: 240 }}>
              <label htmlFor="cap-filter">Search</label>
              <input id="cap-filter" className="input" value={text} onChange={(e) => setText(e.target.value)} placeholder="id, title, product…" />
            </div>
            <div className="field">
              <label htmlFor="cap-area">Area</label>
              <select id="cap-area" className="input" value={area} onChange={(e) => setArea(e.target.value)}>
                <option value="">All areas</option>
                {areas.map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="cap-statusf">Status</label>
              <select id="cap-statusf" className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="">All statuses</option>
                {byStatus.map(([s]) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="cap-prio">Priority</label>
              <select id="cap-prio" className="input" value={priority} onChange={(e) => setPriority(e.target.value)}>
                <option value="">All priorities</option>
                {priorities.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <section className="panel">
            <div className="panel-body flush">
              <DataTable
                caption={`Capability register: ${rows.length} of ${reg.rows.length} entries`}
                columns={columns}
                rows={rows}
                rowKey={(r) => r.key}
                onRowActivate={(r) => navigate('capabilities', r.id)}
                activateLabel="open"
                initialSort={{ columnId: 'id', direction: 'ascending' }}
                emptyText="No entry matches the filters."
              />
            </div>
          </section>
          <p className="muted" style={{ fontSize: 'var(--fs-sm)' }}>
            Press Enter on a focused row (or double-click it) to open the full record.
          </p>
        </>
      ) : null}
    </WorkspaceFrame>
  );
}
