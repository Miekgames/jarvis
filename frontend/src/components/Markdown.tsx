import { Fragment, ReactNode, useState } from 'react';

/**
 * Replies, rendered.
 *
 * The model writes markdown — headings, bold, bullet lists, fenced code —
 * and until now every one of those arrived on screen as literal characters.
 * `voice.ts` has always stripped the same syntax before speaking, so the
 * assistant has been careful not to *say* "asterisk asterisk" while showing
 * it all along.
 *
 * **Rendered to React elements, never to HTML.** This is model output: text
 * that can be influenced by a web page it read or a file it opened. Building
 * an HTML string and handing it to `dangerouslySetInnerHTML` would turn
 * every reply into an injection surface, and sanitising your way back out of
 * that is a game nobody wins. Producing elements directly means a `<script>`
 * in the text is characters, because there is no HTML parser anywhere in
 * the path.
 *
 * Deliberately a subset. Code blocks, inline code, bold, italic, headings
 * and lists are what actually turns up in replies. Raw HTML, images and
 * links are not rendered — the first two have no business here, and a link
 * the model was talked into writing is exactly the thing not to make
 * clickable.
 */

interface Block {
  kind: 'code' | 'heading' | 'list' | 'quote' | 'paragraph';
  text: string;
  language?: string;
  ordered?: boolean;
  level?: number;
  items?: string[];
}

export function Markdown({ text }: { text: string }) {
  if (!text) return null;
  const blocks = parse(text);

  return (
    <>
      {blocks.map((block, index) => {
        switch (block.kind) {
          case 'code':
            return <CodeBlock key={index} code={block.text} language={block.language} />;

          case 'heading': {
            const level = Math.min(block.level ?? 2, 4);
            return (
              <div key={index} className={`md-h md-h--${level}`}>
                {inline(block.text)}
              </div>
            );
          }

          case 'list':
            return (
              <ul key={index} className={`md-list ${block.ordered ? 'md-list--ordered' : ''}`}>
                {(block.items ?? []).map((item, i) => (
                  <li key={i}>
                    {block.ordered && <span className="md-list__n">{i + 1}.</span>}
                    {inline(item)}
                  </li>
                ))}
              </ul>
            );

          case 'quote':
            return <div key={index} className="md-quote">{inline(block.text)}</div>;

          default:
            return <p key={index} className="md-p">{inline(block.text)}</p>;
        }
      })}
    </>
  );
}

// ---------------------------------------------------------------------------
function CodeBlock({ code, language }: { code: string; language?: string }) {
  const [copied, setCopied] = useState(false);
  const [running, setRunning] = useState(false);
  const page = isPage(code, language);

  const copy = async () => {
    if (await copyText(code)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    }
  };

  return (
    <div className="md-code">
      <div className="md-code__bar">
        <span className="md-code__lang">{language || 'code'}</span>
        <span className="md-code__actions">
          {page && (
            <button onClick={() => setRunning(!running)}
                    title={running ? 'Close the preview' : 'Run this page in a sealed preview'}>
              {running ? 'close' : 'run'}
            </button>
          )}
          <button onClick={() => saveFile(fileName(language, code), code)}
                  title="Save this code as a file">
            save
          </button>
          <button onClick={() => void copy()} title="Copy this code">
            {copied ? 'copied' : 'copy'}
          </button>
        </span>
      </div>
      {running && <Preview code={code} />}
      <pre><code>{code}</code></pre>
    </div>
  );
}

/**
 * A page JARVIS wrote, running -- sealed off from JARVIS itself.
 *
 * This is model output, and it is about to be executed, so it gets the
 * strongest fence a browser offers rather than the convenient one:
 *
 *   * `sandbox` without `allow-same-origin`. The page runs in an origin of
 *     its own that matches nothing, so it cannot read this page, its
 *     cookies, or JARVIS's saved settings, and it cannot call the server as
 *     you. No popups, no navigating the window it sits in.
 *   * A content policy with no network at all, so it cannot load anything
 *     or send anything anywhere either.
 *
 * A sealed origin has no storage, and a lot of small apps reach for
 * localStorage on their first line and stop dead when it throws. So it is
 * handed an in-memory one: the app works, and what it saves lasts as long
 * as the preview does, which the label says.
 */
