/** One AI note: label, cited lines (unverified lines flagged), data lines, every source considered, and the cost. */
import { useState } from 'react';
import { routeHref } from '../../state/router';
import { useSetLinkedSecurity } from '../../ui/link';
import { Badge, ShortId, StatusPill, Timestamp } from '../../ui/primitives';
import { NoValue } from '../../ui/unavailable';
import { QueryError, WorkspaceFrame } from '../../ui/WorkspaceFrame';
import { usePluginQuery } from '../plugin-api/hooks';
import type { WorkspaceProps } from '../types';
import { STATUS_LABEL, lineFlag, parseNoteDetail, statusTone, trimAmount, type Citation, type NoteDetail } from './model';

function CitationChip({ id, note, onOpen }: { id: string; note: NoteDetail; onOpen: (id: string) => void }) {
  const c = note.citations[id];
  return (
    <button type="button" className="ai-cite" title={c ? c.title : 'source not in this note'} onClick={() => onOpen(id)}>
      {id}
    </button>
  );
}

function SourcePanel({ citation, navigate }: { citation: Citation } & Pick<WorkspaceProps, 'navigate'>) {
  const stored = usePluginQuery<{ item?: { summary?: string | null } }>(
    citation.apiPath ? citation.apiPath.split('?')[0]! : null,
    citation.apiPath ? Object.fromEntries(new URLSearchParams(citation.apiPath.split('?')[1] ?? '')) : undefined,
  );
  const setLinked = useSetLinkedSecurity();
  return (
    <section className="ai-source" aria-label={`Source ${citation.id}`}>
      <h3>
        {citation.id} <Badge tone="neutral">{citation.kind}</Badge>
      </h3>
      <p className="ai-text">{citation.title}</p>
      <dl className="ai-dl">
        <dt>Source</dt>
        <dd>{citation.source ?? <NoValue reason="NOT_REPORTED" />}</dd>
        <dt>Published</dt>
        <dd>{citation.published ?? <NoValue reason="NOT_REPORTED" detail="the feed states no publication time" />}</dd>
        <dt>Received</dt>
        <dd>{citation.firstSeen ?? <NoValue reason="NOT_REPORTED" />}</dd>
        <dt>Dataset</dt>
        <dd>{citation.datasetId ?? <NoValue reason="NOT_APPLICABLE" />}</dd>
      </dl>
      {citation.apiPath ? (
        <>
          <QueryError what="The stored item" error={stored.error} />
          {stored.data?.item?.summary ? <p className="ai-text ai-quote">{stored.data.item.summary}</p> : null}
        </>
      ) : null}
      <div className="ai-inline">
        {citation.link ? (
          <a href={citation.link} target="_blank" rel="noreferrer noopener">
            Open at the publisher
          </a>
        ) : null}
        {citation.security ? (
          <button
            type="button"
            className="btn small"
            onClick={() => {
              setLinked(citation.security);
              navigate('chart');
            }}
          >
            Chart {citation.security}
          </button>
        ) : null}
      </div>
    </section>
  );
}

