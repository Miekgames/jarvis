import { useCallback, useEffect, useState } from 'react';

import { api, ApiError } from '../../lib/api';

/**
 * The product workshop.
 *
 * Pick a palette, see what a pack would cost before committing to it, build
 * it, then read the listing and promotion drafts it produced.
 *
 * Nothing in here publishes anything. There is no button that lists a
 * product, uploads a file or posts to a platform, because no such endpoint
 * exists — which is the reason this is safe to drive from a phone.
 *
 * Builds take minutes, so the request returns immediately and progress
 * arrives over the websocket. A phone that locks its screen mid-build
 * reconnects and picks the progress back up rather than being left holding a
 * dead request with no way to tell whether the work is still happening.
 */

interface Palette {
  slug: string;
  name: string;
  mood: string;
  dark: boolean;
  colors: string[];
}

interface Catalogue {
  styles: Record<string, string>;
  palettes: Palette[];
  resolutions: Record<string, number[]>;
  root: string;
}

interface Pack {
  slug: string;
  name: string;
  palette: string;
  designs: number;
  files: number;
  megabytes: number;
  archive: string | null;
  preview: string | null;
  built_at: string;
  directory: string;
}

interface Progress {
  slug?: string;
  done?: number;
  total?: number;
  percent?: number;
  file?: string;
}


interface TailDevice {
  name: string;
  os: string;
  address: string;
  online: boolean;
  is_self: boolean;
}

interface SendResult {
  ok: boolean;
  sent: number;
  total: number;
  detail: string;
}




