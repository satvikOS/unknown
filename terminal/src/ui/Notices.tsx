/** Notice (toast) area. Errors persist until dismissed and are announced assertively. */
import { useNotices, useRuntime } from '../state/react';
import { Icon } from './Icon';

const TONE_TEXT = { info: 'Info', ok: 'Done', warn: 'Warning', error: 'Error' } as const;
const TONE_VAR = { info: 'var(--info)', ok: 'var(--ok)', warn: 'var(--warn)', error: 'var(--bad)' } as const;

export function Notices() {
  const { notices } = useRuntime();
  const items = useNotices();
  return (
    <div className="notices" aria-live="polite" aria-relevant="additions">
      {items.map((n) => (
        <div
          key={n.id}
          className="notice"
          role={n.tone === 'error' ? 'alert' : 'status'}
          style={{ ['--tone' as string]: TONE_VAR[n.tone] }}
        >
          <span className="title">
            <span className="sr-only">{TONE_TEXT[n.tone]}: </span>
            {n.title}
          </span>
          <button type="button" className="btn ghost icon-btn small" onClick={() => notices.dismiss(n.id)} aria-label={`Dismiss: ${n.title}`}>
            <Icon name="close" size={14} />
          </button>
          {n.detail ? <span className="detail">{n.detail}</span> : null}
          {n.commandId ? <span className="detail cmd-id">command {n.commandId}</span> : null}
        </div>
      ))}
    </div>
  );
}
