/**
 * Panel context: set by the workstation around each panel. Outside the workstation it is null and a workspace is
 * the page (its title is the page's h1#ws-title); inside a panel the title is an h2 with a panel-scoped id, so
 * several workspaces can share the page without duplicate ids or competing h1s.
 */
import { createContext, useContext } from 'react';

export interface PanelInfo {
  panelId: string;
  /** 1-based position shown to the owner ("Panel 2"). */
  number: number;
}

export const PanelContext = createContext<PanelInfo | null>(null);

export function usePanel(): PanelInfo | null {
  return useContext(PanelContext);
}

/** Id of the workspace title element: 'ws-title' for the page, `ws-title-<panel>` inside a panel. */
export function wsTitleId(panel: PanelInfo | null): string {
  return panel ? `ws-title-${panel.panelId}` : 'ws-title';
}
