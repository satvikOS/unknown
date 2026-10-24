/**
 * Strategy artifacts (CAP-RES-12, ADR-0017): status = the last record of the append-only transition log;
 * one artifact shows its body, its status history (every transition with principal, evidence and record hash),
 * the evidence with gate verdicts and their reasons, and the API's re-verification of the transition chain.
 * Read-only: transitions and approvals happen only in unknown.research.promotion, with the owner.
 */
import { Fragment } from 'react';
import { nsStrOf } from '../../api/nsStrings';
import { DataTable, type Column } from '../../ui/DataTable';
import { StatusPill } from '../../ui/primitives';
import { NoValue } from '../../ui/unavailable';
import { apiPath, useApi } from '../research-common/api';
import { Hash, JsonTree, KV, Section, StatusTag } from '../research-common/parts';
import { ARTIFACT_STATUSES, artifactTone, gateSummary, type ArtifactDetail, type ArtifactRow, type Envelope, type Evidence } from './model';
import { EnvelopeView, NsTime, ProvenanceLabel, ProvenanceNote } from './parts';
import { ResearchFrame, type ResearchCtx } from './ResearchWorkspace';

export function ArtifactStatus({ status }: { status: string }) {
  return <StatusPill label={status} tone={artifactTone(status)} compact detail={`lifecycle: ${ARTIFACT_STATUSES.join(' → ')}`} />;
}

export function ArtifactList({ ctx }: { ctx: ResearchCtx }) {
  const q = useApi<Envelope<ArtifactRow[]>>('/api/research/artifacts');
  const columns: Column<ArtifactRow>[] = [
    {
      id: 'hash',
      header: 'Artifact',
      sortValue: (a) => a.artifact_hash,
      cell: (a) => (
        <a href={`#/research/artifacts/${a.artifact_hash}`} tabIndex={-1} className="mono" title={a.artifact_hash}>
          {a.artifact_hash.slice(0, 16)}…
        </a>
      ),
    },
    { id: 'strategy', header: 'Strategy', sortValue: (a) => a.strategy_id, cell: (a) => <span className="mono">{a.strategy_id}</span> },
    { id: 'status', header: 'Status (authoritative)', sortValue: (a) => ARTIFACT_STATUSES.indexOf(a.status as never), sortKind: 'number', cell: (a) => <ArtifactStatus status={a.status} /> },
    { id: 'registered', header: 'Registered', sortValue: (a) => a.registered_at_ns, sortKind: 'number', cell: (a) => <NsTime ns={a.registered_at_ns} nsStr={nsStrOf(a, 'registered_at_ns')} /> },
    { id: 'author', header: 'Author', cell: (a) => `${a.author} (${a.author_role})` },
    { id: 'spec', header: 'Spec', cell: (a) => <Hash value={a.spec_hash} /> },
  ];
  return (
    <ResearchFrame ctx={ctx} queries={[q]}>
      <EnvelopeView q={q} what="Artifacts">
        {(rows, prov) => (
          <Section title={`Strategy artifacts · ${rows.length}`} flush note={<ProvenanceNote p={prov} />}>
            <DataTable
              caption="Strategy artifacts with their authoritative status"
              captionHidden
              columns={columns}
              rows={rows}
              rowKey={(a) => a.artifact_hash}
              initialSort={{ columnId: 'registered', direction: 'descending' }}
              onRowActivate={(a) => ctx.navigate('research', 'artifacts', a.artifact_hash)}
              activateLabel="open the artifact"
              emptyText="No artifact registered."
            />
          </Section>
        )}
      </EnvelopeView>
    </ResearchFrame>
  );
}

export function ArtifactView({ ctx, hash }: { ctx: ResearchCtx; hash: string }) {
  const q = useApi<Envelope<ArtifactDetail>>(apiPath('research', 'artifacts', hash));
  return (
    <ResearchFrame ctx={ctx} queries={[q]}>
      <EnvelopeView q={q} what={`Artifact ${hash.slice(0, 12)}`}>
        {(a, prov) => <ArtifactBody a={a} prov={prov} />}
      </EnvelopeView>
    </ResearchFrame>
  );
}

