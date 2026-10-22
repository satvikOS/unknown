/**
 * DCF (CAP-CO-11; book eq. 17.1): GET /api/company/<cik>/dcf with owner assumptions. The result is shown as its
 * recipe: every input with its source (owner input, Treasury observation, FCFF TTM with its formula), the WACC
 * build-up, the projections and terminal value, the equity bridge at the latest balance sheet, the WACC x g
 * sensitivity grid and the scenarios. A guard that refuses (WACC <= g, currency mismatch, financial issuer,
 * missing inputs) is shown as REFUSED with the API's reason; nothing is computed in the browser.
 */
import { useState, type FormEvent, type ReactNode } from 'react';
import { formatDecimal } from '../../format/decimal';
import { Money } from '../../ui/primitives';
import { apiPath, useApi } from '../research-common/api';
import { RangeBars } from '../research-common/charts';
import { humanize } from '../research-common/format';
import { ErrorNotice, Flags, IsoTime, JsonTree, KV, Loading, Section, StatusTag, Unavailable, UnitValue } from '../research-common/parts';
import { IssuerFrame, TextField, type IssuerContext } from './CompanyWorkspace';
import { Bridge, PriceLine } from './Metrics';
import {
  EMPTY_DCF,
  TREASURY_TENORS,
  dcfParams,
  isRefused,
  isUnavailable,
  priceParams,
  sameDecimal,
  type DcfForm,
  type DcfInput,
  type DcfResponse,
  type DcfResult,
  type MetricRow,
  type PriceInputJson,
} from './model';

export function DcfView({ ctx }: { ctx: IssuerContext }) {
  const [form, setForm] = useState<DcfForm>(EMPTY_DCF);
  const [submitted, setSubmitted] = useState<Record<string, string> | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const q = useApi<DcfResponse>(submitted ? apiPath('company', ctx.cik, 'dcf') : null, submitted ? { as_of: ctx.opts.as_of, ...priceParams(ctx.opts), ...submitted } : undefined);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const r = dcfParams(form);
    if (!r.ok) {
      setErrors(r.errors);
      return;
    }
    setErrors([]);
    setSubmitted(r.params);
  };
  const set = <K extends keyof DcfForm>(k: K) => (v: DcfForm[K]) => setForm((f) => ({ ...f, [k]: v }));

  return (
    <IssuerFrame ctx={ctx} queries={submitted ? [q] : []}>
      <div className="rx-split">
        <div>
          {!submitted ? (
            <Section title="DCF">
              <p className="muted">
                Enter the owner assumptions and run the model. The API reads the as-of fundamentals (FCFF TTM, the latest balance sheet, diluted shares) and the Treasury
                curve itself; nothing is forecast by this system.
              </p>
            </Section>
          ) : null}
          <ErrorNotice what="DCF" error={q.error} />
          {submitted && !q.data && !q.error ? <Loading what="the DCF" /> : null}
          {q.data && isUnavailable(q.data) ? <Unavailable what="DCF" reason={q.data.reason} /> : null}
          {q.data && isRefused(q.data) ? (
            <div className="callout" role="status" style={{ ['--tone' as string]: 'var(--bad)' }}>
              <StatusTag status="REFUSED" /> <strong>The model refused these inputs.</strong>
              <p className="rx-reason">{q.data.reason}</p>
              <p className="rx-hint">
                Model <span className="mono">{q.data.model}</span>, as of <IsoTime iso={q.data.as_of} />.
              </p>
            </div>
          ) : null}
          {q.data && !isUnavailable(q.data) && !isRefused(q.data) ? <DcfResultView d={q.data} /> : null}
        </div>
        <div>
          <Section title="Assumptions">
            <DcfFormView form={form} set={set} onSubmit={submit} errors={errors} />
          </Section>
        </div>
      </div>
    </IssuerFrame>
  );
}

function Radio<T extends string>({ name, value, current, onChange, children }: { name: string; value: T; current: T; onChange: (v: T) => void; children: ReactNode }) {
  return (
    <label>
      <input type="radio" name={name} value={value} checked={current === value} onChange={() => onChange(value)} />
      {children}
    </label>
  );
}

