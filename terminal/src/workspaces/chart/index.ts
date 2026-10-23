import type { WorkspaceDefinition } from '../types';

export const chartWorkspace: WorkspaceDefinition = {
  id: 'chart',
  title: 'Chart',
  icon: 'markets',
  shortcut: 'g h',
  order: 57,
  load: () => import('./ChartWorkspace').then((m) => m.ChartWorkspace),
  description: 'Price chart of the linked security: any configured bar source, studies, comparison, annotations, live last bar',
  keywords: ['chart', 'candles', 'bars', 'ohlc', 'studies', 'sma', 'ema', 'rsi', 'macd', 'bollinger', 'vwap', 'atr', 'compare', 'technical'],
  keys: [
    { keys: '1', action: 'Interval 1m (keys 2-8: 5m, 15m, 30m, 1h, 1d, 1w, 1mo)' },
    { keys: 'c', action: 'Next chart type (candles, OHLC, line, area)' },
    { keys: 'v', action: 'Show or hide volume' },
    { keys: 'h', action: 'Horizontal line at the crosshair price' },
    { keys: 'n', action: 'Note on the crosshair bar' },
    { keys: 'f', action: 'Fit all bars' },
  ],
};
