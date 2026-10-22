/** Operations > Health and control: service health, terminal link, control state, ownership, blockers. */
import type { ReactNode } from 'react';
import type { Execution, Overview } from '../../api/types';
import { useControl } from '../../controls/ControlProvider';
import { describeBlocker, entryState } from '../../controls/model';
import type { QueryResult } from '../../state/react';
import { useQuery, useStream } from '../../state/react';
import { Age, ShortId, StatusPill, Timestamp } from '../../ui/primitives';
import { reconciliationTone } from '../../ui/tones';
import { QueryError, WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { estimatedFeeExecutions, exceptionCounts, healthChecks, healthLimits, isCoreHealth, worstTone } from './model';
import { TabPanel } from './parts';

const HEALTH_POLL_MS = 5000;

function YesNoPill({ value, yes, no, goodWhen }: { value: boolean; yes: string; no: string; goodWhen: boolean }) {
  return <StatusPill label={value ? yes : no} tone={value === goodWhen ? 'ok' : 'warn'} compact />;
}

export function StatusTab({
  bar,
  overview,
  executions,
  navigate,
}: {
  bar: ReactNode;
  overview: QueryResult<Overview>;
  executions: QueryResult<Execution[]>;
  navigate: (workspace: string, ...rest: string[]) => void;
}) {
  const health = useQuery('/api/health', undefined, { pollMs: HEALTH_POLL_MS });
  const preflight = useQuery('/api/preflight', undefined, { pollMs: HEALTH_POLL_MS });
  const policy = useQuery('/api/policy/active');
  const stream = useStream();
  const { openDrawer } = useControl();

  const limits = policy.data?.body.account_limits;
  const checks = healthChecks(health.data, healthLimits(limits));
  const core = health.data?.core;
  const o = overview.data;
  const control = o?.control ?? null;
  const own = o?.ownership ?? null;
  const rec = o?.last_reconciliation ?? null;
  const state = entryState(control, o);
  const epoch = preflight.data?.evidence.epoch;
  const blockers = preflight.data?.blockers ?? [];
  const counts = exceptionCounts(o?.exceptions_open ?? []);
  const fees = executions.data ? estimatedFeeExecutions(executions.data).length : null;
  const maxRecAgeS = limits?.max_reconciliation_age_s !== undefined ? Number(limits.max_reconciliation_age_s) : undefined;

  return (
    <WorkspaceFrame
      title="Operations"
      description="Service health, control state, reconciliation, exceptions and fee estimates, read from the core and its projections."
      queries={[health, overview, preflight, policy, executions]}
    >
      {bar}
      <TabPanel idBase="ops" current="status">
      <QueryError what="Service health" error={health.error} />
      <QueryError what="Preflight" error={preflight.error} />
      <QueryError what="Control state" error={overview.error} />

      <div className="grid grid-4">
        <section className="panel" aria-labelledby="ops-health">
          <header>
            <h2 id="ops-health">Service health</h2>
            {checks.length ? <StatusPill label={worstTone(checks.map((c) => c.tone)) === 'ok' ? 'healthy' : 'attention'} tone={worstTone(checks.map((c) => c.tone))} compact /> : null}
          </header>
          <div className="panel-body">
            {checks.length ? (
              <ul className="ops-checks">
                {checks.map((c) => (
                  <li key={c.id}>
                    <span className="label">{c.label}</span>
                    <StatusPill label={c.status} tone={c.tone} detail={c.detail} compact />
                    {c.tone !== 'ok' ? <span className="detail">{c.detail}</span> : null}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">{health.status === 'loading' ? 'Asking the core…' : 'No health record.'}</p>
            )}
          </div>
          <div className="panel-note">
            {isCoreHealth(core) ? (
              <>
                Core time <Timestamp ns={core.now_ns} withMs /> · log seq {health.data?.latest_seq ?? '—'} · polled every {HEALTH_POLL_MS / 1000} s
              </>
            ) : (
              <>Polled every {HEALTH_POLL_MS / 1000} s through the API.</>
            )}
          </div>
        </section>

        <section className="panel" aria-labelledby="ops-link">
          <header>
            <h2 id="ops-link">Terminal link</h2>
            <StatusPill
              label={stream.status === 'live' ? 'live' : stream.status}
              tone={stream.status === 'live' ? 'ok' : stream.status === 'reconnecting' ? 'warn' : 'neutral'}
              compact
            />
          </header>
          <div className="panel-body">
            <dl className="kv">
              <dt>Live updates</dt>
              <dd>{stream.status === 'live' ? 'connected: views refresh when the core records a change' : 'not connected: views may be out of date'}</dd>
              <dt>Latest seq seen</dt>
              <dd className="num">{stream.seq ?? 'none yet'}</dd>
              <dt>Last message</dt>
              <dd>{stream.lastMessageAtMs ? <><Age ageMs={0} observedAtMs={stream.lastMessageAtMs} /> ago</> : <span className="muted">none yet</span>}</dd>
              <dt>Reconnects</dt>
              <dd className="num">
                {stream.reconnects} <span className="muted">· gaps {stream.gaps} · resets {stream.resets}</span>
              </dd>
              {stream.lastError ? (
                <>
                  <dt>Last error</dt>
                  <dd>{stream.lastError}</dd>
                </>
              ) : null}
            </dl>
          </div>
          <div className="panel-note">After a reconnect every visible view is fetched again before new state is shown.</div>
        </section>

        <section className="panel" aria-labelledby="ops-control">
          <header>
            <h2 id="ops-control">Control state</h2>
            <StatusPill label={state.label} tone={state.tone} detail={state.detail} compact />
          </header>
          <div className="panel-body">
            {control ? (
              <dl className="kv">
                <dt>Stop latch</dt>
                <dd>
                  {control.stop_latched ? (
                    <>
                      <StatusPill label="latched" tone="bad" compact /> <code className="mono">{control.latch_reason ?? 'no reason recorded'}</code>
                    </>
                  ) : (
                    <StatusPill label="not latched" tone="ok" compact />
                  )}
                </dd>
                {control.stop_latched ? (
                  <>
                    <dt>Latched by</dt>
                    <dd>
                      {control.latched_by ?? <span className="muted">unknown</span>} at <Timestamp ns={control.latched_at_ns} />
                    </dd>
                    <dt>Latch command</dt>
                    <dd className="cmd-id">{control.latch_command_id ?? 'not recorded'}</dd>
                  </>
                ) : null}
                <dt>Resume hold</dt>
                <dd>
                  {control.resume_requires_owner ? (
                    <>
                      <StatusPill label="owner start required" tone="warn" compact /> <code className="mono">{control.resume_reason ?? 'no reason'}</code>
                    </>
                  ) : (
                    <StatusPill label="none" tone="ok" compact />
                  )}
                </dd>
                <dt>Needs reconciliation</dt>
                <dd>
                  <YesNoPill value={control.needs_reconciliation} yes="yes" no="no" goodWhen={false} />
                </dd>
                <dt>Reconciled epoch</dt>
                <dd>
                  <span className="num">{control.reconciled_epoch}</span>{' '}
                  {epoch !== undefined ? (
                    epoch === control.reconciled_epoch ? (
                      <span className="muted">(current process epoch)</span>
                    ) : (
                      <StatusPill label={`process epoch is ${epoch}: not reconciled since restart`} tone="warn" compact />
                    )
                  ) : null}
                </dd>
                <dt>Last clean run</dt>
                <dd>
                  {control.last_clean_reconciliation_ns ? (
                    <>
                      <Timestamp ns={control.last_clean_reconciliation_ns} />{' '}
                      <span className="muted">
                        (<Age sinceNs={control.last_clean_reconciliation_ns} staleAfterMs={maxRecAgeS !== undefined ? maxRecAgeS * 1000 : undefined} /> ago)
                      </span>
                    </>
                  ) : (
                    <span className="muted">never</span>
                  )}
                </dd>
                <dt>Broker (recorded)</dt>
                <dd>
                  {control.broker_connected ? (control.broker_authenticated ? 'connected, authenticated' : 'connected, not authenticated') : 'disconnected'}
                </dd>
                <dt>Opening import</dt>
                <dd>{control.imported ? 'done' : <span className="muted">not yet</span>}</dd>
                <dt>Last start</dt>
                <dd>
                  {control.last_start ? (
                    <>
                      <Timestamp ns={control.last_start.at_ns} /> by {control.last_start.by} <ShortId value={control.last_start.command_id} head={22} />
                    </>
                  ) : (
                    <span className="muted">never</span>
                  )}
                </dd>
              </dl>
            ) : (
              <p className="muted">{overview.status === 'loading' ? 'Loading…' : 'No control record for this account.'}</p>
            )}
          </div>
          <div className="panel-note">
            <button type="button" className="btn small" onClick={(e) => openDrawer(state.startApplies ? 'start' : 'stop', e.currentTarget)}>
              {state.startApplies ? 'Open start preflight' : 'Open stop progress'}
            </button>
          </div>
        </section>

        <section className="panel" aria-labelledby="ops-own">
          <header>
            <h2 id="ops-own">Execution ownership</h2>
            {own ? <StatusPill label={own.status} tone={own.status === 'HELD' ? 'ok' : 'warn'} compact /> : <StatusPill label="not acquired" tone="warn" compact />}
          </header>
          <div className="panel-body">
            {own ? (
              <dl className="kv">
                <dt>Deployment</dt>
                <dd className="mono">{own.deployment_id}</dd>
                <dt>Ownership epoch</dt>
                <dd className="num">{own.epoch}</dd>
                <dt>Granted</dt>
                <dd>
                  <Timestamp ns={own.granted_at_ns} /> by {own.granted_by}
                </dd>
                {own.released_at_ns ? (
                  <>
                    <dt>Released</dt>
                    <dd>
                      <Timestamp ns={own.released_at_ns} />
                    </dd>
                  </>
                ) : null}
                {own.release_certificate ? (
                  <>
                    <dt>Release certificate</dt>
                    <dd>
                      run <ShortId value={own.release_certificate.reconciliation_run} head={20} />, deployment {own.release_certificate.deployment_id}, epoch{' '}
                      {own.release_certificate.epoch}
                    </dd>
                  </>
                ) : null}
              </dl>
            ) : (
              <p className="muted">No deployment holds execution ownership; preflight reports EXECUTION_OWNERSHIP_NOT_HELD.</p>
            )}
          </div>
          <div className="panel-note">One executing deployment per account. Releasing requires a stop, no open orders and a clean reconciliation.</div>
        </section>
      </div>

      <div className="grid grid-2 section-gap">
        <section className="panel" aria-labelledby="ops-blockers">
          <header>
            <h2 id="ops-blockers">Start preflight</h2>
            {preflight.data ? (
              <StatusPill label={blockers.length ? `${blockers.length} blocker${blockers.length === 1 ? '' : 's'}` : 'clear'} tone={blockers.length ? 'warn' : 'ok'} compact />
            ) : null}
          </header>
          <div className="panel-body">
            {preflight.data ? (
              blockers.length ? (
                <ul className="ops-checks">
                  {blockers.map((b) => {
                    const info = describeBlocker(b);
                    const base = b.split(':')[0] ?? b;
                    const target = base === 'BLOCKING_EXCEPTIONS' ? 'exceptions' : base.startsWith('RECONCILIATION') ? 'reconciliation' : null;
                    return (
                      <li key={b}>
                        <span className="label">{info.title}</span>
                        <code className="mono">{b}</code>
                        <span className="detail">
                          {info.resolution}{' '}
                          {target ? (
                            <button type="button" className="btn small" onClick={() => navigate('operations', target)}>
                              Open {target}
                            </button>
                          ) : null}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <p style={{ margin: 0 }}>No blockers: a start request would pass preflight now.</p>
              )
            ) : (
              <p className="muted">{preflight.status === 'loading' ? 'Running the preflight dry run…' : 'Preflight unavailable.'}</p>
            )}
          </div>
          <div className="panel-note">Dry run of green START with this session’s authentication time; there is no bypass.</div>
        </section>

        <section className="panel" aria-labelledby="ops-attention">
          <header>
            <h2 id="ops-attention">Needs attention</h2>
          </header>
          <div className="panel-body">
            <ul className="ops-checks">
              <li>
                <span className="label">Open exceptions</span>
                <StatusPill
                  label={o ? `${counts.open} open, ${counts.blocking} blocking${counts.critical ? `, ${counts.critical} critical` : ''}` : '—'}
                  tone={counts.blocking || counts.critical ? 'bad' : counts.open ? 'warn' : 'ok'}
                  compact
                />
                <span className="detail">
                  <button type="button" className="btn small" onClick={() => navigate('operations', 'exceptions')}>
                    Review exceptions
                  </button>
                </span>
              </li>
              <li>
                <span className="label">Last reconciliation</span>
                {rec ? (
                  <span>
                    <StatusPill label={rec.status} tone={reconciliationTone(rec.status)} compact /> <Timestamp ns={rec.completed_at_ns} />{' '}
                    <span className="muted">
                      (<Age sinceNs={rec.completed_at_ns} staleAfterMs={maxRecAgeS !== undefined ? maxRecAgeS * 1000 : undefined} /> ago)
                    </span>
                  </span>
                ) : (
                  <span className="muted">none yet</span>
                )}
                <span className="detail">
                  <button type="button" className="btn small" onClick={() => navigate('operations', 'reconciliation')}>
                    Review runs
                  </button>
                </span>
              </li>
              <li>
                <span className="label">Estimated fees</span>
                <StatusPill
                  label={fees === null ? '—' : `${fees} execution${fees === 1 ? '' : 's'} with an estimated fee`}
                  tone={fees ? 'warn' : 'ok'}
                  compact
                />
                <span className="detail">
                  Sessions complete on final fees; accept an estimate only when the broker report will not arrive in time.{' '}
                  <button type="button" className="btn small" onClick={() => navigate('operations', 'fees')}>
                    Review fees
                  </button>
                </span>
              </li>
            </ul>
          </div>
        </section>
      </div>
      </TabPanel>
    </WorkspaceFrame>
  );
}
