import { useEffect } from 'react';

import { BootSequence } from './components/BootSequence';
import { Clock } from './components/Clock';
import { Countdowns } from './components/Countdowns';
import { CommandBar } from './components/CommandBar';
import { Confirmation } from './components/Confirmation';
import { Conversation } from './components/Conversation';
import { AtmosphereFX } from './components/AtmosphereFX';
import { HudFrame } from './components/HudFrame';
import { Login } from './components/Login';
import { Panels } from './components/Panels';
import { SetupScreen } from './components/SetupScreen';
import { ParticleStage } from './components/ParticleStage';
import { SleepVeil } from './components/SleepVeil';
import { Palette } from './components/Palette';
import { Shortcuts } from './components/Shortcuts';
import { Sidebar } from './components/Sidebar';
import { StatusRail } from './components/StatusRail';
import { Suggestions } from './components/Suggestions';
import { useStore } from './state/store';

export default function App() {
  const init = useStore((s) => s.init);
  const sleeping = useStore((s) => s.sleeping);
  const toast = useStore((s) => s.toast);
  const toastLink = useStore((s) => s.toastLink);
  const toastPanel = toastLink && toastLink.message === toast ? toastLink.panel : null;
  const stale = useStore((s) => s.stale);
  const authRequired = useStore((s) => s.authRequired);
  const notify = useStore((s) => s.notify);
  const wakeMessage = useStore((s) => s.wakeMessage);
  const settings = useStore((s) => s.settings);
  const stopAll = useStore((s) => s.stopAll);
  const sleep = useStore((s) => s.sleep);

  useEffect(() => { void init(); }, [init]);

  // Auto-dismiss toasts.
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => notify(null), 6000);
    return () => window.clearTimeout(timer);
  }, [toast, notify]);

  // Theme variables driven by settings.
  useEffect(() => {
    const theme = settings.theme ?? {};
    const root = document.documentElement;
    if (theme.accent) root.style.setProperty('--accent', theme.accent);
    if (theme.accent_secondary) root.style.setProperty('--secondary', theme.accent_secondary);
    root.style.setProperty('--font-scale', String(theme.font_scale ?? 1));
    root.classList.toggle('high-contrast', !!theme.high_contrast);
  }, [settings.theme]);

  // Global shortcuts that aren't owned by the command bar.
  useEffect(() => {
    const shortcuts = settings.shortcuts ?? {};
    const onKeyDown = (event: KeyboardEvent) => {
      const parts: string[] = [];
      if (event.ctrlKey) parts.push('ctrl');
      if (event.altKey) parts.push('alt');
      if (event.shiftKey) parts.push('shift');
      if (event.metaKey) parts.push('meta');
      const key = event.key === ' ' ? 'space' : event.key.toLowerCase();
      if (!['control', 'alt', 'shift', 'meta'].includes(key)) parts.push(key);
      const combo = parts.join('+');

      if (combo === (shortcuts.sleep ?? 'ctrl+shift+s')) {
        event.preventDefault();
        void sleep();
      } else if (combo === (shortcuts.stop_all ?? 'ctrl+shift+x')) {
        event.preventDefault();
        void stopAll();
      } else if (combo === (shortcuts.history ?? 'ctrl+h')) {
        event.preventDefault();
        useStore.getState().setPanel('history');
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [settings.shortcuts, sleep, stopAll]);

  return (
    <>
      <ParticleStage />

      {authRequired && <Login />}

      {!sleeping && !authRequired && (
        <>
          <AtmosphereFX />
          <HudFrame />
          <StatusRail />
          <Clock />
          <Countdowns />
          {wakeMessage && <div className="greeting">{wakeMessage}</div>}
          <Conversation />
          <Suggestions />
          <CommandBar />
          <Sidebar />
          <Panels />
        </>
      )}

      {/* Nothing else while locked: the command palette, the confirmation
          queue and the boot readout all speak about a system this browser has
          not been let into yet. */}
      {!authRequired && (
        <>
          <Palette />
          <Shortcuts />
          <Confirmation />
          <SleepVeil />
          <BootSequence />
          <SetupScreen />
        </>
      )}

      {/* The backend has restarted since this page loaded, so the page is
          running older code. It stays until acted on: a stale page has
          already been the cause of real misbehaviour here. */}
      {stale && (
        <div className="stale-bar" role="alert">
          <span>
            JARVIS restarted since this page was opened, so it is running an older
            version.
          </span>
          <button className="btn btn--small" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      )}

      {toast && (
        <div className={`toast ${toastPanel ? 'toast--action' : ''}`} role="status"
             onClick={() => {
               // A toast that names a place goes there when tapped; any
               // other one is just dismissed.
               const panel = toastPanel;
               notify(null);
               if (panel && useStore.getState().panel !== panel) {
                 useStore.getState().setPanel(panel);
               }
             }}>
          {toast}
          {toastPanel && <span className="toast__go">Open ›</span>}
        </div>
      )}
    </>
  );
}