function ArtifactBody({ a, prov }: { a: ArtifactDetail; prov: Envelope<unknown>['provenance'] }) {
  const gates = gateSummary(a.evidence);
  const body = a.body;
  return (
    <>
      <p className="rx-identity">
        <a href="#/research/artifacts">All artifacts</a>
        <span className="rx-name mono" title={a.artifact_hash}>
          {a.strategy_id} · {a.artifact_hash.slice(0, 16)}…
        </span>
        <ProvenanceLabel p={prov} />
        <ArtifactStatus status={a.status} />
      </p>
      <div className="rx-split">
        <div>
          <Section title={`Status history · ${a.history.length}`} flush note={<ProvenanceNote p={prov} />}>
            <div className="rx-table-wrap">
              <table className="rx-table">
                <caption>Append-only transitions of this artifact (seq is the global log position)</caption>
                <thead>
                  <tr>
                    <th scope="col">Seq</th>
                    <th scope="col">Transition</th>
                    <th scope="col">At</th>
                    <th scope="col">Principal</th>
                    <th scope="col">Reason</th>
                    <th scope="col">Evidence</th>
                    <th scope="col">Record · previous</th>
                  </tr>
                </thead>
                <tbody>
                  {a.history.map((t) => (
                    <tr key={t.seq}>
                      <th scope="row" className="num">
                        {t.seq}
                      </th>
                      <td>
                        {t.from_status ? <ArtifactStatus status={t.from_status} /> : <span className="muted">new</span>} → <ArtifactStatus status={t.to_status} />
                      </td>
                      <td>
                        <NsTime ns={t.at_ns} nsStr={nsStrOf(t, 'at_ns')} />
                      </td>
                      <td>
                        {t.principal} ({t.role})
                        {t.approval ? <span className="muted"> · owner approval recorded</span> : null}
                      </td>
                      <td className="wrap">{t.reason ?? <NoValue reason="NOT_REPORTED" />}</td>
                      <td className="mono wrap">{t.evidence_ids.join(', ') || <NoValue reason="NOT_REPORTED" detail="No evidence recorded." />}</td>
                      <td>
                        <Hash value={t.record_hash} head={8} /> · <Hash value={t.prev_record_hash} head={8} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
          <Section
            title="Evidence and gate verdicts"
            actions={
              <span className="rx-flags">
                <StatusPill label={`${gates.passed} PASSED`} tone="ok" compact />
                {gates.failed.length ? <StatusPill label={`${gates.failed.length} FAILED`} tone="bad" compact detail={gates.failed.join(', ')} /> : null}
              </span>
            }
          >
            {a.evidence.length ? (
              <ul className="ops-checks">
                {a.evidence.map((e) => (
                  <li key={e.evidence_id}>
                    <EvidenceLine e={e} />
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">No evidence filed.</p>
            )}
          </Section>
        </div>
        <div>
          <Section title="Transition chain">
            {a.chain_problems.length ? (
              <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)' }}>
                <strong>{a.chain_problems.length} chain problem(s) in the whole transition log</strong>{' '}
                <span>(the API verifies every artifact's records on this read, not only this artifact's; the seq numbers below are global log positions)</span>
                <ul>
                  {a.chain_problems.map((p, i) => (
                    <li key={i}>{typeof p === 'string' ? p : JSON.stringify(p)}</li>
                  ))}
                </ul>
              </div>
            ) : (
              <p>
                <StatusTag status="OK" /> The API re-verified the whole transition log on this read: every record hash matches its content and links to the previous one.
              </p>
            )}
          </Section>
          <Section title="Artifact">
            <KV
              rows={[
                ['Hash', <Hash value={a.artifact_hash} head={24} />],
                ['Strategy', <span className="mono">{a.strategy_id} · spec <a href={`#/research/strategies/${a.spec_hash}`}>{a.spec_hash.slice(0, 12)}…</a></span>],
                ['Registered', <span><NsTime ns={a.registered_at_ns} nsStr={nsStrOf(a, 'registered_at_ns')} /> by {a.author} ({a.author_role})</span>],
                Array.isArray(body.instruments) ? ['Instruments', <span className="mono">{(body.instruments as unknown[]).map(String).join(', ')}</span>] : null,
                Array.isArray(body.allowed_markets) ? ['Allowed markets', (body.allowed_markets as unknown[]).map(String).join(', ')] : null,
                Array.isArray(body.training_interval) ? ['Training interval', <span className="mono">{(body.training_interval as unknown[]).map(String).join(' → ')}</span>] : null,
                typeof body.expires_on === 'string' ? ['Expires on', <span className="mono">{body.expires_on}</span>] : null,
                typeof body.evaluation_manifest_hash === 'string' ? ['Evaluation manifest', <Hash value={body.evaluation_manifest_hash} />] : null,
              ]}
            />
            <details className="rx-details rx-gap">
              <summary>Full artifact body</summary>
              <JsonTree value={body} label="Artifact body" />
            </details>
          </Section>
        </div>
      </div>
    </>
  );
}

function EvidenceLine({ e }: { e: Evidence }) {
  return (
    <>
      <span className="label">
        <StatusPill label={e.passed ? 'PASS' : 'FAIL'} tone={e.passed ? 'ok' : 'bad'} compact /> <span className="mono">{e.kind}</span>
      </span>
      <span>
        {e.gate_reasons.length ? <span className="rx-reason">{e.gate_reasons.join('; ')}</span> : <span className="muted">no gate reason (passed)</span>}
      </span>
      <span className="detail">
        {e.evidence_id} · by {e.created_by} ({e.created_role}) at <NsTime ns={e.created_at_ns} nsStr={nsStrOf(e, 'created_at_ns')} />
        {e.refs.length ? (
          <>
            {' · refs '}
            {e.refs.map((r, i) => (
              <Fragment key={`${r.type}:${r.id}`}>
                {i ? ', ' : ''}
                {r.type === 'trial' ? <a href={`#/research/trials/${encodeURIComponent(r.id)}`}>{r.id}</a> : `${r.type} ${r.id}`}
              </Fragment>
            ))}
          </>
        ) : null}
        <details className="rx-details">
          <summary>Payload ({e.payload_hash.slice(0, 10)}…)</summary>
          <JsonTree value={e.payload} label={`Evidence ${e.kind} payload`} />
        </details>
      </span>
    </>
  );
}
