import { useState } from 'react';
import type { FormEvent, ReactNode } from 'react';

import { api, ApiError } from '../../lib/api';

/**
 * Everyday lookups: convert units and money, the time somewhere else,
 * Wikipedia, a definition, a coin or dice. Each answer comes from the
 * backend (`/api/everyday/*`): computed there or read from a public source.
 * Chat uses the same tools ("5 miles in km", "time in Tokyo").
 */

interface Answer { text: string; link?: string; note?: string }

function useLookup() {
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [error, setError] = useState('');
  const run = async (load: () => Promise<Answer>) => {
    setBusy(true);
    setError('');
    setAnswer(null);
    try {
      setAnswer(await load());
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : 'That lookup failed. Check the connection.');
    } finally {
      setBusy(false);
    }
  };
  return { busy, answer, error, run };
}

const q = (params: Record<string, string>) => new URLSearchParams(params).toString();

function Lookup({ title, help, children, busy, answer, error, onSubmit, label = 'Go' }: {
  title: string; help: string; children: ReactNode; busy: boolean; answer: Answer | null;
  error: string; onSubmit: () => void; label?: string;
}) {
  const submit = (event: FormEvent) => { event.preventDefault(); onSubmit(); };
  return (
    <form className="panel__section" onSubmit={submit}>
      <div className="panel__section-title">{title}</div>
      <div className="ev-row">
        {children}
        <button className="btn btn--primary" type="submit" disabled={busy}>
          {busy ? 'Looking…' : label}
        </button>
      </div>
      <p className="control__help">{help}</p>
      {error && <p className="cd-form__error" role="alert">{error}</p>}
      {answer && (
        <div className="ev-answer" aria-live="polite">
          <p>{answer.text}</p>
          {answer.note && <p className="control__help">{answer.note}</p>}
          {answer.link && (
            <a className="control__help" href={answer.link} target="_blank" rel="noreferrer">{answer.link}</a>
          )}
        </div>
      )}
    </form>
  );
}

interface Converted { text: string }
interface Money { text: string; source: string }
interface Clock { text: string }
interface Wiki { title: string; description: string; summary: string; url: string; disambiguation: boolean }
interface Definition { word: string; url: string; senses: { part_of_speech: string; definition: string; example: string }[] }
interface Rolled { text: string }

export function EverydayPanel() {
  const units = useLookup();
  const [amount, setAmount] = useState('5');
  const [from, setFrom] = useState('miles');
  const [to, setTo] = useState('km');

  const money = useLookup();
  const [cash, setCash] = useState('50');
  const [cur1, setCur1] = useState('USD');
  const [cur2, setCur2] = useState('EUR');

  const clock = useLookup();
  const [place, setPlace] = useState('');

  const wiki = useLookup();
  const [topic, setTopic] = useState('');

  const dict = useLookup();
  const [word, setWord] = useState('');

  const random = useLookup();
  const [dice, setDice] = useState('2d6');
  const [choices, setChoices] = useState('');

  return (
    <>
      <div className="panel__section">
        <p className="control__help">
          Or just ask: “70 F in C”, “50 dollars in euros”, “time in Tokyo”, “who is Ada Lovelace”,
          “define serendipity”, “roll 2d6”.
        </p>
      </div>

      <Lookup title="Convert units" help="Length, weight, volume, temperature, speed, area, data, time, energy."
        {...units} onSubmit={() => void units.run(async () =>
          ({ text: (await api.get<Converted>(`/everyday/convert?${q({ amount, from, to })}`)).text }))}>
        <input className="text-input ev-num" inputMode="decimal" value={amount} aria-label="Amount"
          onChange={(e) => setAmount(e.target.value)} />
        <input className="text-input" value={from} aria-label="From unit" onChange={(e) => setFrom(e.target.value)} />
        <span className="ev-to">to</span>
        <input className="text-input" value={to} aria-label="To unit" onChange={(e) => setTo(e.target.value)} />
      </Lookup>

      <Lookup title="Currency" help="Today's European Central Bank reference rate (about 30 currencies)."
        {...money} onSubmit={() => void money.run(async () => {
          const r = await api.get<Money>(`/everyday/currency?${q({ amount: cash, from: cur1, to: cur2 })}`);
          return { text: r.text, note: r.source };
        })}>
        <input className="text-input ev-num" inputMode="decimal" value={cash} aria-label="Amount of money"
          onChange={(e) => setCash(e.target.value)} />
        <input className="text-input ev-code" value={cur1} aria-label="From currency" onChange={(e) => setCur1(e.target.value)} />
        <span className="ev-to">to</span>
        <input className="text-input ev-code" value={cur2} aria-label="To currency" onChange={(e) => setCur2(e.target.value)} />
      </Lookup>

      <Lookup title="World clock" help="A city or country. Daylight saving is taken into account."
        {...clock} onSubmit={() => void clock.run(async () =>
          ({ text: (await api.get<Clock>(`/everyday/time?${q({ place })}`)).text }))}>
        <input className="text-input" placeholder="e.g. Tokyo" value={place} aria-label="City or country"
          onChange={(e) => setPlace(e.target.value)} />
      </Lookup>

      <Lookup title="Wikipedia" help="The summary at the top of the article." {...wiki} label="Look up"
        onSubmit={() => void wiki.run(async () => {
          const r = await api.get<Wiki>(`/everyday/wikipedia?${q({ topic })}`);
          return {
            text: `${r.title}${r.description ? ` (${r.description})` : ''}: ${r.summary}`,
            link: r.url,
            note: r.disambiguation ? 'That name means several things — try something more specific.' : undefined,
          };
        })}>
        <input className="text-input" placeholder="e.g. Ada Lovelace" value={topic} aria-label="Topic"
          onChange={(e) => setTopic(e.target.value)} />
      </Lookup>

      <Lookup title="Dictionary" help="Definitions from Wiktionary." {...dict} label="Define"
        onSubmit={() => void dict.run(async () => {
          const r = await api.get<Definition>(`/everyday/define?${q({ word })}`);
          return {
            text: r.senses.map((s, i) => `${i + 1}. (${s.part_of_speech}) ${s.definition}`).join('  '),
            link: r.url,
          };
        })}>
        <input className="text-input" placeholder="a word" value={word} aria-label="Word"
          onChange={(e) => setWord(e.target.value)} />
      </Lookup>

      <Lookup title="Coin, dice, pick one" help="Dice like 2d6 or d20+3. To pick, separate the options with commas."
        {...random} label="Roll"
        onSubmit={() => void random.run(async () => {
          const options = choices.split(',').map((c) => c.trim()).filter(Boolean);
          const params: Record<string, string> = options.length
            ? { kind: 'pick', choices: options.join('|') }
            : dice.trim() ? { kind: 'dice', dice } : { kind: 'coin' };
          return { text: (await api.get<Rolled>(`/everyday/random?${q(params)}`)).text };
        })}>
        <input className="text-input ev-code" value={dice} aria-label="Dice" placeholder="coin"
          onChange={(e) => setDice(e.target.value)} />
        <input className="text-input" value={choices} aria-label="Options to pick from"
          placeholder="or: pizza, tacos" onChange={(e) => setChoices(e.target.value)} />
      </Lookup>
      <div className="panel__section">
        <button className="btn" onClick={() => void random.run(async () =>
          ({ text: (await api.get<Rolled>('/everyday/random?kind=coin')).text }))}>Flip a coin</button>
      </div>
    </>
  );
}
