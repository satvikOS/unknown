/** Wires the API client, state stream, query cache, command tracker and UI brokers together. */
import { ApiClient, defaultCommandId } from '../api/client';
import type { Endpoint, ParamsOf } from '../api/types';
import { AuthStore } from './auth';
import { CommandTracker } from './commands';
import { NoticeStore } from './notices';
import { QueryCache, type InvalidationScope } from './queries';
import { RouterStore } from './router';
import { StepUpBroker } from './stepup';
import { StreamStore, type EventSourceFactory, type RefetchReason } from './stream';

export interface Runtime {
  client: ApiClient;
  stream: StreamStore;
  queries: QueryCache;
  commands: CommandTracker;
  auth: AuthStore;
  notices: NoticeStore;
  stepUp: StepUpBroker;
  router: RouterStore;
}

export interface RuntimeOptions {
  baseUrl?: string;
  createEventSource?: EventSourceFactory;
}

/**
 * Which queries a stream version bump refetches. A new seq or a gap means the core wrote events: only data in
 * its event log can have changed. A reset (restore), a reconnect (the API may have restarted) or a manual
 * refresh refetches everything visible, including data that is not seq-driven.
 */
export function invalidationScope(reason: RefetchReason | null): InvalidationScope {
  return reason === 'seq' || reason === 'gap' ? 'core' : 'all';
}

export function createRuntime(options: RuntimeOptions = {}): Runtime {
  const baseUrl = options.baseUrl ?? '';
  const stepUp = new StepUpBroker();
  const notices = new NoticeStore();
  // `auth` is assigned before any request can fail with 401.
  let auth: AuthStore | null = null;
  const client = new ApiClient({
    baseUrl,
    onUnauthorized: () => auth?.markUnauthenticated(),
    stepUp: stepUp.request,
  });
  auth = new AuthStore(client);
  const authStore = auth;

  const stream = new StreamStore({
    url: `${baseUrl}/api/stream`,
    ...(options.createEventSource ? { createEventSource: options.createEventSource } : {}),
    // A refused stream is usually an expired session: confirm before showing the login screen.
    onConnectionRefused: () => void authStore.check(),
  });

  const queries = new QueryCache({
    fetcher: (endpoint, params) => client.get(endpoint as Endpoint, params as ParamsOf<Endpoint>),
    currentSeq: () => stream.getSnapshot().seq,
  });

  let lastVersion = stream.getSnapshot().version;
  stream.subscribe(() => {
    const { version, lastReason } = stream.getSnapshot();
    if (version !== lastVersion) {
      lastVersion = version;
      queries.invalidate(invalidationScope(lastReason));
    }
  });

  const commands = new CommandTracker(
    client,
    {
      waitForSeq: (seq) => stream.waitForSeq(seq),
      refreshAll: () => stream.refreshAll(),
      isLive: () => stream.getSnapshot().status === 'live',
    },
    { newCommandId: defaultCommandId },
  );

  let lastStatus = authStore.getSnapshot().status;
  authStore.subscribe(() => {
    const s = authStore.getSnapshot().status;
    if (s === lastStatus) return;
    lastStatus = s;
    if (s === 'authenticated') {
      stream.start();
    } else if (s === 'unauthenticated') {
      stream.stop();
      queries.clear();
      client.forgetValidated();
      stepUp.settle(false);
    }
  });

  return { client, stream, queries, commands, auth: authStore, notices, stepUp, router: new RouterStore() };
}
