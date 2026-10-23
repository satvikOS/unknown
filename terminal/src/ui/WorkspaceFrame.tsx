/** Shared workspace chrome: title, description and data provenance (seq, age, refresh). */
import type { ReactNode } from 'react';
import { describeError } from '../api/client';
import { hasWorkspace } from '../workspaces/registry';
import { routeHref } from '../state/router';
import type { QueryResult } from '../state/react';
import { Icon } from './Icon';
import { usePanel, wsTitleId } from './panel';
import { Age, StatusPill } from './primitives';

export interface WorkspaceFrameProps {
  title: string;
  description?: string;
  /** Queries whose freshness the header reports; refresh refetches them. */
  queries?: readonly QueryResult<unknown>[];
  actions?: ReactNode;
  children: ReactNode;
}

export function WorkspaceFrame({ title, description, queries = [], actions, children }: WorkspaceFrameProps) {
  const fetched = queries.map((q) => q.fetchedAtMs).filter((x): x is number => x !== null);
  const oldest = fetched.length ? Math.min(...fetched) : null;
  const seqs = queries.map((q) => q.seqAtFetch).filter((x): x is number => x !== null);
  const seq = seqs.length ? Math.min(...seqs) : null;
  const fetching = queries.some((q) => q.fetching);
  const errors = queries.filter((q) => q.error);
  const panel = usePanel();
  const Heading = panel ? 'h2' : 'h1';
  return (
    <>
      <div className="ws-head">
        <div>
          <Heading id={wsTitleId(panel)} className={panel ? 'ws-panel-title' : undefined} tabIndex={-1}>
            {title}
          </Heading>
          {description ? <p>{description}</p> : null}
        </div>
        <div className="provenance" aria-live="polite">
          {errors.length ? <StatusPill label="refresh failed" tone="bad" detail={describeError(errors[0]?.error)} compact /> : null}
          {fetching ? <StatusPill label="updating" tone="info" compact /> : null}
          {seq !== null ? <span className="num">as of seq ≥ {seq}</span> : null}
          {oldest !== null ? (
            <span>
              fetched <Age ageMs={0} observedAtMs={oldest} /> ago
            </span>
          ) : null}
          {queries.length ? (
            <button type="button" className="btn small" onClick={() => queries.forEach((q) => q.refetch())}>
              <Icon name="refresh" size={14} /> Refresh
            </button>
          ) : null}
          {actions}
        </div>
      </div>
      {children}
    </>
  );
}

/** A link to another workspace that renders as plain text when that workspace is not registered. */
export function WorkspaceLink({ to, rest = [], children }: { to: string; rest?: string[]; children: ReactNode }) {
  if (!hasWorkspace(to)) return <>{children}</>;
  return <a href={routeHref(to, ...rest)}>{children}</a>;
}

/** Inline error box for a failed query (message from the API, never swallowed). */
export function QueryError({ what, error }: { what: string; error: unknown }) {
  if (!error) return null;
  return (
    <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)', margin: '8px 0' }}>
      <strong>{what} could not be loaded.</strong> {describeError(error)}
    </div>
  );
}
