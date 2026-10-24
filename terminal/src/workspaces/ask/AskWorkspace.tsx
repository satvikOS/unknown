/**
 * ASK: the AI analyst (backend api/routes/ai.py; ADR-0016). Routes: `#/ask` (new thread), `#/ask/<thread_id>`.
 *
 * The owner asks a question, optionally about the linked security (`<SECURITY> ASK` on the command line). The API
 * queues it; the analyst worker answers from the platform's own read-only data and the answer streams in over
 * GET /api/ai/stream. Every answer is labelled AI and advisory, shows the model, effort, tokens and exact cost, and
 * cites the tool results it used: a citation opens the result, exactly as the model saw it, in the side panel. Lines
 * without a source and numbers not found in the cited results are flagged. Nothing here can trade.
 */
import { useCallback, useEffect, useReducer, useState, type FormEvent, type ReactNode } from 'react';
import { describeError, encodePathSegment } from '../../api/client';
import { useLinkedSecurity } from '../../ui/link';
import { Badge, StatusPill } from '../../ui/primitives';
import { NoValue } from '../../ui/unavailable';
import { WorkspaceFrame } from '../../ui/WorkspaceFrame';
import type { WorkspaceProps } from '../types';
import { usePluginPost, usePluginQuery } from '../plugin-api/hooks';
import {
  activeTurn,
  answerTone,
  askRoute,
  buildAsk,
  checksByLine,
  exactCost,
  initialLive,
  lineKind,
  liveReducer,
  parseAskResponse,
  parseAskRoute,
  parseStatus,
  parseThread,
  parseThreads,
  resultBody,
  splitLine,
  type Answer,
  type AnswerCheck,
  type LiveState,
  type StreamEvent,
  type Usage,
} from './model';
import './ask.css';

const STREAM_EVENTS = ['job', 'status', 'turn', 'thinking', 'text', 'segment_end', 'reset', 'tool_pending', 'tool', 'tool_result',
  'usage', 'answer', 'done'] as const;

function useAskStream(jobId: string | null, onEvent: (e: StreamEvent) => void): void {
  useEffect(() => {
    if (!jobId || typeof EventSource === 'undefined') return;
    const es = new EventSource(`/api/ai/stream?job=${encodeURIComponent(jobId)}`, { withCredentials: true });
    for (const kind of STREAM_EVENTS) {
      es.addEventListener(kind, (ev) => {
        let data: unknown = null;
        try {
          data = JSON.parse((ev as MessageEvent<string>).data) as unknown;
        } catch {
          return;
        }
        onEvent({ kind, data });
        if (kind === 'done') es.close();
      });
    }
    return () => es.close();
  }, [jobId, onEvent]);
}

// ---------------------------------------------------------------------------- pieces
function tokens(n: number | null | undefined): ReactNode {
  return typeof n === 'number' ? n.toLocaleString('en-US') : <NoValue reason="NOT_REPORTED" detail="not reported yet" />;
}

export function CostLine({ model, effort, usage, cost, currency, exact }: {
  model: string | null; effort: string | null; usage: Usage | null; cost: string | null; currency: string | null; exact: boolean | null;
}) {
  return (
    <dl className="ask-meta">
      <div><dt>Model</dt><dd className="mono">{model ?? <NoValue reason="PENDING" detail="no model call yet" />}</dd></div>
      <div><dt>Effort</dt><dd>{effort ?? <NoValue reason="PENDING" detail="no model call yet" />}</dd></div>
      <div><dt>Tokens in / out</dt><dd className="num">{tokens(usage?.inputTokens)} / {tokens(usage?.outputTokens)}</dd></div>
      <div><dt>Cache read / write</dt><dd className="num">{tokens(usage?.cacheReadTokens)} / {tokens(usage?.cacheWriteTokens)}</dd></div>
      <div>
        <dt>Cost</dt>
        <dd className="num">
          {cost && currency ? `${exactCost(cost)} ${currency}` : <NoValue reason="PENDING" detail="no settled model call yet" />}
          {exact === false ? <span className="ask-flag" title="Usage was incomplete or a rate is unverified: the full reservation is booked until reconciled."> reserved, not exact</span> : null}
        </dd>
      </div>
    </dl>
  );
}

function RefChip({ jobRef, onOpen, known }: { jobRef: string; onOpen: (ref: string) => void; known: boolean }) {
  return (
    <button type="button" className={`ask-ref${known ? '' : ' ask-ref-bad'}`} onClick={() => onOpen(jobRef)}
      title={known ? `Open tool result ${jobRef}` : `${jobRef} names no tool result of this answer`}>
      {jobRef}
    </button>
  );
}

