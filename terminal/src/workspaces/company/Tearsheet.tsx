/**
 * Tearsheet (CAP-CO-12): GET /api/company/<cik> — typed blocks that fail independently. Each block shows its
 * status (OK, STALE, NOT_LICENSED, NOT_REPORTED, NOT_AVAILABLE, NOT_CONNECTED, PENDING, ERROR), source, as-of
 * time, newest input and age; a block without content shows the API's reason verbatim.
 */
import type { ReactNode } from 'react';
import { formatAge } from '../../format/time';
import { apiPath, useApi } from '../research-common/api';
import { humanize } from '../research-common/format';
import { ErrorNotice, ExtLink, IsoTime, KV, Loading, Section, StatusTag, Unavailable, UnitValue } from '../research-common/parts';
import { IssuerFrame, type IssuerContext } from './CompanyWorkspace';
import { PriceLine } from './Metrics';
import { isUnavailable, priceParams, unitLabel, type Block, type Identity, type StatementResponse, type TearsheetResponse, type UnavailableBody } from './model';
import { FilingsTable, PeerGroupPanel } from './Peers';
import { NoValue } from '../../ui/unavailable';

const KEY_ITEMS = ['revenue', 'gross_profit', 'operating_income', 'net_income', 'cfo', 'free_cash_flow', 'total_assets', 'total_debt', 'cash_and_equivalents', 'equity_total', 'eps_diluted'];

