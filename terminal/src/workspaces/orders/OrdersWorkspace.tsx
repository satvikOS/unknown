/**
 * Orders workspace (book Ch. 22: "Orders — intent, broker status, fills, cancel state; controlled
 * cancel"). Views, each addressable and bookmarkable:
 *   #/orders                          blotter (open / all, per session)
 *   #/orders/<order_id>               order detail: holds, outbox, executions, pretrade decision, timeline
 *   #/orders/~executions              executions (fills) with fee status
 *   #/orders/~new[/<instrument_id>]   order ticket for owner manual intents
 * Keys (when no field has focus and no dialog is open): n new order, l blotter, e executions.
 */
import { useEffect, useRef } from 'react';
import { routeHref } from '../../state/router';
import { charKeysEnabled, useCharKeys } from '../../ui/charKeys';
import { isTypingTarget } from '../../ui/keys';
import { Kbd } from '../../ui/primitives';
import type { WorkspaceProps } from '../types';
import { BlotterView } from './BlotterView';
import { ExecutionsView } from './ExecutionsView';
import { ordersRest, parseOrdersRoute, type OrdersRoute } from './model';
import { OrderDetailView } from './OrderDetailView';
import { useInstrumentMap } from './parts';
import { TicketView } from './TicketView';
import './orders.css';

type TabView = 'blotter' | 'executions' | 'ticket';

const TABS: { view: TabView; label: string; key: string; route: OrdersRoute }[] = [
  { view: 'blotter', label: 'Blotter', key: 'l', route: { view: 'blotter' } },
  { view: 'executions', label: 'Executions', key: 'e', route: { view: 'executions' } },
  { view: 'ticket', label: 'New order', key: 'n', route: { view: 'ticket', instrumentId: null } },
];

/** Single-key view shortcuts, ignored while typing, with a modifier, in a dialog, or after "g". */
function useViewKeys(go: (r: OrdersRoute) => void) {
  const goRef = useRef(go);
  goRef.current = go;
  useEffect(() => {
    let prev = { key: '', at: 0 };
    const onKey = (e: KeyboardEvent) => {
      const before = prev;
      prev = { key: e.key.toLowerCase(), at: Date.now() };
      if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented || e.repeat) return;
      if (isTypingTarget(e.target) || document.querySelector('.overlay') || !charKeysEnabled()) return;
      if (before.key === 'g' && prev.at - before.at < 1500) return; // second key of a "g x" sequence
      const tab = TABS.find((t) => t.key === prev.key);
      if (!tab) return;
      e.preventDefault();
      goRef.current(tab.route);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
}

/** View switcher shown under the workspace title. */
export function OrdersTabs({ active, orderId }: { active: TabView; orderId?: string | undefined }) {
  const charKeys = useCharKeys();
  return (
    <nav className="ord-tabs" aria-label="Orders views">
      {TABS.map((t) => (
        <a
          key={t.view}
          href={routeHref('orders', ...ordersRest(t.route))}
          aria-current={active === t.view && !orderId ? 'page' : undefined}
          data-active={active === t.view ? 'true' : undefined}
          className="ord-tab"
          aria-keyshortcuts={charKeys ? t.key : undefined}
        >
          {t.label} {charKeys ? <Kbd keys={t.key} /> : null}
        </a>
      ))}
      {orderId ? (
        <span className="ord-crumb" aria-current="page">
          <span aria-hidden="true">›</span> order <span className="cmd-id">{orderId}</span>
        </span>
      ) : null}
    </nav>
  );
}

export function OrdersWorkspace({ route, navigate }: WorkspaceProps) {
  const r = parseOrdersRoute(route.rest);
  const instruments = useInstrumentMap();
  useViewKeys((next) => navigate('orders', ...ordersRest(next)));

  // Moving between views inside the workspace: put focus on the new title (as the shell does between workspaces).
  const viewKey = r.view === 'detail' ? `detail:${r.orderId}` : r.view;
  const lastView = useRef<string | null>(null);
  useEffect(() => {
    if (lastView.current !== null && lastView.current !== viewKey) document.getElementById('ws-title')?.focus();
    lastView.current = viewKey;
  }, [viewKey]);

  switch (r.view) {
    case 'blotter':
      return <BlotterView tabs={<OrdersTabs active="blotter" />} instruments={instruments} onOpen={(id) => navigate('orders', id)} />;
    case 'executions':
      return <ExecutionsView tabs={<OrdersTabs active="executions" />} instruments={instruments} />;
    case 'ticket':
      return <TicketView tabs={<OrdersTabs active="ticket" />} instruments={instruments} presetInstrumentId={r.instrumentId} />;
    case 'detail':
      return (
        <OrderDetailView
          key={r.orderId}
          tabs={<OrdersTabs active="blotter" orderId={r.orderId} />}
          orderId={r.orderId}
          instruments={instruments}
          onBack={() => navigate('orders')}
        />
      );
  }
}
