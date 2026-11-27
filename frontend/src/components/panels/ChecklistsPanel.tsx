import { useCallback, useEffect, useState } from 'react';

import { api, ApiError } from '../../lib/api';
import { useStore } from '../../state/store';

/**
 * Checklists: a grocery list, a workout, anything to tick off.
 *
 * The menu lists them; pick one and tap items to check and uncheck. All of it
 * can be said too ("show me my checklists", "add butter to my grocery list",
 * "check off eggs"), and the panel follows along: whatever JARVIS changes
 * shows here at once.
 */

interface Summary { id: string; title: string; total: number; done: number }
interface Item { id: string; text: string; done: boolean }
interface Checklist { id: string; title: string; items: Item[] }

export function ChecklistsPanel() {
  const version = useStore((s) => s.checklistsVersion);
  const focus = useStore((s) => s.checklistFocus);
  const [lists, setLists] = useState<Summary[] | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [list, setList] = useState<Checklist | null>(null);
  const [newTitle, setNewTitle] = useState('');
  const [newItem, setNewItem] = useState('');
  const [problem, setProblem] = useState('');

  const fail = (error: unknown) =>
    setProblem(error instanceof ApiError ? error.message : (error as Error).message);

  // "What's on my grocery list?" opens that one; "show me my checklists", the menu.
  useEffect(() => {
    if (focus) setOpenId(focus.id);
  }, [focus]);

  const loadLists = useCallback(async () => {
    try {
      setLists((await api.get<{ checklists: Summary[] }>('/checklists')).checklists);
    } catch (error) {
      fail(error);
    }
  }, []);

  const loadList = useCallback(async (id: string) => {
    try {
      setList(await api.get<Checklist>(`/checklists/${id}`));
    } catch {
      setOpenId(null);            // deleted elsewhere: back to the menu
      setList(null);
    }
  }, []);

  useEffect(() => { void loadLists(); }, [loadLists, version]);
  useEffect(() => {
    if (openId) void loadList(openId);
    else setList(null);
  }, [openId, loadList, version]);

  const change = async (work: () => Promise<Checklist>) => {
    setProblem('');
    try {
      setList(await work());
    } catch (error) {
      fail(error);
      if (openId) void loadList(openId);
    }
  };

  const toggle = (item: Item) => {
    if (!list) return;
    // Ticked at once; the server catches up.
    setList({ ...list, items: list.items.map((i) => (i.id === item.id ? { ...i, done: !i.done } : i)) });
    void change(() => api.patch<Checklist>(`/checklists/${list.id}/items/${item.id}`, { done: !item.done }));
  };

  const addItems = () => {
    if (!list || !newItem.trim()) return;
    const items = newItem.split(',').map((s) => s.trim()).filter(Boolean);
    setNewItem('');
    void change(() => api.post<Checklist>(`/checklists/${list.id}/items`, { items }));
  };

  const makeList = async () => {
    if (!newTitle.trim()) return;
    setProblem('');
    try {
      const made = await api.post<Checklist>('/checklists', { title: newTitle.trim(), items: [] });
      setNewTitle('');
      setOpenId(made.id);
      void loadLists();
    } catch (error) {
      fail(error);
    }
  };

  const deleteList = async () => {
    if (!list || !window.confirm(`Delete “${list.title}” and everything on it?`)) return;
    try {
      await api.del(`/checklists/${list.id}`);
      setOpenId(null);
      void loadLists();
    } catch (error) {
      fail(error);
    }
  };

  // -- one checklist ----------------------------------------------------------
  if (openId && list) {
    const done = list.items.filter((i) => i.done).length;
    return (
      <>
        <div className="panel__section">
          <button className="mini-action" onClick={() => setOpenId(null)}>‹ all checklists</button>
          <div className="checklist__head">
            <span className="checklist__title">{list.title}</span>
            <span className="checklist__count">{done} of {list.items.length} done</span>
          </div>
        </div>

        <div className="panel__section checklist">
          {list.items.length === 0 && (
            <div className="control__help">Nothing on it yet. Add something below, or say
              “add eggs to my {list.title.toLowerCase()}”.</div>
          )}
          {list.items.map((item) => (
            <div className="checklist__row" key={item.id}>
              <button role="checkbox" aria-checked={item.done}
                      className={`checklist__item ${item.done ? 'checklist__item--done' : ''}`}
                      onClick={() => toggle(item)}>
                <span className="checklist__box" aria-hidden="true">{item.done ? '✓' : ''}</span>
                <span className="checklist__text">{item.text}</span>
              </button>
              <button className="mini-action checklist__remove" aria-label={`Remove ${item.text}`}
                      title="Remove from the list"
                      onClick={() => void change(() => api.del<Checklist>(`/checklists/${list.id}/items/${item.id}`))}>
                ×
              </button>
            </div>
          ))}
          <div className="checklist__add">
            <input className="text-input" placeholder="Add items (commas for several)" value={newItem}
                   aria-label="Add items"
                   onChange={(event) => setNewItem(event.target.value)}
                   onKeyDown={(event) => { if (event.key === 'Enter') addItems(); }} />
            <button className="btn" disabled={!newItem.trim()} onClick={addItems}>Add</button>
          </div>
        </div>

        <div className="panel__section checklist__actions">
          <button className="mini-action" disabled={!done}
                  onClick={() => void change(() => api.post<Checklist>(`/checklists/${list.id}/reset`, { mode: 'uncheck' }))}>
            uncheck all
          </button>
          <button className="mini-action" disabled={!done}
                  onClick={() => void change(() => api.post<Checklist>(`/checklists/${list.id}/reset`, { mode: 'clear_checked' }))}>
            clear checked
          </button>
          <button className="mini-action" onClick={() => void deleteList()}>delete list</button>
        </div>
        {problem && <div className="panel__section"><div className="control__help control__help--warn">{problem}</div></div>}
      </>
    );
  }

  // -- the menu ---------------------------------------------------------------------
  return (
    <>
      <div className="panel__section">
        {lists === null && <div className="control__help">Loading…</div>}
        {lists?.length === 0 && (
          <div className="control__help">No checklists yet. Make one below, or say “make a grocery
            list: eggs, milk and bread”.</div>
        )}
        {lists?.map((summary) => (
          <button key={summary.id} className="checklist__pick" onClick={() => setOpenId(summary.id)}>
            <span className="checklist__title">{summary.title}</span>
            <span className="checklist__count">{summary.done} of {summary.total}</span>
          </button>
        ))}
      </div>
      <div className="panel__section">
        <div className="checklist__add">
          <input className="text-input" placeholder="New checklist, e.g. Workout" value={newTitle}
                 aria-label="New checklist name"
                 onChange={(event) => setNewTitle(event.target.value)}
                 onKeyDown={(event) => { if (event.key === 'Enter') void makeList(); }} />
          <button className="btn" disabled={!newTitle.trim()} onClick={() => void makeList()}>Make</button>
        </div>
      </div>
      {problem && <div className="panel__section"><div className="control__help control__help--warn">{problem}</div></div>}
    </>
  );
}