export function TearsheetView({ ctx }: { ctx: IssuerContext }) {
  const q = useApi<TearsheetResponse | UnavailableBody>(apiPath('company', ctx.cik), { as_of: ctx.opts.as_of, ...priceParams(ctx.opts) });
  const t = q.data && !isUnavailable(q.data) ? q.data : null;
  const tab = (to: 'statements' | 'metrics' | 'peers' | 'filings', label: string) => (
    <button type="button" className="btn small" onClick={() => ctx.go({ tab: to })}>
      {label}
    </button>
  );
  return (
    <IssuerFrame ctx={ctx} queries={[q]}>
      <ErrorNotice what="Tearsheet" error={q.error} />
      {q.data && isUnavailable(q.data) ? <Unavailable what="Tearsheet" reason={q.data.reason} /> : null}
      {!q.data && !q.error ? <Loading what="the tearsheet" /> : null}
      {t ? (
        <>
          <p className="rx-hint">
            As of <IsoTime iso={t.as_of} />, generated <IsoTime iso={t.generated_at} />. Block status words: {t.status_vocabulary.join(', ')}.
          </p>
          <div className="rx-blocks">
            <BlockPanel name="Identity" b={t.blocks.identity}>
              {(c) => <IdentityBody id={c} />}
            </BlockPanel>
            <BlockPanel name="Key figures" wide b={t.blocks.statements} actions={tab('statements', 'All statements')}>
              {(c) => ('annual' in c ? <KeyFigures annual={c.annual} ttm={c.ttm} quarterly={c.quarterly} /> : <p className="rx-reason">{c.reason}</p>)}
            </BlockPanel>
            <BlockPanel name="Metrics" wide b={t.blocks.metrics} actions={tab('metrics', 'Show calculations')}>
              {(c) => (
                <>
                  <p className="rx-hint">
                    {c.note} Price input: <PriceLine p={c.price_input} />
                  </p>
                  <div className="rx-table-wrap" style={{ maxHeight: 420 }}>
                    <table className="rx-table">
                      <caption>Metrics with status; reasons are the API's text</caption>
                      <thead>
                        <tr>
                          <th scope="col">Metric</th>
                          <th scope="col" className="num">
                            Value
                          </th>
                          <th scope="col">Status and reason</th>
                        </tr>
                      </thead>
                      <tbody>
                        {c.metrics.map((m) => (
                          <tr key={m.metric}>
                            <th scope="row" title={`${m.formula}: ${m.expression}`}>
                              {humanize(m.metric)}
                            </th>
                            <td className="num">
                              <UnitValue value={m.value} unit={m.unit} missing={m.status} compact />
                            </td>
                            <td className="wrap">
                              <StatusTag status={m.status} /> {m.reason ? <span className="rx-reason">{m.reason}</span> : null}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </BlockPanel>
            <BlockPanel name="Peers" b={t.blocks.peers} actions={tab('peers', 'Comparables')}>
              {(c) => <PeerGroupPanel g={c.group} opts={ctx.opts} />}
            </BlockPanel>
            <BlockPanel name="Filings" wide b={t.blocks.filings} actions={tab('filings', 'All filings')}>
              {(c) => <FilingsTable f={{ ...c, filings: c.filings.slice(0, 10) }} title="Latest filings" />}
            </BlockPanel>
            <BlockPanel name="Business description" b={t.blocks.description}>
              {(c) => (
                <p>
                  <span className="rx-reason">{c.reason}</span>{' '}
                  <ExtLink href={c.link}>{`Annual report ${c.accession}`}</ExtLink>
                </p>
              )}
            </BlockPanel>
            <BlockPanel name="Quote" b={t.blocks.quote}>
              {() => null}
            </BlockPanel>
            <BlockPanel name="Events" b={t.blocks.events}>
              {() => null}
            </BlockPanel>
          </div>
        </>
      ) : null}
    </IssuerFrame>
  );
}

function BlockPanel<T>({ name, b, actions, wide, children }: { name: string; b: Block<T>; actions?: ReactNode; wide?: boolean; children: (content: T) => ReactNode }) {
  const id = `ts-${name.toLowerCase().replace(/[^a-z]+/g, '-')}`;
  return (
    <Section
      id={id}
      className={wide ? 'rx-wide' : undefined}
      title={
        <>
          {name} <StatusTag status={b.status} />
        </>
      }
      actions={b.content !== undefined ? actions : undefined}
    >
      <div className="rx-block-meta">
        <span>source {b.source}</span>
        <span>
          as of <IsoTime iso={b.as_of} />
        </span>
        {b.newest_input ? (
          <span>
            newest input <IsoTime iso={b.newest_input} />
          </span>
        ) : null}
        {b.age_seconds !== undefined ? <span>age {formatAge(b.age_seconds * 1000)}</span> : null}
      </div>
      {b.content !== undefined ? children(b.content) : null}
      {b.reason && b.content === undefined ? <Unavailable what={name} reason={b.reason} status={b.status} /> : null}
    </Section>
  );
}

function IdentityBody({ id }: { id: Identity }) {
  return (
    <>
      <KV
        rows={[
          ['Name at as of', <strong>{id.name_as_of}</strong>],
          id.name !== id.name_as_of ? ['Current name', id.name] : null,
          ['CIK', <span className="mono">{`${id.cik} (${id.entity_id})`}</span>],
          ['SIC', id.sic ? `${id.sic}${id.sic_description ? ` · ${id.sic_description}` : ''}` : <span className="muted">not stated</span>],
          ['Fiscal year end', <span className="mono">{id.fiscal_year_end ?? <NoValue reason="NOT_REPORTED" />}</span>],
          ['Incorporated', id.state_of_incorporation ?? <NoValue reason="NOT_REPORTED" />],
          ['Entity type', <>{id.entity_type ?? <NoValue reason="NOT_REPORTED" />} · filer category {id.filer_category ?? <NoValue reason="NOT_REPORTED" />}</>],
          ['LEI', <span className="mono">{id.lei ?? 'not stated'}</span>],
          ['Listings', id.listings.length ? id.listings.map((l) => `${l.ticker} (${l.exchange}; ${l.validity})`).join(', ') : <span className="muted">none</span>],
          id.former_names.length ? ['Former names', id.former_names.map((f) => `${f.name} (${f.from ?? '…'} → ${f.to ?? '…'})`).join('; ')] : null,
          [
            'Source',
            <span>
              {id.source.attribution} · retrieved <IsoTime iso={id.source.retrieved_at} />
            </span>,
          ],
          [
            'Links',
            <span className="rx-flags">
              {Object.entries(id.links).map(([k, href]) => (
                <ExtLink key={k} href={href}>
                  {humanize(k)}
                </ExtLink>
              ))}
            </span>,
          ],
        ]}
      />
      {id.caveats.length ? (
        <ul className="rx-hint">
          {id.caveats.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      ) : null}
    </>
  );
}

function KeyFigures({ annual, ttm, quarterly }: { annual: StatementResponse; ttm: StatementResponse; quarterly: StatementResponse }) {
  const sources = [
    { s: ttm, col: 0 },
    { s: quarterly, col: 0 },
    { s: annual, col: 0 },
    { s: annual, col: 1 },
  ].filter((x) => x.s.columns[x.col]);
  const row = (s: StatementResponse, item: string) => Object.values(s.statements).flat().find((r) => r.item === item);
  return (
    <div className="rx-table-wrap">
      <table className="rx-table">
        <caption>Key standardized figures ({annual.currency}); select Statements for every line and its sources</caption>
        <thead>
          <tr>
            <th scope="col">Line item</th>
            {sources.map(({ s, col }) => (
              <th key={`${s.basis}${col}`} scope="col" className="num">
                {s.columns[col]!.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {KEY_ITEMS.map((item) => {
            const label = row(annual, item)?.label;
            if (!label) return null;
            return (
              <tr key={item}>
                <th scope="row">{label}</th>
                {sources.map(({ s, col }) => {
                  const c = row(s, item)?.cells[col];
                  return (
                    <td key={`${s.basis}${col}`} className="num" title={c?.value === null ? (c?.detail ?? '') : (c?.method ?? '')}>
                      {!c ? <NoValue reason="NOT_REPORTED" /> : c.value === null ? <span className="rx-missing">{c.missing_reason}</span> : <UnitValue value={c.value} unit={unitLabel(c.unit)} />}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