function DcfFormView({
  form,
  set,
  onSubmit,
  errors,
}: {
  form: DcfForm;
  set: <K extends keyof DcfForm>(k: K) => (v: DcfForm[K]) => void;
  onSubmit: (e: FormEvent) => void;
  errors: string[];
}) {
  return (
    <form className="rx-form" onSubmit={onSubmit} aria-label="DCF assumptions" noValidate>
      <TextField id="dcf-g" label="Terminal growth g (decimal, required)" value={form.g} onChange={set('g')} placeholder="0.025" />
      <fieldset>
        <legend>Cash flows (FCFF, reporting currency)</legend>
        <div className="rx-radio-row" role="radiogroup" aria-label="Cash flow input">
          <Radio name="dcf-flows" value="explicit" current={form.flows} onChange={set('flows')}>
            Explicit forecast
          </Radio>
          <Radio name="dcf-flows" value="growth" current={form.flows} onChange={set('flows')}>
            Grow FCFF TTM at a constant rate
          </Radio>
        </div>
        {form.flows === 'explicit' ? (
          <TextField id="dcf-fcff" label="FCFF per year (comma-separated)" value={form.fcff} onChange={set('fcff')} placeholder="100, 110, 120" />
        ) : (
          <>
            <TextField id="dcf-fcff-growth" label="FCFF growth (decimal)" value={form.fcff_growth} onChange={set('fcff_growth')} placeholder="0.03" />
            <TextField id="dcf-years" label="Years (1-30)" value={form.years} onChange={set('years')} placeholder="5" />
          </>
        )}
      </fieldset>
      <fieldset>
        <legend>Discount rate</legend>
        <div className="rx-radio-row" role="radiogroup" aria-label="Discount rate input">
          <Radio name="dcf-rate" value="capm" current={form.rate} onChange={set('rate')}>
            Build up WACC (CAPM)
          </Radio>
          <Radio name="dcf-rate" value="wacc" current={form.rate} onChange={set('rate')}>
            Give WACC
          </Radio>
        </div>
        {form.rate === 'wacc' ? (
          <TextField id="dcf-wacc" label="WACC (decimal)" value={form.wacc} onChange={set('wacc')} placeholder="0.09" />
        ) : (
          <>
            <div className="rx-radio-row" role="radiogroup" aria-label="Risk-free rate">
              <Radio name="dcf-rf" value="treasury" current={form.rf_mode} onChange={set('rf_mode')}>
                Risk-free from the Treasury par curve
              </Radio>
              <Radio name="dcf-rf" value="explicit" current={form.rf_mode} onChange={set('rf_mode')}>
                Risk-free given
              </Radio>
            </div>
            {form.rf_mode === 'treasury' ? (
              <>
                <div className="field">
                  <label htmlFor="dcf-rf-tenor">Treasury tenor</label>
                  <select id="dcf-rf-tenor" className="input" value={form.rf_tenor} onChange={(e) => set('rf_tenor')(e.target.value)}>
                    {TREASURY_TENORS.map((t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ))}
                  </select>
                </div>
                <TextField id="dcf-rf-date" label="Curve date (optional)" value={form.rf_date} onChange={set('rf_date')} placeholder="latest at as of" />
              </>
            ) : (
              <>
                <TextField id="dcf-rf" label="Risk-free rate (decimal)" value={form.rf} onChange={set('rf')} placeholder="0.042" />
                <TextField id="dcf-rate-ccy" label="Rate currency" value={form.rate_currency} onChange={set('rate_currency')} placeholder="reporting currency" />
              </>
            )}
            <TextField id="dcf-beta" label="Beta" value={form.beta} onChange={set('beta')} placeholder="1.0" />
            <TextField id="dcf-erp" label="Equity risk premium" value={form.erp} onChange={set('erp')} placeholder="0.05" />
            <TextField id="dcf-crp" label="Country risk premium (optional)" value={form.crp} onChange={set('crp')} placeholder="0" />
            <TextField id="dcf-kd" label="Pre-tax cost of debt" value={form.kd} onChange={set('kd')} placeholder="0.05" />
            <TextField id="dcf-tax" label="Tax rate (optional: effective TTM)" value={form.tax} onChange={set('tax')} placeholder="0.21" />
            <TextField id="dcf-we" label="Equity weight (optional with a price)" value={form.we} onChange={set('we')} placeholder="0.8" />
            <TextField id="dcf-wd" label="Debt weight (optional with a price)" value={form.wd} onChange={set('wd')} placeholder="0.2" />
          </>
        )}
      </fieldset>
      <fieldset>
        <legend>Sensitivity steps and overrides</legend>
        <TextField id="dcf-step-wacc" label="WACC step" value={form.step_wacc} onChange={set('step_wacc')} placeholder="0.01" />
        <TextField id="dcf-step-g" label="g step" value={form.step_g} onChange={set('step_g')} placeholder="0.005" />
        <label className="rx-check">
          <input type="checkbox" checked={form.allow_financial} onChange={(e) => set('allow_financial')(e.target.checked)} />
          Apply to a financial-sector issuer (owner override)
        </label>
        <label className="rx-check">
          <input type="checkbox" checked={form.allow_currency_mismatch} onChange={(e) => set('allow_currency_mismatch')(e.target.checked)} />
          Allow a discount rate in another currency (owner override)
        </label>
      </fieldset>
      {errors.length ? (
        <ul className="form-error" role="alert" style={{ gridColumn: '1 / -1' }}>
          {errors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      ) : null}
      <div className="rx-form-actions">
        <button type="submit" className="btn primary">
          Run DCF
        </button>
        <span className="rx-hint">The price input above (if any) is passed through for market-value weights and the price marker.</span>
      </div>
    </form>
  );
}

function inputValue(v: DcfInput): ReactNode {
  if (v.values) return <span className="mono">{v.values.map((x) => formatDecimal(x)).join(', ')}</span>;
  if (v.we !== undefined || v.wd !== undefined) return <span className="mono">{`we ${v.we ?? '—'} · wd ${v.wd ?? '—'}`}</span>;
  if (v.value !== undefined) return <span className="num">{v.value}</span>;
  return <span className="muted">—</span>;
}

function DcfResultView({ d }: { d: DcfResult }) {
  const ccy = d.currency;
  const v = d.valuation;
  const perShareUnit = `${ccy} per share`;
  const ff = d.football_field;
  const rangeRows = (['dcf_scenarios', 'dcf_sensitivity'] as const)
    .map((k) => ({ k, r: ff[k] as { min?: string; max?: string } | undefined }))
    .filter((x): x is { k: 'dcf_scenarios' | 'dcf_sensitivity'; r: { min: string; max: string } } => !!x.r?.min && !!x.r.max)
    .map(({ k, r }) => ({ label: k === 'dcf_scenarios' ? 'DCF scenarios' : 'WACC x g grid', min: Number(r.min), max: Number(r.max), minText: r.min, maxText: r.max }));
  const price = ff.current_price as PriceInputJson | undefined;
  return (
    <>
      <Section
        title={`DCF result · ${d.model}`}
        note={
          <>
            {d.disclaimer} Arithmetic: {d.arithmetic}.
          </>
        }
      >
        <div className="rx-tiles">
          <Tile label="Enterprise value">
            <Money amount={v.enterprise_value} currency={ccy} />
          </Tile>
          <Tile label="Equity value">
            <Money amount={d.equity_bridge.equity_value} currency={ccy} />
          </Tile>
          <Tile label="Per share (diluted)">{d.equity_bridge.per_share ? <Money amount={d.equity_bridge.per_share} currency={ccy} /> : <span className="muted">not computed</span>}</Tile>
          <Tile label="WACC">
            <span>{v.wacc}</span>
          </Tile>
          <Tile label="Terminal growth g">
            <span>{v.g}</span>
          </Tile>
          <Tile label="Terminal share of EV">
            <span>{v.terminal_share_of_ev}</span>
          </Tile>
        </div>
        <p className="rx-hint">
          As of <IsoTime iso={d.as_of} />, balance sheet at <span className="mono">{d.balance_sheet_date}</span>, values in {ccy}. Rates are decimals (0.05 = 5 %).
          {d.equity_bridge.status !== 'OK' ? (
            <>
              {' '}
              Equity bridge <StatusTag status={d.equity_bridge.status} />
              {d.equity_bridge.missing_components?.length ? ` missing: ${d.equity_bridge.missing_components.join(', ')} (excluded, not zero).` : null}
            </>
          ) : null}
        </p>
        {d.overrides.length ? (
          <div className="callout" style={{ ['--tone' as string]: 'var(--warn)' }}>
            <strong>Owner overrides applied:</strong> {d.overrides.join('; ')}
          </div>
        ) : null}
      </Section>

      <Section title="Recipe: inputs and their sources">
        <div className="rx-table-wrap">
          <table className="rx-table">
            <caption>Every input with its source; Treasury inputs carry their observation id and availability time</caption>
            <thead>
              <tr>
                <th scope="col">Input</th>
                <th scope="col">Value</th>
                <th scope="col">Source and lineage</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(d.inputs).map(([k, val]) =>
                typeof val === 'string' ? (
                  <tr key={k}>
                    <th scope="row">{humanize(k)}</th>
                    <td colSpan={2} className="wrap rx-reason">
                      {val}
                    </td>
                  </tr>
                ) : (
                  <tr key={k}>
                    <th scope="row">{k}</th>
                    <td className="wrap">
                      {inputValue(val)}
                      {val.unit ? <span className="muted"> {val.unit}</span> : null}
                      {val.currency ? <span className="muted"> ({val.currency})</span> : null}
                    </td>
                    <td className="wrap">
                      {val.source ?? <span className="muted">no source stated</span>}
                      {val.obs_id !== undefined ? <span className="muted"> · obs {val.obs_id}</span> : null}
                      {val.available_time ? (
                        <span className="muted">
                          {' '}
                          · available <IsoTime iso={val.available_time} />
                        </span>
                      ) : null}
                      {val.fallback ? <span className="muted"> · {val.fallback}</span> : null}
                      {val.convention_note ? <span className="muted"> · {val.convention_note}</span> : null}
                      {val.base ? (
                        <details className="rx-details">
                          <summary>FCFF TTM base ({val.base.formula})</summary>
                          <MetricSummary m={val.base} />
                        </details>
                      ) : null}
                    </td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </div>
        {d.wacc_build_up ? (
          <>
            <h3 className="rx-h3">WACC build-up · {d.wacc_build_up.formula}</h3>
            <p className="mono">{d.wacc_build_up.expression}</p>
            <KV
              rows={[
                ...Object.entries(d.wacc_build_up.inputs).map(([k, val]) => [
                  k,
                  <span key={k}>
                    <span className="num">{val}</span> <span className="muted">· {d.wacc_build_up?.sources[k] ?? d.wacc_build_up?.sources[k === 'we' || k === 'wd' ? 'weights' : k] ?? 'source not stated'}</span>
                  </span>,
                ] as const),
                ['cost of equity', <span className="num">{d.wacc_build_up.cost_of_equity}</span>],
                ['after-tax cost of debt', <span className="num">{d.wacc_build_up.after_tax_cost_of_debt}</span>],
                ['WACC', <strong className="num">{d.wacc_build_up.wacc}</strong>],
              ]}
            />
          </>
        ) : null}
      </Section>

      <Section title={`Valuation · ${v.formula}`} flush>
        <div className="rx-table-wrap">
          <table className="rx-table">
            <caption>
              Projected FCFF discounted at WACC {v.wacc}; terminal value by Gordon growth at g {v.g} after year {v.T} (values in {ccy})
            </caption>
            <thead>
              <tr>
                <th scope="col">Year t</th>
                <th scope="col" className="num">
                  FCFF
                </th>
                <th scope="col" className="num">
                  Discount factor
                </th>
                <th scope="col" className="num">
                  Present value
                </th>
              </tr>
            </thead>
            <tbody>
              {v.projections.map((p) => (
                <tr key={p.t}>
                  <th scope="row">{p.t}</th>
                  <td className="num">
                    <Money amount={p.fcff} currency={ccy} />
                  </td>
                  <td className="num">{p.discount_factor}</td>
                  <td className="num">
                    <Money amount={p.pv} currency={ccy} />
                  </td>
                </tr>
              ))}
              <SumRow label="PV of the forecast" amount={v.pv_forecast} ccy={ccy} />
              <SumRow label={`FCFF in year ${v.T + 1}`} amount={v.fcff_next} ccy={ccy} />
              <SumRow label="Terminal value (at year T)" amount={v.terminal_value} ccy={ccy} />
              <SumRow label="PV of the terminal value" amount={v.pv_terminal_value} ccy={ccy} />
              <SumRow label="Enterprise value" amount={v.enterprise_value} ccy={ccy} strong />
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Equity bridge" note={d.equity_bridge.expression}>
        <Bridge items={d.equity_bridge.bridge.map((b) => ({ ...b, sign: b.sign > 0 ? 1 : -1 }))} currency={ccy} />
        <KV
          rows={[
            ['Equity value', <Money amount={d.equity_bridge.equity_value} currency={ccy} />],
            [
              'Diluted shares',
              d.equity_bridge.shares.value ? (
                <span>
                  <span className="num">{formatDecimal(d.equity_bridge.shares.value)}</span> shares <span className="muted">· {d.equity_bridge.shares.source} · obs {d.equity_bridge.shares.obs_ids.join(', ')}</span>
                  <Flags flags={d.equity_bridge.shares.flags} />
                </span>
              ) : (
                <span className="muted">not available: per-share value not computed</span>
              ),
            ],
            ['Per share', d.equity_bridge.per_share ? <Money amount={d.equity_bridge.per_share} currency={ccy} /> : <span className="muted">not computed</span>],
          ]}
        />
      </Section>

      <SensitivityGrid d={d} />

      <Section title="Scenarios" note={d.scenarios.note} flush>
        <div className="rx-table-wrap">
          <table className="rx-table">
            <caption>Scenario shifts and their values ({perShareUnit} for per share)</caption>
            <thead>
              <tr>
                <th scope="col">Scenario</th>
                <th scope="col" className="num">WACC</th>
                <th scope="col" className="num">g</th>
                <th scope="col" className="num">FCFF scale</th>
                <th scope="col" className="num">Enterprise value</th>
                <th scope="col" className="num">Equity value</th>
                <th scope="col" className="num">Per share</th>
              </tr>
            </thead>
            <tbody>
              {d.scenarios.scenarios.map((s) => (
                <tr key={s.name}>
                  <th scope="row">{s.name}</th>
                  <td className="num">{s.wacc}</td>
                  <td className="num">{s.g}</td>
                  <td className="num">{s.fcff_scale}</td>
                  {s.valid ? (
                    <>
                      <td className="num">
                        <Money amount={s.enterprise_value} currency={ccy} />
                      </td>
                      <td className="num">
                        <Money amount={s.equity_value} currency={ccy} />
                      </td>
                      <td className="num">{s.per_share ? <Money amount={s.per_share} currency={ccy} /> : '—'}</td>
                    </>
                  ) : (
                    <td colSpan={3} className="wrap rx-reason">
                      not valid{s.reason ? `: ${s.reason}` : ''}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Football field (per share)">
        {rangeRows.length ? (
          <RangeBars
            title={`Per-share value ranges in ${ccy}`}
            summary={rangeRows.map((r) => `${r.label}: ${r.minText} to ${r.maxText} ${perShareUnit}`).join('; ')}
            rows={rangeRows}
            format={(x) => String(Number(x.toPrecision(4)))}
            unit={perShareUnit}
          />
        ) : (
          <p className="muted">No per-share ranges (diluted shares not available).</p>
        )}
        <ul className="ops-checks">
          {Object.entries(ff)
            .filter(([k]) => k !== 'dcf_scenarios' && k !== 'dcf_sensitivity' && k !== 'current_price')
            .map(([k, val]) => (
              <li key={k}>
                <span className="label mono">{k}</span>
                <span>
                  <StatusTag status={(val as { status?: string }).status ?? 'OK'} />
                </span>
                <span className="detail">{(val as { note?: string; reason?: string }).note ?? (val as { reason?: string }).reason ?? ''}</span>
              </li>
            ))}
          {price ? (
            <li>
              <span className="label mono">current_price</span>
              <span>
                <PriceLine p={price} />
              </span>
            </li>
          ) : null}
        </ul>
      </Section>
      <details className="rx-details rx-gap">
        <summary>Full DCF response (as received)</summary>
        <JsonTree value={d} label="DCF response" />
      </details>
    </>
  );
}

function MetricSummary({ m }: { m: MetricRow }) {
  return (
    <KV
      rows={[
        ['Expression', <span className="mono">{m.expression}</span>],
        ['Value', <UnitValue value={m.value} unit={m.unit} missing={m.status} />],
        ['Status', <StatusTag status={m.status} />],
        m.reason ? ['Reason', <span className="rx-reason">{m.reason}</span>] : null,
        ['Period', <span className="mono">{m.period}</span>],
        ['Inputs', <span className="mono">{m.inputs.map((i) => `${i.role} ${i.value ?? 'missing'}${i.period ? ` (${i.period}, ${i.method ?? ''})` : ''}`).join('; ')}</span>],
        m.flags.length ? ['Flags', <Flags flags={m.flags} />] : null,
      ]}
    />
  );
}

function Tile({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="rx-tile">
      <span className="micro">{label}</span>
      <span className="rx-tile-value">{children}</span>
    </div>
  );
}

function SumRow({ label, amount, ccy, strong }: { label: string; amount: string; ccy: string; strong?: boolean }) {
  return (
    <tr>
      <th scope="row">{strong ? <strong>{label}</strong> : label}</th>
      <td className="num" colSpan={3}>
        {strong ? (
          <strong>
            <Money amount={amount} currency={ccy} />
          </strong>
        ) : (
          <Money amount={amount} currency={ccy} />
        )}
      </td>
    </tr>
  );
}

function SensitivityGrid({ d }: { d: DcfResult }) {
  const s = d.sensitivity;
  const ccy = d.currency;
  return (
    <Section
      title={`Sensitivity · ${s.measure.replace('_', ' ')}`}
      flush
      note={
        <>
          Rows: WACC; columns: terminal growth g. The outlined cell is the base case.
          {s.range ? ` Range ${s.range.min} to ${s.range.max} ${ccy}${s.measure === 'per_share' ? ' per share' : ''}.` : ''}
          {s.invalid_cells ? ` ${s.invalid_cells} cell(s) invalid (WACC must exceed g).` : ''}
        </>
      }
    >
      <div className="rx-table-wrap">
        <table className="rx-table">
          <caption>
            {s.measure === 'per_share' ? `Per-share value in ${ccy}` : `${humanize(s.measure)} in ${ccy}`} by WACC and g
          </caption>
          <thead>
            <tr>
              <th scope="col">WACC \ g</th>
              {s.g_axis.map((g) => (
                <th key={g} scope="col" className="num">
                  {g}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {s.grid.map((row, i) => (
              <tr key={s.wacc_axis[i] ?? i}>
                <th scope="row" className="num">
                  {s.wacc_axis[i]}
                </th>
                {row.map((c) => {
                  const base = sameDecimal(c.wacc, d.valuation.wacc) && sameDecimal(c.g, d.valuation.g);
                  const value = s.measure === 'per_share' ? c.per_share : s.measure === 'equity_value' ? c.equity_value : c.enterprise_value;
                  return (
                    <td key={c.g} className="num" style={base ? { outline: '2px solid var(--accent)', outlineOffset: -2 } : undefined}>
                      {c.valid && value ? <Money amount={value} currency={ccy} /> : <span className="muted" title={c.reason}>n/a</span>}
                      {base ? <span className="sr-only"> (base case)</span> : null}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
