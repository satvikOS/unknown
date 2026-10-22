/**
 * Rendered views of the research workspace (react-dom/server, no browser): what the owner reads, checked as text.
 */
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { QueryResult } from '../../state/react';
import type { Envelope } from './model';
import { EnvelopeView } from './parts';

function result<T>(data: T): QueryResult<T> {
  return { status: 'success', data, error: null, fetching: false, fetchedAtMs: 0, seqAtFetch: 0, key: 'k', refetch: () => undefined };
}

describe('research envelope without a registry', () => {
  it('shows the API reason verbatim and a study command that runs (with its required --out)', () => {
    // api/routes/research.py, verbatim.
    const reason = 'research registry not found at /h/research/experiments.sqlite; record an experiment or run `python -m unknown.research.study` first';
    const env: Envelope<unknown[]> = { available: false, data: null, unavailable_reason: reason, provenance: { source: 'research registry' } };
    const html = renderToStaticMarkup(createElement(EnvelopeView<unknown[]>, { q: result(env), what: 'Experiments', children: () => 'data' }));
    expect(html).toContain('research registry not found at /h/research/experiments.sqlite; record an experiment or run `python -m unknown.research.study` first');
    const commands = [...html.matchAll(/<code class="mono rx-command">([^<]*)<\/code>/g)].map((m) => m[1]);
    // `python -m unknown.research.study` alone exits with "the following arguments are required: --out".
    expect(commands).toEqual(['python -m unknown.research.study --out &lt;DIR&gt;']);
  });
});
