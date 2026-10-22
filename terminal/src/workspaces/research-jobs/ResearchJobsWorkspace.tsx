/**
 * Research jobs (ADR-0016; book Ch. 22 "bounded agents").
 * Routes: `#/research-jobs` (list), `#/research-jobs/~new` (ask a question), `#/research-jobs/<job_id>`.
 */
import type { WorkspaceProps } from '../types';
import { JobDetailView } from './JobDetailView';
import { JobListView } from './JobListView';
import { parseJobsRoute } from './model';
import { SubmitView } from './SubmitView';

export function ResearchJobsWorkspace({ route, navigate }: WorkspaceProps) {
  const r = parseJobsRoute(route.rest);
  if (r.view === 'new') return <SubmitView />;
  if (r.view === 'job') return <JobDetailView key={r.jobId} jobId={r.jobId} />;
  return <JobListView navigate={navigate} />;
}