function Preview({ code }: { code: string }) {
  return (
    <div className="md-preview">
      <iframe
        title="Preview of the page in this reply"
        sandbox="allow-scripts allow-modals"
        srcDoc={sealed(code)}
        referrerPolicy="no-referrer"
      />
      <div className="md-preview__note">
        Running in a sealed preview: no network, no access to JARVIS, and nothing
        it saves is kept once this is closed.
      </div>
    </div>
  );
}

const PREVIEW_POLICY = [
  "default-src 'none'",
  "script-src 'unsafe-inline' 'unsafe-eval'",
  "style-src 'unsafe-inline'",
  'img-src data: blob:',
  'media-src data: blob:',
  'font-src data:',
  "connect-src 'none'",
  "form-action 'none'",
].join('; ');

// A Proxy rather than a plain object, so that it behaves like the real
// thing to the code that lists what was saved: `Object.keys(localStorage)`
// gives the saved keys, not the names of these methods, and
// `localStorage.count = 3` saves.
const MEMORY_STORAGE = `<script>
(() => {
  const make = () => {
    const data = new Map();
    const api = {
      getItem: (k) => (data.has(String(k)) ? data.get(String(k)) : null),
      setItem: (k, v) => { data.set(String(k), String(v)); },
      removeItem: (k) => { data.delete(String(k)); },
      clear: () => { data.clear(); },
      key: (i) => Array.from(data.keys())[i] ?? null,
    };
    return new Proxy(api, {
      get: (t, n) => n === 'length' ? data.size
        : (n in t ? t[n] : (typeof n === 'string' && data.has(n) ? data.get(n) : undefined)),
      set: (t, n, v) => { data.set(String(n), String(v)); return true; },
      deleteProperty: (t, n) => { data.delete(String(n)); return true; },
      has: (t, n) => n in t || n === 'length' || data.has(String(n)),
      ownKeys: () => Array.from(data.keys()),
      getOwnPropertyDescriptor: (t, n) => data.has(String(n))
        ? { value: data.get(String(n)), enumerable: true, configurable: true, writable: true }
        : undefined,
    });
  };
  for (const name of ['localStorage', 'sessionStorage']) {
    try { Object.defineProperty(window, name, { value: make(), configurable: true }); } catch (e) {}
  }
})();
</script>`;

/** The page with its fence put in first, before any of its own code runs. */
function sealed(code: string): string {
  const fence = `<meta http-equiv="Content-Security-Policy" content="${PREVIEW_POLICY}">\n`
    + MEMORY_STORAGE;
  const head = code.match(/<head[^>]*>/i);
  if (head && head.index !== undefined) {
    const at = head.index + head[0].length;
    return code.slice(0, at) + '\n' + fence + code.slice(at);
  }
  const html = code.match(/<html[^>]*>/i);
  if (html && html.index !== undefined) {
    const at = html.index + html[0].length;
    return code.slice(0, at) + '\n<head>' + fence + '</head>' + code.slice(at);
  }
  // A fragment -- some markup and a script, no document around it.
  return `<!DOCTYPE html><html><head>${fence}</head><body>${code}</body></html>`;
}

/** Whether a block is a page that can be shown, rather than code to read. */
function isPage(code: string, language?: string): boolean {
  const lang = (language ?? '').toLowerCase();
  if (lang === 'html' || lang === 'htm' || lang === 'xhtml') return true;
  return /^\s*(<!doctype html|<html[\s>])/i.test(code);
}

const EXTENSIONS: Record<string, string> = {
  html: 'html', htm: 'html', xhtml: 'html', css: 'css',
  js: 'js', javascript: 'js', jsx: 'jsx', ts: 'ts', typescript: 'ts', tsx: 'tsx',
  py: 'py', python: 'py', json: 'json', sh: 'sh', bash: 'sh', shell: 'sh',
  powershell: 'ps1', ps1: 'ps1', sql: 'sql', yaml: 'yml', yml: 'yml',
  md: 'md', markdown: 'md', c: 'c', cpp: 'cpp', java: 'java', go: 'go',
  rust: 'rs', rs: 'rs', ruby: 'rb', php: 'php', xml: 'xml', csv: 'csv',
};

