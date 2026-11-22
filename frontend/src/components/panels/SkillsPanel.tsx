import { useCallback, useEffect, useState } from 'react';

import { api, ApiError } from '../../lib/api';
import { useStore } from '../../state/store';

/**
 * Skills: written know-how JARVIS picks up when a request calls for it.
 *
 * Each is a SKILL.md -- a name, a one-line "when to use this", and plain
 * instructions -- in the agentskills.io format, so skills written for other
 * assistants work here too. JARVIS reads one when your request matches it,
 * or straight away when you start a message with /its-name.
 *
 * Imported skills are screened as they come in: one whose instructions try
 * to override JARVIS's rules is saved switched off, with the reason shown,
 * and runs only if you turn it on.
 */

interface Skill {
  name: string;
  description: string;
  tier: 'bundled' | 'mine' | 'imported';
  enabled: boolean;
  editable: boolean;
  origin: string;
  flagged: string[];
  lines: number;
  body?: string;
  text?: string;
}

type Mode = { kind: 'list' } | { kind: 'view'; name: string } | { kind: 'new' } | { kind: 'import' };

const TIER_LABEL: Record<string, string> = {
  bundled: 'built in', mine: 'yours', imported: 'imported',
};

export function SkillsPanel() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [mode, setMode] = useState<Mode>({ kind: 'list' });
  const [error, setError] = useState('');
  const notify = useStore((s) => s.notify);

  const load = useCallback(async () => {
    try {
      setSkills((await api.get<{ skills: Skill[] }>('/skills')).skills);
    } catch (exc) {
      setError((exc as Error).message);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const toggle = async (skill: Skill) => {
    try {
      await api.patch(`/skills/${skill.name}`, { enabled: !skill.enabled });
      await load();
    } catch (exc) {
      notify((exc as Error).message);
    }
  };

  if (mode.kind === 'view') {
    return <SkillDetail name={mode.name} onBack={() => { setMode({ kind: 'list' }); void load(); }} />;
  }
  if (mode.kind === 'new') {
    return <NewSkill onDone={(name) => { void load(); setMode(name ? { kind: 'view', name } : { kind: 'list' }); }} />;
  }
  if (mode.kind === 'import') {
    return <ImportSkill onDone={(name) => { void load(); setMode(name ? { kind: 'view', name } : { kind: 'list' }); }} />;
  }

  return (
    <>
      <div className="panel__section">
        <div className="control__help">
          JARVIS reads a skill when your request fits it. To use one directly, start your
          message with its name — for example <code>/meeting-notes</code> followed by your notes.
        </div>
        <div className="control__buttons">
          <button className="btn btn--small" onClick={() => setMode({ kind: 'new' })}>Write a skill</button>
          <button className="btn btn--small" onClick={() => setMode({ kind: 'import' })}>Import</button>
        </div>
        {error && <div className="control__help control__help--bad">{error}</div>}
      </div>

      <div className="panel__section">
        {skills.length === 0 && !error && <p className="empty">No skills yet.</p>}
        {skills.map((skill) => (
          <div className={`skill ${skill.enabled ? '' : 'skill--off'}`} key={skill.name}>
            <button className="skill__main" onClick={() => setMode({ kind: 'view', name: skill.name })}>
              <span className="skill__name">/{skill.name}</span>
              <span className="skill__description">{skill.description}</span>
              <span className="skill__meta">
                {TIER_LABEL[skill.tier] ?? skill.tier}
                {skill.flagged.length > 0 && ' · flagged when imported'}
              </span>
            </button>
            <button className={`switch ${skill.enabled ? 'switch--on' : ''}`}
                    role="switch" aria-checked={skill.enabled}
                    aria-label={`${skill.enabled ? 'Switch off' : 'Switch on'} ${skill.name}`}
                    onClick={() => void toggle(skill)} />
          </div>
        ))}
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
function SkillDetail({ name, onBack }: { name: string; onBack: () => void }) {
  const [skill, setSkill] = useState<Skill | null>(null);
  const [text, setText] = useState('');
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const notify = useStore((s) => s.notify);

  useEffect(() => {
    api.get<Skill>(`/skills/${name}`)
      .then((loaded) => { setSkill(loaded); setText(loaded.text ?? ''); })
      .catch((exc) => setError((exc as Error).message));
  }, [name]);

  const save = async () => {
    setBusy(true);
    setError('');
    try {
      const saved = await api.put<Skill>(`/skills/${name}`, { text });
      setSkill(saved);
      setEditing(false);
      notify(saved.enabled ? 'Skill saved.' : 'Saved, but switched off: it was flagged.');
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!window.confirm(`Delete the skill “${name}”? This cannot be undone.`)) return;
    try {
      await api.del(`/skills/${name}`);
      onBack();
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  return (
    <div className="panel__section">
      <button className="mini-action" onClick={onBack}>← All skills</button>
      {error && <div className="control__help control__help--bad">{error}</div>}
      {skill && (
        <>
          <div className="control__label">/{skill.name}</div>
          <div className="control__help">{skill.description}</div>
          <div className="control__help">
            {TIER_LABEL[skill.tier]}{skill.origin ? ` · from ${skill.origin}` : ''}
            {' · '}{skill.enabled ? 'on' : 'off'}
          </div>
          {skill.flagged.length > 0 && (
            <div className="app__wrong">
              <div>Flagged when it was imported, so it was saved switched off:</div>
              {skill.flagged.map((reason) => (
                <div className="app__wrong-item" key={reason}>it {reason}</div>
              ))}
            </div>
          )}
          {editing ? (
            <textarea className="source__code" value={text} spellCheck={false}
                      onChange={(event) => setText(event.target.value)}
                      aria-label="The skill's SKILL.md" />
          ) : (
            <pre className="skill__body">{skill.body}</pre>
          )}
          <div className="control__buttons">
            {skill.editable && !editing && (
              <button className="btn btn--small" onClick={() => setEditing(true)}>Edit</button>
            )}
            {editing && (
              <>
                <button className="btn btn--small btn--primary" disabled={busy}
                        onClick={() => void save()}>{busy ? 'Saving…' : 'Save'}</button>
                <button className="btn btn--small" onClick={() => {
                  setEditing(false); setText(skill.text ?? '');
                }}>Cancel</button>
              </>
            )}
            {skill.editable && !editing && (
              <button className="btn btn--small btn--danger" onClick={() => void remove()}>Delete</button>
            )}
          </div>
          {!skill.editable && (
            <div className="control__help">
              Built-in skills can be switched off but not edited. To change one, write your own
              skill with the same name — yours takes its place.
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
function NewSkill({ onDone }: { onDone: (name?: string) => void }) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [instructions, setInstructions] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const create = async () => {
    setBusy(true);
    setError('');
    try {
      const saved = await api.post<Skill>('/skills', { name, description, instructions });
      onDone(saved.name);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel__section control--stack">
      <button className="mini-action" onClick={() => onDone()}>← All skills</button>
      <div className="control__label">Write a skill</div>
      <input className="text-input" style={{ maxWidth: 'none', width: '100%' }} placeholder="Name, e.g. weekly-review" value={name}
             onChange={(event) => setName(event.target.value)} aria-label="Skill name" />
      <input className="text-input" style={{ maxWidth: 'none', width: '100%' }} placeholder="When to use it, in one sentence" value={description}
             onChange={(event) => setDescription(event.target.value)} aria-label="Skill description" />
      <textarea className="away__prompt" rows={10} value={instructions}
                placeholder={'The steps, in plain words.\n\n1. First…\n2. Then…'}
                onChange={(event) => setInstructions(event.target.value)}
                aria-label="Skill instructions" />
      {error && <div className="control__help control__help--bad">{error}</div>}
      <button className="btn btn--primary" disabled={busy || !name.trim() || !description.trim()
                                                   || !instructions.trim()}
              onClick={() => void create()}>
        {busy ? 'Saving…' : 'Save skill'}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
function ImportSkill({ onDone }: { onDone: (name?: string) => void }) {
  const [url, setUrl] = useState('');
  const [text, setText] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const bring = async () => {
    setBusy(true);
    setError('');
    try {
      const saved = await api.post<Skill>('/skills/import', { url, text });
      onDone(saved.name);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel__section control--stack">
      <button className="mini-action" onClick={() => onDone()}>← All skills</button>
      <div className="control__label">Import a skill</div>
      <div className="control__help">
        Give the address of a SKILL.md (a GitHub link works), or paste one. It is checked as it
        comes in, and one whose instructions try to override JARVIS's rules is kept switched off.
      </div>
      <input className="text-input" style={{ maxWidth: 'none', width: '100%' }} placeholder="https://github.com/…/SKILL.md" value={url}
             onChange={(event) => setUrl(event.target.value)} aria-label="Skill address" />
      <div className="control__label control__label--small">or paste it</div>
      <textarea className="away__prompt" rows={8} value={text}
                placeholder={'---\nname: my-skill\ndescription: When to use it\n---\n\nInstructions…'}
                onChange={(event) => setText(event.target.value)} aria-label="SKILL.md text" />
      {error && <div className="control__help control__help--bad">{error}</div>}
      <button className="btn btn--primary" disabled={busy || (!url.trim() && !text.trim())}
              onClick={() => void bring()}>
        {busy ? 'Importing…' : 'Import'}
      </button>
    </div>
  );
}