/** The note itself (pure: parsed note in, markup out), so its labels and flags are testable without a runtime. */
export function NoteBody({ note, onCite }: { note: NoteDetail; onCite: (id: string) => void }) {
  const cite = onCite;
  return (
    <div className="ai-stack">
      <div className="ai-inline">
        <StatusPill label={STATUS_LABEL[note.status] ?? note.status} tone={statusTone(note.status)} />
        {note.label?.kind === 'AI' ? (
          <Badge tone="info" title={note.label.advisory}>
            {`AI · ${note.label.modelIds.join(', ') || 'model not reported'}`}
          </Badge>
        ) : (
          <Badge tone="neutral">data only (no model output)</Badge>
        )}
        <Timestamp ns={note.createdNs} nsStr={note.createdNsStr} />
        {note.label?.promptTemplateHash ? (
          <span className="muted">
            prompt <ShortId value={note.label.promptTemplateHash} head={10} />
          </span>
        ) : null}
        {!note.contentHashOk ? <Badge tone="bad">content hash mismatch</Badge> : null}
      </div>
      <div className="callout" role="note" style={{ ['--tone' as string]: 'var(--info)' }}>
        {note.label?.advisory || 'Advisory note; not investment advice.'}
        {note.window ? ` Window ${note.window.from ?? '?'} to ${note.window.to ?? '?'} (UTC).` : ''}
      </div>
      {note.headline ? (
        <p className="ai-headline">
          {note.headline}{' '}
          <Badge tone="warn" title="The overview is model text that cites nothing by itself; rely on the cited lines">
            overview, uncited
          </Badge>
        </p>
      ) : null}
      {note.explanation ? <p className="ai-headline">{note.explanation}</p> : null}
      {note.ai.status === 'ai_unavailable' ? (
        <div className="callout" role="status" style={{ ['--tone' as string]: 'var(--warn)' }}>
          No AI text: {note.ai.reason ?? 'the model call did not run'}. The sources considered are listed below without AI.
        </div>
      ) : null}
      {note.lines.length ? (
        <ol className="ai-lines">
          {note.lines.map((ln, i) => {
            const flag = lineFlag(ln);
            return (
              <li key={i} className={flag ? 'flagged' : ''}>
                <span className="ai-text">{ln.text}</span>{' '}
                {ln.citations.map((c) => (
                  <CitationChip key={c} id={c} note={note} onOpen={cite} />
                ))}
                {flag ? (
                  <div className="ai-flag" role="note">
                    <Badge tone="warn">{ln.status}</Badge> {flag}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ol>
      ) : null}
      {note.dataLines.length ? (
        <section aria-label="Data lines">
          <h3>Prices (data, not AI)</h3>
          <ul className="ai-plain">
            {note.dataLines.map((d, i) => (
              <li key={i}>
                {d.text}{' '}
                {d.citations.map((c) => (
                  <CitationChip key={c} id={c} note={note} onOpen={cite} />
                ))}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {note.unavailable.length ? (
        <section aria-label="Unavailable">
          <h3>Not available</h3>
          <ul className="ai-plain">
            {note.unavailable.map((u, i) => (
              <li key={i}>
                <NoValue reason="NO_SOURCE" detail={u.reason} label={u.what} /> <strong>{u.what}</strong>: {u.reason}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <section aria-label="Sources considered">
        <h3>Sources considered ({note.items.length})</h3>
        {note.items.length ? (
          <ul className="ai-plain">
            {note.items.map((it) => (
              <li key={it.citation}>
                <CitationChip id={it.citation} note={note} onOpen={cite} /> {it.form ? `${it.form} ` : ''}
                {it.title} <span className="muted">({it.why})</span>{' '}
                {it.sentToModel ? (
                  <Badge tone="neutral">sent to model</Badge>
                ) : (
                  <Badge tone="warn" title={it.notSentReason ?? ''}>
                    not sent
                  </Badge>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No stored filing or release was in the window.</p>
        )}
      </section>
      <p className="muted">
        Cost:{' '}
        {note.cost.amount ? (
          `${trimAmount(note.cost.amount)} ${note.cost.currency ?? ''}`
        ) : (
          <NoValue reason="NOT_APPLICABLE" detail="no model call" />
        )}
        {note.cost.promptTokens !== null ? ` · ${note.cost.promptTokens} prompt and ${note.cost.outputTokens ?? 0} output tokens` : ''} · note hash{' '}
        <ShortId value={note.contentHash} head={12} />
      </p>
    </div>
  );
}

export function NoteView({ noteId, navigate }: { noteId: string } & Pick<WorkspaceProps, 'navigate'>) {
  const q = usePluginQuery<unknown>(`/api/ai/notes/${encodeURIComponent(noteId)}`);
  const note = q.data === undefined ? null : parseNoteDetail(q.data);
  const [open, setOpen] = useState<string | null>(null);
  const cite = (id: string) => setOpen(id);
  const selected = note && open ? note.citations[open] : null;
  return (
    <WorkspaceFrame
      title={note?.title ?? 'AI note'}
      description="Advisory note: every line cites its source or is flagged."
      queries={[q]}
      actions={
        <a className="btn small" href={routeHref('ai')}>
          All notes
        </a>
      }
    >
      <QueryError what="The note" error={q.error} />
      {q.data !== undefined && note === null ? (
        <div className="callout" role="alert" style={{ ['--tone' as string]: 'var(--bad)' }}>
          The note response did not have the expected shape; nothing is shown rather than a guess.
        </div>
      ) : null}
      {note ? (
        <div className="ai-split">
          <NoteBody note={note} onCite={cite} />
          <aside className="ai-stack">
            {selected ? (
              <SourcePanel key={selected.id} citation={selected} navigate={navigate} />
            ) : (
              <p className="muted">Select a source id to see where a line comes from.</p>
            )}
            {open && !selected ? <p className="ai-bad">Source {open} is not part of this note.</p> : null}
          </aside>
        </div>
      ) : null}
    </WorkspaceFrame>
  );
}
