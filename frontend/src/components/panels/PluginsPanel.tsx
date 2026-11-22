import { useCallback, useEffect, useState } from 'react';

import { api, ApiError } from '../../lib/api';
import { useStore } from '../../state/store';

/**
 * Plug-ins: MCP servers, the common plug-in format for AI tools.
 *
 * Paste the JSON a server's README gives (Claude Desktop's format works) or
 * fill in a command or address, and its tools become JARVIS tools.
 *
 * Every plug-in tool asks before it runs unless you mark the plug-in
 * trusted, and none run while you are away. Keys and tokens are encrypted
 * on disk, or written as ${NAME} to read them from your .env file, and are
 * never shown here again.
 */

interface Server {
  name: string;
  transport: 'stdio' | 'http' | '';
  command: string;
  url: string;
  enabled: boolean;
  trusted: boolean;
  state: string;
  error: string;
  server: { name?: string; version?: string };
  tools: { name: string; tool: string; description: string }[];
  skipped: { tool: string; reason: string }[];
  secrets: Record<string, string>;
}

const EXAMPLE = `{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/you/Documents"]
    }
  }
}`;

export function PluginsPanel() {
  const [servers, setServers] = useState<Server[]>([]);
  const [paste, setPaste] = useState('');
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [open, setOpen] = useState<string | null>(null);
  const notify = useStore((s) => s.notify);

  const load = useCallback(async () => {
    try {
      setServers((await api.get<{ servers: Server[] }>('/mcp')).servers);
    } catch (exc) {
      setError((exc as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
    // Servers connect in the background at start-up; look again shortly.
    const timer = window.setTimeout(() => void load(), 4000);
    return () => window.clearTimeout(timer);
  }, [load]);

  const add = async () => {
    setBusy('add');
    setError('');
    try {
      const result = await api.post<{ servers: Server[] }>('/mcp/import', { text: paste });
      const failed = result.servers.filter((s) => s.state === 'error');
      notify(failed.length
        ? `Added, but ${failed.map((s) => s.name).join(', ')} could not connect.`
        : `Connected ${result.servers.map((s) => s.name).join(', ')}.`);
      setPaste('');
      setAdding(false);
      await load();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy('');
    }
  };

  const change = async (server: Server, patch: { enabled?: boolean; trusted?: boolean }) => {
    setBusy(server.name);
    try {
      await api.patch(`/mcp/${server.name}`, patch);
      await load();
    } catch (exc) {
      notify((exc as Error).message);
    } finally {
      setBusy('');
    }
  };

  const reconnect = async (server: Server) => {
    setBusy(server.name);
    try {
      await api.post(`/mcp/${server.name}/reconnect`, {});
      await load();
    } finally {
      setBusy('');
    }
  };

  const remove = async (server: Server) => {
    if (!window.confirm(`Remove the plug-in “${server.name}”? Its tools go with it.`)) return;
    await api.del(`/mcp/${server.name}`);
    await load();
  };

  return (
    <>
      <div className="panel__section">
        <div className="control__help">
          Plug-ins use MCP, the common format for AI tools, so most tools built for other
          assistants work here. Each plug-in tool asks before running unless you trust that
          plug-in, and none run while you are away.
        </div>
        {!adding && (
          <button className="btn btn--small" onClick={() => setAdding(true)}>Add a plug-in</button>
        )}
        {adding && (
          <div className="control--stack">
            <div className="control__help">
              Paste the JSON from the plug-in's instructions. For a key or token, write
              <code> ${'{'}NAME{'}'}</code> to read it from your .env file, or paste it — it is
              encrypted before it is saved.
            </div>
            <textarea className="source__code" style={{ height: '12rem' }} value={paste}
                      placeholder={EXAMPLE} spellCheck={false}
                      onChange={(event) => setPaste(event.target.value)}
                      aria-label="Plug-in settings JSON" />
            {error && <div className="control__help control__help--bad">{error}</div>}
            <div className="control__buttons">
              <button className="btn btn--small btn--primary" disabled={!paste.trim() || busy === 'add'}
                      onClick={() => void add()}>
                {busy === 'add' ? 'Connecting…' : 'Add and connect'}
              </button>
              <button className="btn btn--small" onClick={() => { setAdding(false); setError(''); }}>
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>

      <div className="panel__section">
        {servers.length === 0 && <p className="empty">No plug-ins yet.</p>}
        {servers.map((server) => (
          <div className="plugin" key={server.name}>
            <div className="plugin__head">
              <span className={`wake-dot wake-dot--${server.state === 'connected' ? 'ok'
                : server.state === 'error' ? 'bad' : 'idle'}`} />
              <button className="plugin__name" onClick={() => setOpen(open === server.name ? null : server.name)}>
                {server.name}
              </button>
              <span className="row__meta">
                {server.state === 'connected' ? `${server.tools.length} tool(s)` : server.state}
              </span>
            </div>
            <div className="control__help">
              {server.transport === 'http' ? server.url : server.command}
            </div>
            {server.error && <div className="control__help control__help--bad">{server.error}</div>}

            {open === server.name && (
              <div className="plugin__detail">
                {server.tools.map((tool) => (
                  <div className="plugin__tool" key={tool.name}>
                    <span className="skill__name">{tool.tool}</span>
                    <span className="skill__description">{tool.description}</span>
                  </div>
                ))}
                {server.skipped.map((tool) => (
                  <div className="control__help control__help--bad" key={tool.tool}>
                    Not offered: {tool.tool} — {tool.reason}.
                  </div>
                ))}
                {Object.entries(server.secrets).map(([key, how]) => (
                  <div className="control__help" key={key}>{key}: {how}</div>
                ))}
              </div>
            )}

            <div className="plugin__switches">
              <label className="plugin__switch">
                <button className={`switch ${server.enabled ? 'switch--on' : ''}`} role="switch"
                        aria-checked={server.enabled} disabled={busy === server.name}
                        onClick={() => void change(server, { enabled: !server.enabled })} />
                <span>On</span>
              </label>
              <label className="plugin__switch"
                     title="Trusted plug-ins run without asking each time. They are still never run while you are away.">
                <button className={`switch ${server.trusted ? 'switch--on' : ''}`} role="switch"
                        aria-checked={server.trusted} disabled={busy === server.name}
                        onClick={() => void change(server, { trusted: !server.trusted })} />
                <span>Trusted (don't ask)</span>
              </label>
              <span style={{ flex: 1 }} />
              {server.enabled && (
                <button className="mini-action" disabled={busy === server.name}
                        onClick={() => void reconnect(server)}>
                  {busy === server.name ? 'Connecting…' : 'Reconnect'}
                </button>
              )}
              <button className="mini-action mini-action--danger" onClick={() => void remove(server)}>
                Remove
              </button>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
