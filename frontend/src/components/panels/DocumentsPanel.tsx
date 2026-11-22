import { useCallback, useEffect, useState } from 'react';

import { api, ApiError } from '../../lib/api';

/**
 * Your documents, searchable by JARVIS.
 *
 * Add a folder and everything readable in it -- notes, PDFs, Word files,
 * text, code -- is indexed on this machine. Ask JARVIS about anything in
 * them and it answers from the passages it finds, naming the file.
 *
 * The search matches words, not meanings: it finds "boiler service" in a
 * note that says "boiler service", not one that only says "heating
 * maintenance". Nothing is uploaded anywhere.
 */

interface Folder {
  path: string;
  status: 'waiting' | 'indexing' | 'ready' | 'error';
  files: number;
  passages: number;
  indexed_at: string | null;
  error: string;
  running: boolean;
}

interface Hit {
  file: string;
  path: string;
  section: string;
  snippet: string;
}

export function DocumentsPanel() {
  const [folders, setFolders] = useState<Folder[]>([]);
  const [totals, setTotals] = useState({ files: 0, passages: 0, unreadable: 0 });
  const [path, setPath] = useState('');
  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await api.get<{ folders: Folder[]; totals: typeof totals }>('/documents');
      setFolders(data.folders);
      setTotals(data.totals);
    } catch (exc) {
      setError((exc as Error).message);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // While anything is being read, keep the counts moving.
  const working = folders.some((f) => f.running || f.status === 'indexing' || f.status === 'waiting');
  useEffect(() => {
    if (!working) return;
    const timer = window.setInterval(() => void load(), 2500);
    return () => window.clearInterval(timer);
  }, [working, load]);

  const add = async () => {
    setBusy(true);
    setError('');
    try {
      await api.post('/documents/folders', { path });
      setPath('');
      await load();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const search = async () => {
    if (!query.trim()) return;
    try {
      setHits((await api.get<{ results: Hit[] }>(
        `/documents/search?q=${encodeURIComponent(query)}&limit=8`)).results);
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  const remove = async (folder: Folder) => {
    if (!window.confirm(`Stop indexing ${folder.path}? Your files are not touched.`)) return;
    await api.del(`/documents/folders?path=${encodeURIComponent(folder.path)}`);
    await load();
  };

  return (
    <>
      <div className="panel__section">
        <div className="control__help">
          Add a folder and JARVIS can answer from what is in it — notes, PDFs, Word documents,
          text and code — and say which file each answer came from. Everything stays on this
          machine. Ask in chat, e.g. “what does my lease say about notice?”.
        </div>
        <div className="control" style={{ gap: '0.5rem' }}>
          <input className="text-input" style={{ flex: 1, maxWidth: 'none' }}
                 placeholder="A folder, e.g. C:\\Users\\you\\Documents\\Notes"
                 value={path} onChange={(event) => setPath(event.target.value)}
                 onKeyDown={(event) => { if (event.key === 'Enter' && path.trim()) void add(); }}
                 aria-label="Folder to index" />
          <button className="btn btn--small" disabled={busy || !path.trim()}
                  onClick={() => void add()}>{busy ? 'Adding…' : 'Add'}</button>
        </div>
        {error && <div className="control__help control__help--bad">{error}</div>}
        <div className="control__help">
          {totals.files} file(s) · {totals.passages.toLocaleString()} passages indexed
          {totals.unreadable > 0 && ` · ${totals.unreadable} could not be read`}
        </div>
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Folders</div>
        {folders.length === 0 && <p className="empty">No folders yet.</p>}
        {folders.map((folder) => (
          <div className="plugin" key={folder.path}>
            <div className="plugin__head">
              <span className={`wake-dot wake-dot--${folder.status === 'ready' ? 'ok'
                : folder.status === 'error' ? 'bad' : 'idle'}`} />
              <span className="plugin__name" style={{ overflowWrap: 'anywhere' }}>{folder.path}</span>
            </div>
            <div className="control__help">
              {folder.status === 'ready' && `${folder.files} file(s) · ${folder.passages} passages`}
              {(folder.status === 'indexing' || folder.running) && 'Reading files…'}
              {folder.status === 'waiting' && !folder.running && 'Waiting to start…'}
              {folder.status === 'error' && folder.error}
            </div>
            <div className="plugin__switches">
              <span style={{ flex: 1 }} />
              <button className="mini-action mini-action--danger" onClick={() => void remove(folder)}>
                Stop indexing
              </button>
            </div>
          </div>
        ))}
        {folders.length > 0 && (
          <button className="mini-action" onClick={async () => {
            await api.post('/documents/refresh', {});
            await load();
          }}>Check for changes now</button>
        )}
      </div>

      {totals.passages > 0 && (
        <div className="panel__section">
          <div className="panel__section-title">Try a search</div>
          <div className="control" style={{ gap: '0.5rem' }}>
            <input className="text-input" style={{ flex: 1, maxWidth: 'none' }}
                   placeholder="e.g. boiler warranty" value={query}
                   onChange={(event) => setQuery(event.target.value)}
                   onKeyDown={(event) => { if (event.key === 'Enter') void search(); }}
                   aria-label="Search your documents" />
            <button className="btn btn--small" onClick={() => void search()}>Search</button>
          </div>
          {hits && hits.length === 0 && <p className="empty">Nothing matched.</p>}
          {hits?.map((hit, index) => (
            <div className="doc-hit" key={`${hit.path}-${index}`}>
              <div className="doc-hit__file" title={hit.path}>
                {hit.file}{hit.section ? ` · ${hit.section}` : ''}
              </div>
              <div className="doc-hit__snippet">{highlight(hit.snippet)}</div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

/** The search engine marks matches with [ ]; show them as highlights. */
function highlight(snippet: string) {
  return snippet.split(/(\[[^\]]*\])/g).map((part, index) =>
    part.startsWith('[') && part.endsWith(']')
      ? <mark key={index} className="palette__hit">{part.slice(1, -1)}</mark>
      : <span key={index}>{part}</span>);
}
