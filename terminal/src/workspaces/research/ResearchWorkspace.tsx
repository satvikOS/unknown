/**
 * Research (I5; CAP-RES-10/-11/-12; DAT-05, DAT-06, TRM-03): read-only views of the research registry served
 * at GET /api/research — pre-registered experiments with trial counts and the holdout access log, every trial
 * with its manifest and return series, strategy specifications, artifacts with status history and gate
 * verdicts, reports, and the formula registry. Anything whose provenance is FIXTURE is refused and shown as an error.
 * When the registry does not exist the API's reason is shown with the command that creates it.
 *
 * Routes: `#/research[/experiments[/<id>[/segment=…]]]`, `#/research/trials/<trial_id>`,
 * `#/research/<artifacts|strategies|reports>[/<id>]`, `#/research/formulas`.
 */
import type { ReactNode } from 'react';
import type { QueryResult } from '../../state/react';
import { WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { RouteTabs, TabPanel } from '../operations/parts';
import { useApi } from '../research-common/api';
import '../research-common/research.css';
import type { WorkspaceProps } from '../types';
import { ArtifactList, ArtifactView } from './Artifacts';
import { ExperimentList, ExperimentView, TrialView } from './Experiments';
import { FormulaList, ReportList, ReportView, StrategyList, StrategyView } from './Library';
import { RESEARCH_TABS, RESEARCH_TAB_LABELS, isTrialView, parseResearchRoute, type Envelope, type ResearchIndex, type ResearchTab, type ResearchView } from './model';

export interface ResearchCtx {
  view: ResearchView;
  navigate: WorkspaceProps['navigate'];
  bar: ReactNode;
  index: QueryResult<Envelope<ResearchIndex>>;
}

const COUNT_KEY: Partial<Record<ResearchTab, string>> = {
  experiments: 'experiment',
  artifacts: 'artifact',
  strategies: 'strategy_spec',
  reports: 'report',
};

export function ResearchWorkspace({ route, navigate }: WorkspaceProps) {
  const view = parseResearchRoute(route.rest);
  const index = useApi<Envelope<ResearchIndex>>('/api/research');
  const counts = index.data?.available ? index.data.data?.counts : undefined;
  const bar = (
    <RouteTabs
      idBase="research"
      label="Research sections"
      tabs={RESEARCH_TABS.map((t) => ({ id: t, label: RESEARCH_TAB_LABELS[t], count: COUNT_KEY[t] && counts ? (counts[COUNT_KEY[t]!] ?? 0) : null }))}
      current={view.tab}
      onSelect={(t) => navigate('research', t)}
    />
  );
  const ctx: ResearchCtx = { view, navigate, bar, index };
  if (isTrialView(view)) return <TrialView ctx={ctx} id={view.trial} />;
  switch (view.tab) {
    case 'artifacts':
      return view.id ? <ArtifactView ctx={ctx} hash={view.id} /> : <ArtifactList ctx={ctx} />;
    case 'strategies':
      return view.id ? <StrategyView ctx={ctx} hash={view.id} /> : <StrategyList ctx={ctx} />;
    case 'reports':
      return view.id ? <ReportView ctx={ctx} id={view.id} /> : <ReportList ctx={ctx} />;
    case 'formulas':
      return <FormulaList ctx={ctx} />;
    default:
      return 'segment' in view && view.id ? <ExperimentView ctx={ctx} id={view.id} segment={view.segment} /> : <ExperimentList ctx={ctx} />;
  }
}

export function ResearchFrame({ ctx, queries, children }: { ctx: ResearchCtx; queries: readonly QueryResult<unknown>[]; children: ReactNode }) {
  const counts = ctx.index.data?.available ? ctx.index.data.data?.counts : undefined;
  return (
    <WorkspaceFrame
      title="Research"
      description="Pre-registered experiments, every trial, strategy artifacts and their evidence: the research registry, read-only."
      queries={[ctx.index, ...queries]}
    >
      {ctx.bar}
      <TabPanel idBase="research" current={ctx.view.tab}>
        {counts ? (
          <p className="rx-hint">
            Registry: {counts.experiment ?? 0} experiments, {counts.trial ?? 0} trials, {counts.strategy_spec ?? 0} specifications, {counts.artifact ?? 0} artifacts, {counts.evidence ?? 0} evidence records,{' '}
            {counts.transition ?? 0} transitions, {counts.report ?? 0} reports. Nothing here can change a status or approve anything.
          </p>
        ) : null}
        {children}
      </TabPanel>
    </WorkspaceFrame>
  );
}
