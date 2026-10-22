/**
 * New policy version (#/policy/draft[/<base hash>]): a JSON editor seeded with the active policy (or
 * the chosen base), client-side JSON validation only, a live field-by-field diff against the base,
 * and Publish (step-up `publish_policy`). The core validates the schema; its rejection message is
 * shown as it was returned. Nothing is applied locally.
 */
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { defaultCommandId } from '../../api/client';
import type { PolicyBody, PolicyRecord } from '../../api/types';
import { CommandStatus } from '../../controls/Controls';
import { useCommands, useQuery, useSendCommand, useTrackedCommand } from '../../state/react';
import { routeHref } from '../../state/router';
import { ShortId, StatusPill, Timestamp } from '../../ui/primitives';
import { differences, diffBodies, summarize } from './diff';
import { checkPolicyJson, prettyJson } from './jsonCheck';
import { activationChecks, clearDraft, loadDraft, policyByHash, saveDraft } from './model';
import { DiffTable } from './PolicyParts';
import { ViewHeading, type PolicyContext } from './shared';

export function PolicyDraft({ ctx, baseHash }: { ctx: PolicyContext; baseHash: string | null }) {
  const editorId = useId();
  const statusId = useId();
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const send = useSendCommand();
  const commands = useCommands();
  const accounts = useQuery('/api/accounts');
  const [text, setText] = useState<string | null>(null);
  const [restoredAt, setRestoredAt] = useState<number | null>(null);
  const [commandId, setCommandId] = useState<string | null>(null);

  const base: PolicyRecord | undefined = policyByHash(ctx.policies, baseHash ?? ctx.active?.policy_hash ?? null);
  const baseKey = base?.policy_hash ?? null;

  // Seed once per base: an unsent draft for the same base wins over the published body.
  useEffect(() => {
    if (!base) return;
    const stored = loadDraft();
    if (stored && stored.baseHash === base.policy_hash) {
      setText(stored.text);
      setRestoredAt(stored.savedAtMs);
    } else {
      setText(prettyJson(base.body));
      setRestoredAt(null);
    }
    // Reseed only when the base changes, not when the policies list refetches.
  }, [baseKey]);

  const check = useMemo(() => (text === null ? null : checkPolicyJson(text)), [text]);
  const rows = useMemo(() => (base && check?.ok ? diffBodies(base.body, check.value) : []), [base, check]);
  const changes = differences(rows);
  const summary = summarize(rows);
  const draftBody = check?.ok ? (check.value as Record<string, unknown>) : null;
  const account = accounts.data?.find((a) => a.account_key === ctx.accountKey) ?? null;
  const activationProblems = draftBody ? activationChecks(draftBody, account, null).filter((c) => c.definite && c.ok === false) : [];
  const nameUnchanged = draftBody && base ? draftBody.name === base.body.name : false;

  const latestPublish = commands.find((c) => c.kind === 'publish_policy');
  const shownId = commandId ?? latestPublish?.commandId ?? null;
  const tracked = useTrackedCommand(shownId);
  const resultBody = (tracked?.response?.body ?? null) as { policy_hash?: unknown; already_published?: unknown } | null;
  const publishedHash = tracked?.response?.accepted && typeof resultBody?.policy_hash === 'string' ? resultBody.policy_hash : null;
  const sending = tracked?.status === 'sending';
  const unknownOutcome = tracked?.status === 'failed' && tracked.outcomeUnknown;

  const edit = (next: string) => {
    setText(next);
    if (base) saveDraft({ baseHash: base.policy_hash, text: next, savedAtMs: Date.now() });
  };

  const reset = () => {
    if (!base) return;
    clearDraft();
    setRestoredAt(null);
    setText(prettyJson(base.body));
    editorRef.current?.focus();
  };

  const jump = (offset: number) => {
    const el = editorRef.current;
    if (!el) return;
    el.focus();
    el.setSelectionRange(offset, Math.min(offset + 1, el.value.length));
  };

  const publish = async () => {
    if (!draftBody) return;
    const id = defaultCommandId('publish_policy');
    setCommandId(id);
    const t = await send(
      'publish_policy',
      { body: draftBody as PolicyBody },
      { label: `Publish policy ${String(draftBody.name ?? '')}`.trim(), commandId: id },
    );
    if (t.status === 'pending' || t.status === 'applied') clearDraft();
  };

  if (ctx.policies === undefined || ctx.active === undefined) {
    return (
      <section className="panel" aria-labelledby="policy-view-title">
        <ViewHeading ctx={ctx}>New policy version</ViewHeading>
        <div className="panel-body muted">Loading…</div>
      </section>
    );
  }
  if (!base) {
    return (
      <section className="panel" aria-labelledby="policy-view-title">
        <ViewHeading ctx={ctx}>New policy version</ViewHeading>
        <div className="panel-body">
          {baseHash ? (
            <p>
              No published policy has the hash <code className="mono">{baseHash}</code>. <a href={routeHref('policy', 'draft')}>Start from the active policy</a>
              .
            </p>
          ) : (
            <p className="muted">
              No policy is active, so there is nothing to start from. Open a published version and choose “New version from this”, or publish one with the owner
              command line (<code className="mono">python -m unknown.cli bootstrap-sim</code> in simulation).
            </p>
          )}
        </div>
      </section>
    );
  }

  const canPublish = Boolean(draftBody) && changes.length > 0 && !sending && !unknownOutcome;
  return (
    <div className="rk-split">
      <section className="panel" aria-labelledby="policy-view-title">
        <ViewHeading
          ctx={ctx}
          aside={
            <span className="rk-actions">
              <button type="button" className="btn small" onClick={() => draftBody && edit(prettyJson(draftBody))} disabled={!draftBody}>
                Format
              </button>
              <button type="button" className="btn small" onClick={reset}>
                Reset to base
              </button>
            </span>
          }
        >
          New policy version
        </ViewHeading>
        <div className="panel-body">
          <p style={{ margin: '0 0 8px' }}>
            Starting from <a href={routeHref('policy', base.policy_hash)}>{base.name}</a> <ShortId value={base.policy_hash} head={12} />
            {base.policy_hash === ctx.active?.policy_hash ? ' (active)' : ''}.{' '}
            {restoredAt !== null ? (
              <span className="muted">
                Restored your unsent draft from <Timestamp ms={restoredAt} zone="local" />.
              </span>
            ) : null}
          </p>
          <label htmlFor={editorId} className="micro" style={{ display: 'block', marginBottom: 4 }}>
            Policy body (JSON)
          </label>
          <textarea
            id={editorId}
            ref={editorRef}
            className="rk-editor"
            value={text ?? ''}
            onChange={(e) => edit(e.target.value)}
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            aria-describedby={statusId}
            aria-invalid={check ? !check.ok : undefined}
          />
          <div id={statusId} aria-live="polite" style={{ marginTop: 6 }}>
            {check === null ? null : check.ok ? (
              <StatusPill label="valid JSON object" tone="ok" compact detail="JSON syntax only; the core validates the policy schema when you publish" />
            ) : (
              <>
                <StatusPill label={`${check.issues.length} JSON ${check.issues.length === 1 ? 'problem' : 'problems'}`} tone="bad" compact />
                <ol className="rk-issues">
                  {check.issues.map((i, n) => (
                    <li key={n}>
                      <button type="button" onClick={() => jump(i.offset)}>
                        Line {i.line}, column {i.column}
                      </button>
                      : {i.message}
                    </li>
                  ))}
                </ol>
              </>
            )}
          </div>
          <p className="rk-basis" style={{ margin: '8px 0 0' }}>
            The terminal checks JSON only. The core validates the schema (required fields, ranges, released features) and refuses anything it does not accept;
            its message appears here. Amounts are decimal strings in quotes.
          </p>
        </div>
      </section>

      <section className="panel" aria-labelledby="policy-draft-changes">
        <header>
          <h2 id="policy-draft-changes">Changes from the base</h2>
          {check?.ok ? (
            <span className="num muted">
              {summary.changed} changed, {summary.added} added, {summary.removed} removed
            </span>
          ) : null}
        </header>
        <div className="panel-body flush">
          {check?.ok ? (
            <DiffTable
              rows={changes}
              currencyA={base.body.currency}
              currencyB={typeof draftBody?.currency === 'string' ? draftBody.currency : base.body.currency}
              baseLabel="Base"
              compareLabel="Draft"
              compact
              caption="Fields the draft changes"
              emptyText="No changes yet. Edit a value to create a new version."
              maxHeight="360px"
            />
          ) : (
            <p className="empty">Fix the JSON to see the changes.</p>
          )}
        </div>
        <div className="panel-body" style={{ borderTop: '1px solid var(--border)' }}>
          {nameUnchanged && changes.length ? (
            <p className="muted" style={{ margin: '0 0 8px' }}>
              The name is unchanged. Versions are identified by hash, but a new name keeps the list readable.
            </p>
          ) : null}
          {activationProblems.length ? (
            <div className="callout" style={{ ['--tone' as string]: 'var(--warn)', marginBottom: 8 }}>
              This version can be published, but activating it for {ctx.accountKey} would be rejected:{' '}
              {activationProblems.map((c) => `${c.code} (${c.detail})`).join('; ')}.
            </div>
          ) : null}
          <p style={{ margin: '0 0 8px' }}>
            Publishing records an immutable version and changes nothing until it is activated. Activation applies to future sessions; existing mandates are
            unchanged. You may be asked for your passphrase.
          </p>
          <div className="rk-actions">
            <button type="button" className="btn primary" disabled={!canPublish} onClick={() => void publish()}>
              {sending ? 'Publishing…' : 'Publish new version'}
            </button>
            {!draftBody ? (
              <span className="muted">Fix the JSON first.</span>
            ) : changes.length === 0 ? (
              <span className="muted">Nothing changed yet.</span>
            ) : null}
          </div>
          {shownId ? (
            <div style={{ marginTop: 10 }}>
              {commandId === null ? (
                <p className="micro" style={{ margin: '0 0 4px' }}>
                  Last publish from this terminal
                </p>
              ) : null}
              <CommandStatus commandId={shownId} />
              {tracked?.status === 'rejected' && tracked.rejection ? (
                <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)', marginTop: 8, whiteSpace: 'pre-wrap' }}>
                  <strong>The core rejected this policy</strong> (<code className="mono">{tracked.rejection.code}</code>): {tracked.rejection.message}
                </div>
              ) : null}
              {publishedHash ? (
                <div className="callout" role="status" style={{ ['--tone' as string]: 'var(--ok)', marginTop: 8 }}>
                  {resultBody?.already_published ? 'This exact body was already published as ' : 'Published as '}
                  <span className="cmd-id">{publishedHash}</span>. It is not active yet.
                  <div className="rk-actions" style={{ marginTop: 6 }}>
                    {tracked?.status === 'applied' ? (
                      <a className="btn small" href={routeHref('policy', publishedHash)}>
                        View version
                      </a>
                    ) : (
                      <span className="muted">Waiting for the state stream to confirm…</span>
                    )}
                    {ctx.accountKey && tracked?.status === 'applied' && ctx.policies?.some((p) => p.policy_hash === publishedHash) ? (
                      <button type="button" className="btn primary small" onClick={() => ctx.activate(publishedHash)}>
                        Activate for {ctx.accountKey}…
                      </button>
                    ) : null}
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </section>
    </div>
  );
}
