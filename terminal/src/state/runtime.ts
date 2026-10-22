/** Wires the API client, state stream, query cache, command tracker and UI brokers together. */
import { ApiClient, defaultCommandId } from '../api/client';
import type { Endpoint, ParamsOf } from '../api/types';
import { AuthStore } from './auth';
import { CommandTracker } from './commands';
import { NoticeStore } from './notices';
import { QueryCache } from './queries';
import { RouterStore } from './router';
import { StepUpBroker } from './stepup';
import { StreamStore, type EventSourceFactory } from './stream';

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
    const v = stream.getSnapshot().version;
    if (v !== lastVersion) {
      lastVersion = v;
      queries.invalidateAll();
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
      stepUp.settle(false);
    }
  });

  return { client, stream, queries, commands, auth: authStore, notices, stepUp, router: new RouterStore() };
}
