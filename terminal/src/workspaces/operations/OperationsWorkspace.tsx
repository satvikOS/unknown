/**
 * Operations (book Ch. 22: reconciliation, alerts, service health; primary action "follow runbook").
 * Route: `#/operations/<tab>/<id>` with tabs status, exceptions/<case_id>, reconciliation/<run_id>, fees.
 */
import { useQuery } from '../../state/react';
import type { WorkspaceProps } from '../types';
import { ExceptionsTab } from './ExceptionsTab';
import { EXECUTION_LIMIT, FeesTab } from './FeesTab';
import { OPS_TABS, estimatedFeeExecutions, exceptionCounts, parseOpsRoute, type OpsTab } from './model';
import { RouteTabs, type TabSpec } from './parts';
import { ReconciliationTab } from './ReconciliationTab';
import { StatusTab } from './StatusTab';

export function OperationsWorkspace({ route, navigate }: WorkspaceProps) {
  const { tab, id } = parseOpsRoute(route.rest);
  const overview = useQuery('/api/overview');
  const executions = useQuery('/api/executions', { limit: EXECUTION_LIMIT });
  // Same cache entry as the Exceptions tab's default view, so the count and the list agree.
  const openCases = useQuery('/api/exceptions', { status: 'OPEN' });
  const counts = exceptionCounts(openCases.data ?? []);
  const fees = executions.data ? estimatedFeeExecutions(executions.data).length : null;
  const rec = overview.data?.last_reconciliation ?? null;

  const tabs: TabSpec<OpsTab>[] = OPS_TABS.map((t) => {
    if (t.id === 'exceptions') {
      return { ...t, count: openCases.data ? counts.open : null, countTone: counts.blocking ? 'bad' : counts.open ? 'warn' : 'neutral' };
    }
    if (t.id === 'fees') return { ...t, count: fees, countTone: fees ? 'warn' : 'neutral' };
    if (t.id === 'reconciliation' && rec && rec.status !== 'CLEAN') return { ...t, label: `${t.label} (${rec.status.toLowerCase()})` };
    return t;
  });
  const bar = <RouteTabs idBase="ops" label="Operations sections" tabs={tabs} current={tab} onSelect={(t) => navigate('operations', t)} />;

  switch (tab) {
    case 'exceptions':
      return <ExceptionsTab bar={bar} caseId={id} navigate={navigate} />;
    case 'reconciliation':
      return <ReconciliationTab bar={bar} runId={id} navigate={navigate} />;
    case 'fees':
      return <FeesTab bar={bar} executions={executions} />;
    default:
      return <StatusTab bar={bar} overview={overview} executions={executions} navigate={navigate} />;
  }
}
