/**
 * News tape export (ui/export.ts). Publisher fields (title, summary, link, form, accession) are licensed per row by
 * the news source's rights record (`news:<source_id>` alias); the platform's own fields (item id, first seen) are
 * owner data. Headlines are untrusted text: formula-like titles are neutralised by the export primitive.
 */
import { type ExportColumn, type ExportSource, type ExportTable } from '../../ui/export';
import type { NewsItem } from './model';

const src = (it: NewsItem): ExportSource => ({ kind: 'licensed', source: `news:${it.source_id}` });

export function newsExport(rows: readonly NewsItem[], asOf: string | null, filters: Record<string, string>): ExportTable<NewsItem> {
  const columns: ExportColumn<NewsItem>[] = [
    { id: 'id', header: 'Item', kind: 'decimal', value: (it) => String(it.id) },
    { id: 'first_seen', header: 'First seen', kind: 'datetime', value: (it) => it.first_seen },
    { id: 'source', header: 'Source', value: (it) => it.source_id },
    { id: 'published', header: 'Published', kind: 'datetime', value: (it) => it.published, reason: () => 'NOT_REPORTED', source: src },
    { id: 'form', header: 'Form', value: (it) => it.form, reason: () => 'NOT_APPLICABLE', source: src },
    { id: 'accession', header: 'Accession', value: (it) => it.accession, reason: () => 'NOT_APPLICABLE', source: src },
    { id: 'title', header: 'Title', value: (it) => it.title, source: src },
    { id: 'link', header: 'Link', value: (it) => it.link, reason: () => 'NOT_REPORTED', source: src },
    {
      id: 'issuers',
      header: 'Issuer CIKs',
      value: (it) => it.entities.filter((e) => e.scheme === 'SEC_CIK').map((e) => e.value).join(' ') || null,
      reason: () => 'NOT_REPORTED',
      source: src,
    },
    { id: 'duplicate_of', header: 'Duplicate of', kind: 'decimal', value: (it) => (it.duplicate_of === null ? null : String(it.duplicate_of)), reason: () => 'NOT_APPLICABLE' },
  ];
  return {
    title: 'News tape',
    rows,
    columns,
    provenance: {
      source: 'News & filings tape: GET /api/news (the local news store written by the news poller)',
      asOf,
      asOfReason: 'NOT_REPORTED',
      filters,
      notes: ['headlines and summaries are publisher text, exported as inert text'],
    },
  };
}
