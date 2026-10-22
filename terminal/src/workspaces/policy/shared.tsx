/** Pieces shared by the policy views: the view context, the focusable view heading, names and the activate button. */
import type { ReactNode, RefObject } from 'react';
import type { ActivePolicy, EventRecord, PolicyRecord } from '../../api/types';
import { StatusPill } from '../../ui/primitives';

export interface PolicyContext {
  policies: readonly PolicyRecord[] | undefined;
  active: ActivePolicy | null | undefined;
  /** policy_hash -> the PolicyPublished event (principal, time, command id). */
  publishedBy: ReadonlyMap<string, EventRecord>;
  accountKey: string | null;
  navigate: (workspace: string, ...rest: string[]) => void;
  activate: (hash: string) => void;
  headingRef: RefObject<HTMLHeadingElement | null>;
}

export function ViewHeading({ ctx, children, aside }: { ctx: PolicyContext; children: ReactNode; aside?: ReactNode }) {
  return (
    <header>
      <h2 ref={ctx.headingRef} tabIndex={-1} id="policy-view-title">
        {children}
      </h2>
      {aside}
    </header>
  );
}

export function PolicyName({ policy, activeHash }: { policy: PolicyRecord; activeHash: string | null | undefined }) {
  return (
    <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
      <strong>{policy.name}</strong>
      {policy.policy_hash === activeHash ? <StatusPill label="active" tone="ok" compact /> : null}
    </span>
  );
}

export function ActivateButton({ ctx, policy }: { ctx: PolicyContext; policy: PolicyRecord }) {
  if (!ctx.accountKey) return null;
  const isActive = ctx.active?.policy_hash === policy.policy_hash;
  return (
    <button
      type="button"
      className="btn primary small"
      disabled={isActive}
      title={isActive ? `Already active for ${ctx.accountKey}` : 'Review the changes, then activate (passphrase may be required)'}
      onClick={() => ctx.activate(policy.policy_hash)}
    >
      {isActive ? `Active for ${ctx.accountKey}` : `Activate for ${ctx.accountKey}…`}
    </button>
  );
}