export function AnswerText({ text, check, refs, onOpen }: { text: string; check: AnswerCheck | null; refs: ReadonlySet<string>; onOpen: (ref: string) => void }) {
  const byLine = checksByLine(check);
  return (
    <div className="ask-answer-text">
      {text.split('\n').map((line, i) => {
        const { kind, body } = lineKind(line);
        if (kind === 'blank') return null;
        const c = byLine.get(i);
        const pieces = splitLine(body).map((p, j) =>
          p.kind === 'ref' ? <RefChip key={j} jobRef={p.ref} onOpen={onOpen} known={refs.has(p.ref)} />
            : p.kind === 'bold' ? <strong key={j}>{p.text}</strong> : <span key={j}>{p.text}</span>);
        const flags = c ? (
          <>
            {c.status === 'unsourced' ? <span className="ask-flag" title="No tool result is cited for this statement.">no source</span> : null}
            {c.numbersUnmatched.length ? (
              <span className="ask-flag" title="These numbers were not found in the cited tool results.">
                not in source: {c.numbersUnmatched.join(', ')}
              </span>
            ) : null}
          </>
        ) : null;
        const cls = `ask-line ask-${kind}${c?.status === 'unsourced' ? ' ask-unsourced' : ''}`;
        if (kind === 'h') return <h3 key={i} className={cls}>{pieces}</h3>;
        return <p key={i} className={cls}>{pieces}{flags}</p>;
      })}
    </div>
  );
}

export function AnswerView({ answer, onOpen }: { answer: Answer; onOpen: (ref: string) => void }) {
  const refs = new Set(answer.citations.map((c) => c.ref));
  const check = answer.check;
  return (
    <div className="ask-answer">
      <div className="ask-answer-head">
        <Badge tone="info" title={answer.label ?? 'AI output: advisory only'}>AI</Badge>
        <StatusPill label={answer.status} tone={answerTone(answer.status)} detail={answer.reason ?? undefined} compact />
        {check ? (
          <span className="muted">
            {check.cited} cited, {check.unsourced} without source{check.numbersUnmatched ? `, ${check.numbersUnmatched} numbers not in source` : ''}
          </span>
        ) : null}
      </div>
      {answer.text ? <AnswerText text={answer.text} check={check} refs={refs} onOpen={onOpen} /> : (
        <p className="ask-reason">{answer.reason ?? 'No answer was produced.'}</p>
      )}
      {answer.citations.length ? (
        <div className="ask-cites">
          <span className="muted">Sources:</span>
          {answer.citations.map((c) => (
            <button type="button" key={c.ref} className="ask-cite" onClick={() => onOpen(c.ref)}
              title={`${c.tool} ${JSON.stringify(c.arguments)} (${c.decision}, ${c.ruleId})`}>
              <span className="ask-ref">{c.ref}</span> {c.tool}
              {c.status && c.status !== 'ok' ? <span className="ask-flag"> {c.status}</span> : null}
            </button>
          ))}
        </div>
      ) : null}
      <CostLine model={answer.model} effort={answer.effort} usage={answer.usage} cost={answer.cost} currency={answer.currency} exact={answer.costExact} />
      {answer.label ? <p className="ask-label">{answer.label}</p> : null}
    </div>
  );
}

