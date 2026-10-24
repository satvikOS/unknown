/**
 * Header widget registry: other branches add a ticker tape or similar strip under the header without editing the
 * shell. Widgets render in `order`, each inside its own error boundary (a failing widget shows one line, the
 * header keeps working). A widget labels its own data type and shows only real data (ui/unavailable.tsx for the rest). Nothing renders
 * while the registry is empty.
 */
import { Component, useSyncExternalStore, type ComponentType, type ReactNode } from 'react';

export interface HeaderWidget {
  /** Unique id; registering the same id again replaces the widget. */
  id: string;
  /** Accessible name of the widget's region, e.g. "Ticker tape". */
  label: string;
  component: ComponentType;
  /** Ascending (default 100). */
  order?: number;
}

let widgets: readonly HeaderWidget[] = [];
const listeners = new Set<() => void>();

function set(next: readonly HeaderWidget[]): void {
  widgets = [...next].sort((a, b) => (a.order ?? 100) - (b.order ?? 100) || a.id.localeCompare(b.id));
  for (const l of [...listeners]) l();
}

/** Register a header widget; returns a function that removes it. */
export function registerHeaderWidget(w: HeaderWidget): () => void {
  set([...widgets.filter((x) => x.id !== w.id), w]);
  return () => {
    if (widgets.includes(w)) set(widgets.filter((x) => x !== w));
  };
}

export function headerWidgets(): readonly HeaderWidget[] {
  return widgets;
}

export function useHeaderWidgets(): readonly HeaderWidget[] {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    headerWidgets,
    headerWidgets,
  );
}

class WidgetBoundary extends Component<{ label: string; children: ReactNode }, { failed: boolean }> {
  override state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  override render(): ReactNode {
    return this.state.failed ? <span className="muted">{this.props.label} could not be shown.</span> : this.props.children;
  }
}

/** The strip under the header; empty registry renders nothing. */
export function HeaderWidgetStrip() {
  const list = useHeaderWidgets();
  if (!list.length) return null;
  return (
    <div className="header-widgets">
      {list.map((w) => (
        <section key={w.id} className="header-widget" aria-label={w.label}>
          <WidgetBoundary label={w.label}>
            <w.component />
          </WidgetBoundary>
        </section>
      ))}
    </div>
  );
}
