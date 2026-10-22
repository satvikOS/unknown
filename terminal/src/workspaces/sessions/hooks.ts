/** React bindings for the sessions workspace: the per-session command log and the send path. */
import { useCallback, useSyncExternalStore } from 'react';
import { defaultCommandId } from '../../api/client';
import type { CommandKind } from '../../api/types';
import type { TrackedCommand } from '../../state/commands';
import { useCommands, useSendCommand } from '../../state/react';
import { sessionCommandLog, type SessionCommandRef } from './commandLog';

export function useSessionCommandLog(): readonly SessionCommandRef[] {
  return useSyncExternalStore(sessionCommandLog.subscribe, sessionCommandLog.getSnapshot);
}

export type SessionSend = (
  kind: CommandKind,
  payload: Record<string, unknown>,
  label: string,
  sessionId: string | null,
) => Promise<TrackedCommand>;

/**
 * Send a session command through the shared tracker. The command id is generated first and recorded
 * against the session, so the page shows it while the command is still `sending`.
 */
export function useSessionSend(): SessionSend {
  const send = useSendCommand();
  return useCallback<SessionSend>(
    async (kind, payload, label, sessionId) => {
      const commandId = defaultCommandId(kind);
      sessionCommandLog.record({ commandId, kind, sessionId, atMs: Date.now() });
      return send(kind, payload, { label, commandId });
    },
    [send],
  );
}

/** Latest command this browser sent for a session, with its live tracked status. */
export function useLatestSessionCommand(sessionId: string | null): { ref: SessionCommandRef | undefined; tracked: TrackedCommand | undefined } {
  const log = useSessionCommandLog();
  const commands = useCommands();
  const ref = sessionId ? log.find((r) => r.sessionId === sessionId) : undefined;
  const tracked = ref ? commands.find((c) => c.commandId === ref.commandId) : undefined;
  return { ref, tracked };
}