export function LiveView({ live, onOpen, onCancel, cancelling }: { live: LiveState; onOpen: (ref: string) => void; onCancel: () => void; cancelling: boolean }) {
  if (live.answer) return <AnswerView answer={live.answer} onOpen={onOpen} />;
  const phase = live.phase === 'queued' ? 'queued: waiting for the analyst worker' : live.thinking ? 'thinking' : live.phase;
  return (
    <div className="ask-answer ask-live" aria-live="polite">
      <div className="ask-answer-head">
        <Badge tone="info" title="AI output: advisory only">AI</Badge>
        <StatusPill label={phase} tone={live.phase === 'done' ? 'neutral' : 'info'} compact />
        {live.phase !== 'done' ? (
          <button type="button" className="btn" onClick={onCancel} disabled={cancelling}>{cancelling ? 'Cancelling…' : 'Cancel'}</button>
        ) : null}
      </div>
      {live.tools.length ? (
        <ul className="ask-tools">
          {live.tools.map((t, i) => (
            <li key={i}>
              {t.ref ? <RefChip jobRef={t.ref} onOpen={onOpen} known={t.status !== null} /> : null} <span className="mono">{t.tool}</span>{' '}
              <span className="muted">{t.status ?? 'running'}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {live.segments.map((g, i) => (
        <p key={i} className={`ask-stream${g.interim ? ' ask-interim' : ''}`}>{g.text}</p>
      ))}
      {live.done && !live.answer ? <p className="ask-reason">{live.done.state}: {live.done.reason ?? live.done.outcome}</p> : null}
      <CostLine model={live.model} effort={live.effort} usage={live.usage} cost={live.cost} currency={live.currency} exact={live.costExact} />
    </div>
  );
}

/** One running question: its own stream and reducer, keyed by job id so a new question starts clean. */
function LiveTurn({ jobId, onDone, onOpen, onCancel, cancelling }: {
  jobId: string; onDone: () => void; onOpen: (ref: string) => void; onCancel: () => void; cancelling: boolean;
}) {
  const [live, dispatch] = useReducer(liveReducer, jobId, initialLive);
  const onEvent = useCallback((e: StreamEvent) => {
    dispatch(e);
    if (e.kind === 'done') onDone();
  }, [onDone]);
  useAskStream(jobId, onEvent);
  return <LiveView live={live} onOpen={onOpen} onCancel={onCancel} cancelling={cancelling} />;
}

function ResultPanel({ jobId, jobRef, onClose }: { jobId: string; jobRef: string; onClose: () => void }) {
  const q = usePluginQuery<unknown>(`/api/ai/results/${encodePathSegment(jobId)}/${encodePathSegment(jobRef)}`, undefined, { seqDriven: false });
  const r = q.data as Record<string, unknown> | undefined;
  const content = typeof r?.content === 'string' ? r.content : null;
  const body = content ? resultBody(content) : null;
  const json = body?.json as Record<string, unknown> | null | undefined;
  return (
    <aside className="panel ask-side" aria-label={`Tool result ${jobRef}`}>
      <header>
        <h2>Source {jobRef}</h2>
        <button type="button" className="btn" onClick={onClose}>Close</button>
      </header>
      <div className="panel-body">
        {q.error ? <p className="form-error">{describeError(q.error)}</p> : null}
        {r ? (
          <>
            <dl className="ask-meta">
              <div><dt>Tool</dt><dd className="mono">{String(r.tool ?? '')}</dd></div>
              <div><dt>Arguments</dt><dd className="mono">{JSON.stringify(r.arguments)}</dd></div>
              <div><dt>Gateway</dt><dd>{String(r.decision ?? '')} ({String(r.rule_id ?? '')})</dd></div>
              <div><dt>Datasets</dt><dd className="mono">{Array.isArray(r.datasets) && r.datasets.length ? r.datasets.join(', ') : 'platform'}</dd></div>
            </dl>
            {json && json.status === 'UNAVAILABLE' ? (
              <p className="ask-reason">
                Unavailable <NoValue reason={String(json.reason_code ?? 'NO_DATA')} detail={String(json.reason ?? '')} focusable />: {String(json.reason ?? '')}
              </p>
            ) : null}
            <p className="muted">Exactly what the model saw, as untrusted data{body?.header ? ' (between its nonce markers)' : ''}:</p>
            <pre className="ask-pre">{json ? JSON.stringify(json, null, 2) : body?.text}</pre>
          </>
        ) : null}
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------------------- workspace
export function AskWorkspace({ route, navigate }: WorkspaceProps) {
  const { threadId } = parseAskRoute(route.rest);
  const linked = useLinkedSecurity();
  const post = usePluginPost();
  const status = usePluginQuery<unknown>('/api/ai/status', undefined, { seqDriven: false });
  const threads = usePluginQuery<unknown>('/api/ai/threads', { limit: 50 }, { seqDriven: false });
  const threadQ = usePluginQuery<unknown>(threadId ? `/api/ai/threads/${encodePathSegment(threadId)}` : null, undefined, { seqDriven: false });
  const st = parseStatus(status.data);
  const thread = parseThread(threadQ.data);
  const pending = activeTurn(thread);
  const [question, setQuestion] = useState('');
  const [about, setAbout] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [open, setOpen] = useState<{ jobId: string; ref: string } | null>(null);
  const liveJob = pending?.jobId ?? null;
  const refetchThread = threadQ.refetch;
  const refetchThreads = threads.refetch;
  const refetchStatus = status.refetch;
  const onDone = useCallback(() => {
    refetchThread();
    refetchThreads();
    refetchStatus();
  }, [refetchThread, refetchThreads, refetchStatus]);

  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    const built = buildAsk(question, threadId, about ? linked : null);
    if ('error' in built) return setError(built.error);
    setSending(true);
    setError(null);
    try {
      const res = parseAskResponse(await post<unknown>('/api/ai/ask', built.body));
      setQuestion('');
      if (res) {
        if (res.threadId !== threadId) navigate('ask', ...askRoute(res.threadId));
        else refetchThread();
        refetchThreads();
      }
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSending(false);
    }
  };

  const cancel = async () => {
    if (!liveJob) return;
    setCancelling(true);
    try {
      await post<unknown>(`/api/ai/jobs/${encodePathSegment(liveJob)}/cancel`, {});
    } catch (err) {
      setError(describeError(err));
    } finally {
      setCancelling(false);
      refetchThread();
    }
  };

  const openRef = (jobId: string) => (ref: string) => setOpen({ jobId, ref });
  const threadList = parseThreads(threads.data);
  return (
    <WorkspaceFrame
      title="Ask AI"
      description="The AI analyst answers from the platform's own data with cited sources. Advisory only: it cannot trade, start sessions or move money."
      queries={[status, threads, ...(threadId ? [threadQ] : [])]}
    >
      <div className={`ask-grid${open ? ' ask-grid-side' : ''}`}>
        <nav className="panel ask-threads" aria-label="Conversations">
          <header>
            <h2>Conversations</h2>
            <button type="button" className="btn" onClick={() => navigate('ask')}>New</button>
          </header>
          <ul>
            {threadList.length ? threadList.map((t) => (
              <li key={t.threadId}>
                <a href={`#/ask/${t.threadId}`} aria-current={t.threadId === threadId ? 'page' : undefined}
                  onClick={(e) => { e.preventDefault(); navigate('ask', t.threadId); }}>
                  {t.title || 'Untitled'}
                </a>
                <span className="muted num"> {t.turns}</span>
              </li>
            )) : <li className="muted">No conversations yet.</li>}
          </ul>
        </nav>
        <section className="ask-main">
          {st && !st.enabled ? (
            <p className="ask-reason" role="status">AI is off: {st.disabledReason}</p>
          ) : null}
          {thread?.turns.map((t) => (
            <article key={t.jobId} className="ask-turn">
              <p className="ask-question">
                <span className="muted">You{t.security ? ` · about ${t.security}` : ''}:</span> {t.question}
              </p>
              {t.answer ? <AnswerView answer={t.answer} onOpen={openRef(t.jobId)} />
                : t.jobId === liveJob ? <LiveTurn key={t.jobId} jobId={t.jobId} onDone={onDone} onOpen={openRef(t.jobId)} onCancel={cancel} cancelling={cancelling} />
                  : <p className="ask-reason">{t.jobState ?? 'unknown state'}</p>}
            </article>
          ))}
          <form className="ask-form" onSubmit={submit}>
            <label htmlFor="ask-q" className="sr-only">Question</label>
            <textarea id="ask-q" value={question} onChange={(e) => setQuestion(e.target.value)} rows={3} maxLength={4000}
              placeholder={threadId ? 'Ask a follow-up question' : 'Ask about filings, fundamentals, news or what data is available'}
              onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void submit(e); }} />
            <div className="ask-form-row">
              {linked ? (
                <label><input type="checkbox" checked={about} onChange={(e) => setAbout(e.target.checked)} /> About {linked}</label>
              ) : <span className="muted">No linked security</span>}
              <span className="muted">
                {st?.models[0] ? `${st.models[0].model}, effort ${st.models[0].effort ?? 'unset'}` : 'no approved model'}
                {st?.caps ? ` · cap ${st.caps.perJob} ${st.caps.currency} per question, ${st.caps.perDay} per day` : ''}
                {st?.spentToday ? ` · spent today ${exactCost(st.spentToday)} ${st.caps?.currency ?? ''}` : ''}
              </span>
              <button type="submit" className="btn primary" disabled={sending || !st?.enabled || !!pending}>
                {sending ? 'Asking…' : 'Ask'}
              </button>
            </div>
            {error ? <p className="form-error" role="alert">{error}</p> : null}
          </form>
        </section>
        {open ? <ResultPanel jobId={open.jobId} jobRef={open.ref} onClose={() => setOpen(null)} /> : null}
      </div>
    </WorkspaceFrame>
  );
}
