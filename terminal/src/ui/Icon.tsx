/** Small original stroke icons (16px). Decorative: always paired with visible text. */

export type IconName =
  | 'overview'
  | 'sessions'
  | 'orders'
  | 'risk'
  | 'operations'
  | 'audit'
  | 'policy'
  | 'instruments'
  | 'capabilities'
  | 'markets'
  | 'research'
  | 'agents'
  | 'generic'
  | 'workstation'
  | 'search'
  | 'sun'
  | 'moon'
  | 'auto'
  | 'refresh'
  | 'close'
  | 'logout';

const PATHS: Record<IconName, string> = {
  workstation: 'M2 2.5h5.5v5H2zM8.5 2.5H14v5H8.5zM2 8.5h12v5H2z',
  overview: 'M2.5 2.5h4.5v5H2.5zM9 2.5h4.5v3H9zM9 7.5h4.5v6H9zM2.5 9.5h4.5v4H2.5z',
  sessions: 'M2.5 4h11M2.5 8h11M2.5 12h7M12 10.5l1.8 1.5L12 13.5',
  orders: 'M3 3.5h10M3 6.5h10M3 9.5h6M3 12.5h6M11 9v4.5M9.5 11.5l1.5 1.8 1.5-1.8',
  risk: 'M8 2l5.5 2v4c0 3-2.4 5.2-5.5 6-3.1-.8-5.5-3-5.5-6V4z M8 5.5v3.5M8 11v.5',
  operations: 'M8 5.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4',
  audit: 'M4 2h6l2.5 2.5V14H4zM10 2v2.5h2.5M6 7.5h4.5M6 10h4.5',
  policy: 'M3.5 2.5h9v11h-9zM5.5 5.5h5M5.5 8h5M5.5 10.5h3',
  instruments: 'M2.5 12.5l3-4 3 2 5-6.5M2.5 2.5v11h11',
  capabilities: 'M2.5 3.5h3v3h-3zM2.5 9.5h3v3h-3zM7.5 5h6M7.5 11h6',
  markets: 'M2.5 13.5V9M6 13.5V5.5M9.5 13.5V7.5M13 13.5V3',
  research: 'M7 2.5a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9zM10.3 10.3l3.2 3.2',
  agents: 'M5 6.5h6v6H5zM8 3v3.5M6.5 9h.01M9.5 9h.01M3 9.5h2M11 9.5h2',
  generic: 'M3 3h10v10H3z',
  search: 'M7 2.5a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9zM10.3 10.3l3.2 3.2',
  sun: 'M8 5a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3 3l1 1M12 12l1 1M3 13l1-1M12 4l1-1',
  moon: 'M12.5 10A5.5 5.5 0 0 1 6 3.5a5.5 5.5 0 1 0 6.5 6.5z',
  auto: 'M8 2.5a5.5 5.5 0 1 0 0 11zM8 2.5a5.5 5.5 0 0 1 0 11',
  refresh: 'M13 4.5V2M13 4.5h-2.5M12.6 4.3A5.5 5.5 0 1 0 13.5 8',
  close: 'M4 4l8 8M12 4l-8 8',
  logout: 'M6.5 2.5h-4v11h4M10 5l3 3-3 3M13 8H6',
};

export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
