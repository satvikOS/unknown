import type { WorkspaceDefinition } from '../types';
import { CompanyWorkspace } from './CompanyWorkspace';

export const companyWorkspace: WorkspaceDefinition = {
  id: 'company',
  title: 'Company',
  icon: 'policy',
  shortcut: 'g e',
  order: 110,
  component: CompanyWorkspace,
  description: 'Issuer search, point-in-time statements, metrics, DCF, peers and tearsheet (SEC filings)',
  keywords: ['issuer', 'equity', 'fundamentals', 'sec', 'edgar', 'cik', 'statements', 'ratios', 'valuation', 'dcf', 'peers', 'tearsheet', 'filings'],
  commands: [{ id: 'search', label: 'Search issuers', path: ['search'], keywords: 'company issuer cik ticker' }],
};
