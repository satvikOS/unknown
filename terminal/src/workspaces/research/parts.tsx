/** Research workspace building blocks: the envelope (available / unavailable), provenance and labels. */
import type { ReactNode } from 'react';
import type { QueryResult } from '../../state/react';
import { Badge, FixtureBadge, Timestamp } from '../../ui/primitives';
import { POPULATE } from '../research-common/format';
import { ErrorNotice, Loading, Unavailable } from '../research-common/parts';
import { provenanceLabel, type Envelope, type Provenance } from './model';

/**
 * Renders an /api/research envelope: the API's error, its unavailable reason, or the data. The only unavailable
 * reason the API gives is a missing registry; it names `python -m unknown.research.study` without the `--out`
 * argument that command requires, so the complete documented command is shown in its place.
 */
export function EnvelopeView<T>({ q, what, children }: { q: QueryResult<Envelope<T>>; what: string; children: (data: T, provenance: Provenance) => ReactNode }) {
  if (q.error) return <ErrorNotice what={what} error={q.error} />;
  if (!q.data) return <Loading what={what.toLowerCase()} />;
  if (!q.data.available || q.data.data === null) {
    return <Unavailable what={what} reason={q.data.unavailable_reason ?? 'the API reported this view as unavailable without a reason'} commands={[POPULATE.study]} />;
  }
  return <>{children(q.data.data, q.data.provenance)}</>;
}

/** FIXTURE (or UNKNOWN ORIGIN) exactly where the API's provenance says so. */
export function ProvenanceLabel({ p }: { p: Provenance | null | undefined }) {
  const l = provenanceLabel(p);
  if (l === 'FIXTURE') return <FixtureBadge title="Provenance: derived from a FIXTURE (synthetic) experiment; not market data and not evidence of profitability" />;
  if (l === 'UNKNOWN ORIGIN') return <Badge tone="warn" title="Provenance could not tell whether this came from fixture or real data">UNKNOWN ORIGIN</Badge>;
  return null;
}

export function ProvenanceNote({ p }: { p: Provenance }) {
  return (
    <span className="rx-prov">
      <ProvenanceLabel p={p} /> {p.source}
      {p.path ? <span className="mono"> · {p.path}</span> : null}
      {p.schema_version ? ` · schema ${p.schema_version}` : ''}
      {p.read_at_ns ? (
        <>
          {' '}
          · read <Timestamp ns={p.read_at_ns} />
        </>
      ) : null}
      {p.note ? ` · ${p.note}` : ''}
      {p.formulas?.length ? <span className="mono"> · formulas {p.formulas.join(', ')}</span> : null}
      {p.units ? ` · units: ${Object.entries(p.units).map(([k, v]) => `${k} = ${v}`).join('; ')}` : ''}
    </span>
  );
}

export function NsTime({ ns }: { ns: number | null | undefined }) {
  return <Timestamp ns={ns ?? null} never="not finished" />;
}