export function WorkshopPanel() {
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [packs, setPacks] = useState<Pack[]>([]);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [error, setError] = useState('');

  const [palette, setPalette] = useState('midnight-harbour');
  const [count, setCount] = useState(8);
  const [estimate, setEstimate] = useState<any>(null);
  const [busy, setBusy] = useState(false);

  const [open, setOpen] = useState<string | null>(null);
  const [devices, setDevices] = useState<TailDevice[]>([]);
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState<SendResult | null>(null);

  const load = useCallback(async () => {
    try {
      const [cat, list] = await Promise.all([
        api.get<Catalogue>('/business/catalogue'),
        api.get<{ packs: Pack[]; building: Progress }>('/business/packs'),
      ]);
      setCatalogue(cat);
      setPacks(list.packs);
      try {
        const reply = await api.get<{ devices: TailDevice[] }>('/business/devices');
        setDevices((reply.devices ?? []).filter((d) => !d.is_self && d.online));
      } catch {
        setDevices([]);     // no Tailscale is a normal state, not an error
      }
      setProgress(list.building && list.building.slug ? list.building : null);
      setError('');
    } catch (exc) {
      setError((exc as Error).message);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // Progress and completion arrive on the bus.
  useEffect(() => {
    const onProgress = (event: Event) => {
      setProgress((event as CustomEvent).detail as Progress);
    };
    const onDone = () => { setProgress(null); setBusy(false); void load(); };
    const onFailed = (event: Event) => {
      const detail = (event as CustomEvent).detail as { error?: string };
      setError(detail?.error ?? 'The build failed.');
      setProgress(null);
      setBusy(false);
    };
    window.addEventListener('jarvis:business-progress', onProgress);
    window.addEventListener('jarvis:business-built', onDone);
    window.addEventListener('jarvis:business-failed', onFailed);
    return () => {
      window.removeEventListener('jarvis:business-progress', onProgress);
      window.removeEventListener('jarvis:business-built', onDone);
      window.removeEventListener('jarvis:business-failed', onFailed);
    };
  }, [load]);

  // Costing is cheap and the answer changes with every control, so it runs
  // as you choose rather than behind its own button.
  useEffect(() => {
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        const result = await api.post('/business/estimate', { palette, count });
        if (!cancelled) setEstimate(result);
      } catch { /* the build will report it properly */ }
    }, 250);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [palette, count]);

  const build = async () => {
    setBusy(true);
    setError('');
    try {
      await api.post('/business/build', { palette, count });
      setProgress({ slug: palette, percent: 0, done: 0, total: 0, file: 'starting' });
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
      setBusy(false);
    }
  };

  const openPack = async (slug: string) => {
    setOpen(open === slug ? null : slug);
  };



  const sendToPhone = async (slug: string, device: string) => {
    setSending(true);
    setSent(null);
    setError('');
    try {
      setSent(await api.post<SendResult>('/business/send-wallpapers',
                                         { pack: slug, count: 3, device }));
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setSending(false);
    }
  };


  if (!catalogue) {
    return <p className="control__help">{error || 'Loading…'}</p>;
  }

  const chosen = catalogue.palettes.find((p) => p.slug === palette);

  return (
    <>
      {/* ------------------------------------------------------ make one */}
      <div className="control control--stack">
        <div className="control__label">New wallpaper pack</div>

        <div className="swatches">
          {catalogue.palettes.map((p) => (
            <button
              key={p.slug}
              className={`swatch ${palette === p.slug ? 'swatch--on' : ''}`}
              onClick={() => setPalette(p.slug)}
              title={`${p.name} — ${p.mood}`}
              aria-pressed={palette === p.slug}
            >
              <span className="swatch__strip">
                {p.colors.map((c) => (
                  <span key={c} style={{ background: c }} />
                ))}
              </span>
              <span className="swatch__name">{p.name}</span>
            </button>
          ))}
        </div>

        <label className="control__row">
          <span className="control__key">Designs</span>
          <input
            type="range" min={4} max={20} value={count}
            onChange={(e) => setCount(Number(e.target.value))}
          />
          <span className="control__val">{count}</span>
        </label>

        {estimate && (
          <div className="control__help">
            {estimate.estimate.files} files, about {Math.round(estimate.estimate.megabytes)} MB.
            {' '}Suggested price ${estimate.plan.price_usd.toFixed(2)}.
            {!estimate.estimate.fits && (
              <span className="control__help--bad"> {estimate.estimate.warning}</span>
            )}
          </div>
        )}
        {chosen && (
          <div className="control__help">
            {chosen.name} — {chosen.mood}, {chosen.dark ? 'dark' : 'light'}.
          </div>
        )}

        {error && <div className="control__help control__help--bad">{error}</div>}

        {progress ? (
          <div className="control__stack">
            <div className="progress">
              <div className="progress__fill" style={{ width: `${progress.percent ?? 0}%` }} />
            </div>
            <div className="control__help">
              Rendering {progress.done ?? 0} of {progress.total ?? '…'}
              {progress.file ? ` — ${progress.file}` : ''}
            </div>
          </div>
        ) : (
          <button className="btn btn--primary" onClick={() => void build()} disabled={busy}>
            {busy ? 'Starting…' : `Generate ${count} wallpapers`}
          </button>
        )}

        <div className="control__help">
          Saved to {catalogue.root}. Nothing is published — this only writes files.
        </div>
      </div>

      {/* -------------------------------------------------------- made so far */}
      {packs.length === 0 && !progress && (
        <div className="control__help">No packs yet.</div>
      )}

      {packs.map((pack) => (
        <div className="control control--stack" key={pack.slug}>
          <button className="pack__head" onClick={() => void openPack(pack.slug)}>
            <span className="pack__name">{pack.name}</span>
            <span className="pack__meta">
              {pack.designs} designs · {pack.files} files · {pack.megabytes} MB
            </span>
          </button>

          {open === pack.slug && (
            <>
              {pack.preview && (
                <img className="pack__preview"
                     src={`/api/business/packs/${pack.slug}/preview`}
                     alt={`Every design in ${pack.name}`} />
              )}


              {/* --------------------------------- send to a device */}
              {devices.length > 0 && (
                <div className="control__stack">
                  <div className="control__label">Send to a device</div>
                  <div className="control__help">
                    Over Tailscale, straight to the device — nothing is
                    uploaded anywhere. Sends the phone-sized versions.
                  </div>
                  <div className="control__buttons">
                    {devices.map((device) => (
                      <button key={device.address} className="btn btn--small"
                              disabled={sending}
                              onClick={() => void sendToPhone(pack.slug, device.address)}>
                        {sending ? 'Sending…' : `Send 3 to ${device.name} (${device.os})`}
                      </button>
                    ))}
                  </div>
                  {sent && (
                    <div className={`control__help ${sent.ok ? '' : 'control__help--bad'}`}>
                      {sent.sent} of {sent.total} sent.
                      {sent.ok && ' Open Tailscale on that device to save them.'}
                    </div>
                  )}
                </div>
              )}

            </>
          )}
        </div>
      ))}
    </>
  );
}

// ---------------------------------------------------------------------------
function when(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString([], {
    weekday: 'short', hour: '2-digit', minute: '2-digit',
  });
}