function fileName(language: string | undefined, code: string): string {
  const lang = (language ?? '').toLowerCase();
  const extension = EXTENSIONS[lang] ?? (isPage(code, lang) ? 'html' : 'txt');
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `jarvis-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
    + `-${pad(now.getHours())}${pad(now.getMinutes())}.${extension}`;
}

/** Hand the browser a file to save, made here rather than on the server. */
function saveFile(name: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

// ---------------------------------------------------------------------------
// block parsing
// ---------------------------------------------------------------------------
function parse(text: string): Block[] {
  const lines = text.replace(/\r\n/g, '\n').split('\n');
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  let list: { items: string[]; ordered: boolean } | null = null;

  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push({ kind: 'paragraph', text: paragraph.join(' ').trim() });
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list) {
      blocks.push({ kind: 'list', text: '', items: list.items, ordered: list.ordered });
      list = null;
    }
  };
  const flush = () => { flushParagraph(); flushList(); };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];

    // Fenced code. Consumed whole, so nothing inside is interpreted —
    // markdown inside a code block is code, not markdown.
    const fence = line.match(/^\s*```+\s*(\S*)\s*$/);
    if (fence) {
      flush();
      const language = fence[1] || '';
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !/^\s*```+\s*$/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      blocks.push({ kind: 'code', text: body.join('\n'), language });
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flush();
      blocks.push({ kind: 'heading', text: heading[2], level: heading[1].length });
      continue;
    }

    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      flush();
      blocks.push({ kind: 'quote', text: quote[1] });
      continue;
    }

    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      flushParagraph();
      const ordered = Boolean(numbered);
      const item = (bullet ? bullet[1] : numbered![1]).trim();
      if (list && list.ordered === ordered) {
        list.items.push(item);
      } else {
        flushList();
        list = { items: [item], ordered };
      }
      continue;
    }

    if (!line.trim()) {
      flush();
      continue;
    }

    flushList();
    paragraph.push(line.trim());
  }

  flush();
  return blocks;
}

// ---------------------------------------------------------------------------
// inline parsing
// ---------------------------------------------------------------------------
/**
 * Inline spans, as React nodes.
 *
 * Code first, so `**` inside backticks stays literal — otherwise a snippet
 * containing markdown syntax renders as formatting rather than as the code
 * it is.
 */
function inline(text: string): ReactNode {
  const nodes: ReactNode[] = [];
  let remaining = text;
  let key = 0;

  const pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\n]+\*)|(_[^_\n]+_)/;

  while (remaining) {
    const match = remaining.match(pattern);
    if (!match || match.index === undefined) {
      nodes.push(<Fragment key={key++}>{remaining}</Fragment>);
      break;
    }

    if (match.index > 0) {
      nodes.push(<Fragment key={key++}>{remaining.slice(0, match.index)}</Fragment>);
    }

    const token = match[0];
    if (token.startsWith('`')) {
      nodes.push(<code className="md-inline" key={key++}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith('**') || token.startsWith('__')) {
      nodes.push(<strong key={key++}>{token.slice(2, -2)}</strong>);
    } else {
      nodes.push(<em key={key++}>{token.slice(1, -1)}</em>);
    }

    remaining = remaining.slice(match.index + token.length);
  }

  return nodes;
}

// ---------------------------------------------------------------------------
/** Shared with the conversation: works without a secure context. */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through */
  }
  try {
    const scratch = document.createElement('textarea');
    scratch.value = text;
    scratch.setAttribute('readonly', '');
    scratch.style.position = 'fixed';
    scratch.style.top = '-1000px';
    document.body.appendChild(scratch);
    scratch.select();
    const worked = document.execCommand('copy');
    document.body.removeChild(scratch);
    return worked;
  } catch {
    return false;
  }
}
