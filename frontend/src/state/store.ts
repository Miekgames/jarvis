/**
 * Application state.
 *
 * One store, wired to the backend event stream. Everything the particle field
 * and the interface react to lives here, so the UI has a single source of
 * truth for "what is JARVIS doing right now".
 */
import { create } from 'zustand';

import { api, ApiError, BusEvent, eventStream, streamChat } from '../lib/api';
import type { FieldContext, FieldState } from '../gl/ParticleField';
import type { ActiveSkill } from './skillColours';

/** Live state of the wake word, so the UI can show it honestly. */
export interface WakeStatus {
  /** 'off' — not enabled. 'starting' — opening the microphone.
   *  'armed' — listening. 'triggered' — just heard. 'silent' — the device is
   *  open but no sound is reaching it, which is the failure that used to be
   *  invisible. 'failed' — it cannot run at all. */
  phase: 'off' | 'starting' | 'armed' | 'triggered' | 'silent' | 'failed';
  /** Why it is off, silent or failed. Empty when armed. */
  detail: string;
  /** The last thing the recogniser returned, whether or not it matched.
   *  Only the browser fallback produces this; the local spotter has no
   *  transcript, it has a score. */
  heard: string;
  /** When the wake word last actually fired. */
  lastTrigger: number | null;
  /** Which engine is actually running. 'local' is the openWakeWord spotter in
   *  the backend; 'browser' is the Web Speech fallback. */
  engine: 'local' | 'browser' | 'none';
  /** The input device in use, so a silent microphone is nameable. */
  device: string;
  /** Live input level, 0–1. Non-zero means audio really is arriving. */
  level: number;
  /** Confidence of the last scored frame, 0–1. Local engine only. */
  score: number;
}

/** Live status of the local keyword spotter running in the backend. */
export interface HotwordStatus {
  running: boolean;
  phase: 'off' | 'starting' | 'listening' | 'silent' | 'failed';
  detail: string;
  engine: string;
  model: string;
  threshold: number;
  device: { index: number; name: string; signal: boolean; rms: number } | null;
  level: number;
  score: number;
  detections: number;
  last_detection: string | null;
  suspended: boolean;
  available: boolean;
  /** Whether the backend captures and transcribes the command itself. */
  local_commands: boolean;
  capturing: boolean;
  last_command: string | null;
}

export interface Utterance {
  id: string;
  role: 'user' | 'jarvis';
  text: string;
  ts: number;
  streaming?: boolean;
  tools?: { name: string; ok?: boolean; summary?: string;
            flagged?: boolean; flagLevel?: string; redacted?: number;
            input?: Record<string, unknown>; durationMs?: number }[];
  error?: boolean;
  /** Measured cost of producing this reply (JARVIS turns only). */
  telemetry?: import('../lib/api').Telemetry;
}

export interface TaskRecord {
  id: string;
  name: string;
  type: string;
  status: string;
  priority: string;
  progress: number;
  current_step: string;
  eta?: string | null;
  error?: string | null;
  created_at: string;
  result?: unknown;
}

export interface NotificationRecord {
  id: string;
  title: string;
  body: string;
  priority: string;
  ts: string;
  read: boolean;
  source: string;
}

export interface ConfirmationRecord {
  id: string;
  tool: string;
  summary: string;
  risk: string;
  detail: Record<string, any>;
}

export interface Metrics {
  cpu?: number;
  mem?: number;
  disk?: number;
  gpu?: number | null;
  gpu_temp?: number | null;
  battery?: number | null;
  net_up?: number;
  net_down?: number;
  uptime_seconds?: number;
  mem_used_gb?: number;
  mem_total_gb?: number;
  disk_free_gb?: number;
  processes?: number;
}

export interface BootStep {
  key: string;
  label: string;
  status: string;
  detail: string;
}

export type Panel =
  | null | 'tasks' | 'settings' | 'history' | 'automations'
  | 'system' | 'memory' | 'calendar' | 'integrations' | 'activity' | 'notifications'
  | 'goals' | 'agents' | 'research' | 'developer' | 'selfedit' | 'voice'
  | 'workshop' | 'away' | 'skills' | 'plugins' | 'documents' | 'email' | 'countdowns' | 'everyday' | 'reminders';

interface Store {
  // -- presence ------------------------------------------------------------
  state: FieldState;
  sleeping: boolean;
  connected: boolean;
  booted: boolean;
  bootSteps: BootStep[];
  mode: string;
  amplitude: number;
  wakeMessage: string;

  // -- conversation --------------------------------------------------------
  utterances: Utterance[];
  conversationId: string | null;
  busy: boolean;

  // -- data ----------------------------------------------------------------
  tasks: TaskRecord[];
  notifications: NotificationRecord[];
  confirmations: ConfirmationRecord[];
  metrics: Metrics;
  /**
   * Recent readings, oldest first, for the sidebar's trend lines.
   *
   * Seeded from the backend's stored history so a freshly opened page shows
   * the last half hour rather than a flat line that fills in over the next
   * few minutes, then extended by the live stream.
   */
  metricHistory: Metrics[];
  activity: { ts: string; level: string; source: string; message: string }[];
  health: Record<string, { status: string; detail: string }>;
  overall: string;
  /** What JARVIS can currently see the user doing. */
  focus: { active_app?: string; window_title?: string; category?: string } | null;
  /** Contextual mode driving the shape of the particle field. */
  fieldContext: FieldContext;
  /** How certain JARVIS is right now — tightens or disperses the field. */
  confidence: number;
  /** Running agents, each a particle sub-cluster. */
  agents: { id: string; label: string; colour: string; activity: number }[];
  /** Skills in use right now; the screen fades to the shown one's colour. */
  activeSkills: ActiveSkill[];

  // -- ui ------------------------------------------------------------------
  panel: Panel;
  toast: string | null;
  /** A panel a toast leads to, opened by tapping it. Tied to its own
   *  message, so a later toast set directly cannot inherit the link. */
  toastLink: { message: string; panel: Panel } | null;
  // First-run setup, or reopened from Settings to change the AI.
  setupOpen: boolean;
  setSetupOpen: (open: boolean) => void;
  /** This page loaded against an older run of the backend. */
  stale: boolean;
  /**
   * The backend refused this browser, so a password is needed.
   *
   * Only ever true when JARVIS is reached from a different machine: on the
   * machine it runs on, local mode issues a session transparently. Without
   * this flag a 401 was indistinguishable from the backend being down, and
   * reported as "is it running on port 8000?" -- which sent you looking in
   * entirely the wrong place.
   */
  authRequired: boolean;
  settings: Record<string, any>;
  /**
   * What the wake-word loop is actually doing. Without this the feature is
   * unfalsifiable from the outside: you cannot tell "listening and hearing
   * nothing" apart from "never started".
   */
  wakeStatus: WakeStatus;
  /**
   * The local keyword spotter's own report. Null until first fetched, so
   * "unknown" and "not running" stay distinguishable.
   */
  hotword: HotwordStatus | null;

  // -- actions -------------------------------------------------------------
  init: () => Promise<void>;
  /** Called by the login screen once the backend has set the session cookie. */
  signedIn: () => Promise<void>;
  send: (text: string, source?: string) => Promise<void>;
  setState: (state: FieldState) => void;
  setAmplitude: (value: number) => void;
  setPanel: (panel: Panel) => void;
  notify: (message: string | null, panel?: Panel) => void;
  setWakeStatus: (patch: Partial<WakeStatus>) => void;
  refreshHotword: () => Promise<HotwordStatus | null>;
  startHotword: (options?: { device?: number; threshold?: number }) => Promise<void>;
  stopHotword: () => Promise<void>;
  sleep: () => Promise<void>;
  wake: () => Promise<void>;
  resolveConfirmation: (id: string, approved: boolean) => Promise<void>;
  refreshTasks: () => Promise<void>;
  refreshNotifications: () => Promise<void>;
  stopAll: () => Promise<void>;
  loadSettings: () => Promise<void>;
  updateSetting: (key: string, value: any) => Promise<void>;
  clearConversation: () => void;
}

/** Readings kept for the trend lines. At one every three seconds this is
 *  about twelve minutes, which is as much as a sparkline can usefully say. */
const METRIC_HISTORY = 240;

const MAX_UTTERANCES = 60;

/**
 * Whether the event-bus subscription has been made. Module scope, not store
 * state: it must survive the store being read before it is fully built, and
 * it is not something the UI renders.
 */
let subscribed = false;

/**
 * Conversations this tab is streaming itself.
 *
 * The backend publishes every message to the event bus, including ones this
 * tab asked for. Without this the locally streamed reply and the broadcast
 * copy would both be rendered.
 */
const ownConversations = new Set<string>();

/** How recent a local turn has to be to count as the same one echoed back. */
const ECHO_WINDOW_MS = 20_000;

/**
 * Whether this exact turn was just rendered locally.
 *
 * Only the last few are checked: asking the same question twice on purpose
 * is ordinary, and should show twice.
 */
function justSaid(utterances: Utterance[], role: string, text: string): boolean {
  const now = Date.now();
  return utterances.slice(-4).some(
    (u) => u.role === role && u.text.trim() === text && now - u.ts < ECHO_WINDOW_MS);
}

/**
 * Which run of the backend this page loaded against.
 *
 * A page left open across a restart is running older code than the server.
 * That is not theoretical here: stale tabs kept submitting spoken commands
 * after the backend took that job over, so one sentence produced five
 * conversations and five concurrent calls to a model that serves them one at
 * a time. The page cannot safely reload itself -- there may be a half-typed
 * message in the box -- so it says so instead.
 */
let backendInstance: string | null = null;
let staleNoticeShown = false;

async function checkBackendInstance(set: (patch: Partial<Store>) => void) {
  try {
    const status = await api.get<{ instance_id?: string }>('/status');
    const instance = status.instance_id ?? null;
    if (!instance) return;
    if (backendInstance === null) {
      backendInstance = instance;
      return;
    }
    if (instance !== backendInstance && !staleNoticeShown) {
      staleNoticeShown = true;
      // Not a toast. A toast disappears after six seconds; this condition
      // does not, and a page running older code than the backend is worth
      // more than a notice the user has to be looking at to catch.
      set({ stale: true });
    }
  } catch {
    // Unreachable is already reported by the connection indicator.
  }
}

export const useStore = create<Store>((set, get) => ({
  state: 'offline',
  sleeping: false,
  connected: false,
  booted: false,
  bootSteps: [],
  mode: 'normal',
  amplitude: 0,
  wakeMessage: '',

  utterances: [],
  conversationId: null,
  busy: false,

  tasks: [],
  notifications: [],
  confirmations: [],
  metrics: {},
  metricHistory: [],
  activity: [],
  health: {},
  overall: 'unknown',
  focus: null,
  fieldContext: 'neutral' as FieldContext,
  confidence: 0.8,
  agents: [],
  activeSkills: [],

  panel: null,
  toast: null,
  toastLink: null,
  setupOpen: false,
  setSetupOpen: (setupOpen) => set({ setupOpen }),
  stale: false,
  authRequired: false,
  settings: {},
  wakeStatus: { phase: 'off', detail: '', heard: '', lastTrigger: null,
                engine: 'none', device: '', level: 0, score: 0 },
  hotword: null,

  // ======================================================================
  async init() {
    // Subscribe exactly once.
    //
    // React runs effects twice in development, so `init` is called twice, and
    // a fresh arrow function each time is a *different* listener -- the Set
    // cannot dedupe it. The result was every event on the bus being handled
    // twice: two toasts per notification, agent pips counted double, and a
    // wake-word detection dispatched twice. One socket, two listeners.
    if (!subscribed) {
      subscribed = true;
      eventStream.onStatus((connected) => {
        set({ connected });
        if (!connected && !useStore.getState().sleeping) set({ state: 'offline' });
        // Reconnecting is the moment a restart becomes visible.
        if (connected) void checkBackendInstance(set);
      });
      eventStream.subscribe((event) => handleEvent(event, set, useStore.getState));
    }
    eventStream.connect();

    try {
      const boot = await api.get<{ steps: BootStep[]; online: boolean }>('/boot');
      set({ bootSteps: boot.steps, booted: boot.online });

      const status = await api.get<any>('/status');
      if (backendInstance === null) backendInstance = status.instance_id ?? null;
      set({
        state: status.presence?.sleeping ? 'sleep' : (status.state ?? 'idle'),
        sleeping: !!status.presence?.sleeping,
        mode: status.presence?.mode ?? 'normal',
        health: status.health?.checks ?? {},
        overall: status.health?.overall ?? 'unknown',
      });

      await Promise.all([
        get().refreshTasks(),
        get().refreshNotifications(),
        get().loadSettings(),
        api.get<{ active: ActiveSkill[] }>('/skills/active')
          .then((r) => set({ activeSkills: r.active ?? [] }))
          .catch(() => undefined),
      ]);

      const [metrics, activity, confirmations, history] = await Promise.all([
        api.get<{ latest: Metrics }>('/system/metrics'),
        api.get<{ activity: any[] }>('/activity?limit=40'),
        api.get<{ pending: ConfirmationRecord[] }>('/confirmations'),
        // Non-fatal: the trend lines are a nicety, the live numbers are not.
        api.get<{ history: Metrics[] }>('/system/metrics/history?minutes=30')
          .catch(() => ({ history: [] as Metrics[] })),
      ]);
      set({
        metrics: metrics.latest ?? {},
        metricHistory: (history.history ?? []).slice(-METRIC_HISTORY),
        activity: activity.activity ?? [],
        confirmations: confirmations.pending ?? [],
      });
    } catch (error) {
      console.error('[store] init failed', error);
      // A 401 is not an unreachable backend -- it is a reachable one asking
      // who you are. Saying "is it running on port 8000?" to someone whose
      // only problem is that they have not typed a password wastes their
      // time on the wrong question.
      if (error instanceof ApiError && error.status === 401) {
        set({ authRequired: true, toast: null });
        return;
      }
      set({ toast: 'Could not reach the JARVIS backend. Is it running on port 8000?' });
    }
  },

  // ======================================================================
  async signedIn() {
    // The session is an httpOnly cookie the browser now holds, so there is
    // nothing to store -- just start over with it attached.
    set({ authRequired: false, toast: null });
    await get().init();
  },

  // ======================================================================
  async send(text, source = 'text') {
    const trimmed = text.trim();
    if (!trimmed || get().busy) return;

    const userId = `u_${Date.now()}`;
    const replyId = `j_${Date.now()}`;
    const pending: Utterance[] = [
      { id: userId, role: 'user', text: trimmed, ts: Date.now() },
      { id: replyId, role: 'jarvis', text: '', ts: Date.now(), streaming: true, tools: [] },
    ];
    set((s) => ({
      busy: true,
      state: 'thinking' as FieldState,
      utterances: [...s.utterances, ...pending].slice(-MAX_UTTERANCES),
    }));

    const patch = (updater: (u: Utterance) => Utterance) =>
      set((s) => ({
        utterances: s.utterances.map((u) => (u.id === replyId ? updater(u) : u)),
      }));

    try {
      await streamChat(trimmed, get().conversationId, (event) => {
        switch (event.type) {
          case 'start':
            set({ conversationId: event.conversation_id ?? null });
            if (event.conversation_id) ownConversations.add(event.conversation_id);
            break;
          case 'delta':
            patch((u) => ({ ...u, text: u.text + (event.text ?? '') }));
            break;
          case 'tool_start':
            set({ state: 'working' });
            patch((u) => ({ ...u, tools: [...(u.tools ?? []),
              { name: event.tool ?? '', input: event.input }] }));
            break;
          case 'tool_end':
            patch((u) => {
              const tools = [...(u.tools ?? [])];
              for (let i = tools.length - 1; i >= 0; i -= 1) {
                if (tools[i].name === event.tool && tools[i].ok === undefined) {
                  tools[i] = { ...tools[i], ok: event.ok, summary: event.summary,
                              flagged: event.flagged, flagLevel: event.flag_level,
                              redacted: event.redacted, durationMs: event.duration_ms };
                  break;
                }
              }
              return { ...u, tools };
            });
            set({ state: 'thinking' });
            break;
          case 'final':
            patch((u) => ({ ...u, text: event.text || u.text, streaming: false,
                            telemetry: event.telemetry ?? u.telemetry }));
            set({ conversationId: event.conversation_id ?? get().conversationId });
            break;
          case 'error':
            patch((u) => ({ ...u, text: event.error ?? 'Something went wrong.', streaming: false, error: true }));
            set({ state: 'error' });
            window.setTimeout(() => set({ state: 'idle' }), 2600);
            break;
        }
      });
    } catch (error) {
      patch((u) => ({
        ...u,
        text: `I couldn't reach the backend: ${(error as Error).message}`,
        streaming: false,
        error: true,
      }));
      set({ state: 'error' });
    } finally {
      set({ busy: false });
      if (get().state !== 'error') set({ state: 'idle' });
    }
  },

  setState: (state) => set({ state }),
  setAmplitude: (amplitude) => set({ amplitude }),
  setPanel: (panel) => set((s) => ({ panel: s.panel === panel ? null : panel })),
  notify: (toast, panel = null) => set({
    toast, toastLink: toast && panel ? { message: toast, panel } : null }),

  setWakeStatus: (patch) => set((s) => ({ wakeStatus: { ...s.wakeStatus, ...patch } })),

  /** Pull the spotter's status and mirror the parts the UI shows. */
  refreshHotword: async () => {
    try {
      const status = await api.get<HotwordStatus>('/voice/hotword');
      set((s) => ({
        hotword: status,
        wakeStatus: status.running
          ? {
              ...s.wakeStatus,
              engine: 'local',
              // 'listening' is this store's 'armed'; 'silent' is its own
              // phase because an open-but-deaf microphone is the failure
              // that used to be impossible to see.
              phase: status.phase === 'listening' ? 'armed'
                   : status.phase === 'silent' ? 'silent'
                   : status.phase === 'failed' ? 'failed' : 'starting',
              detail: status.detail,
              device: status.device?.name ?? '',
              level: status.level,
              score: status.score,
            }
          : s.wakeStatus,
      }));
      return status;
    } catch {
      // The backend being unreachable is already surfaced elsewhere; this
      // must not overwrite a real wake-word failure with a network one.
      set({ hotword: null });
      return null;
    }
  },

  startHotword: async (options = {}) => {
    try {
      const status = await api.post<HotwordStatus>('/voice/hotword/start', options);
      set({ hotword: status });
      await get().refreshHotword();
    } catch (error) {
      set((s) => ({
        wakeStatus: { ...s.wakeStatus, phase: 'failed', detail: (error as Error).message },
      }));
      get().notify(`Wake word could not start — ${(error as Error).message}`);
    }
  },

  stopHotword: async () => {
    try {
      const status = await api.post<HotwordStatus>('/voice/hotword/stop', {});
      set({ hotword: status });
    } catch { /* stopping something already stopped is not worth a toast */ }
    set((s) => ({
      wakeStatus: { ...s.wakeStatus, phase: 'off', engine: 'none', level: 0, score: 0,
                    detail: 'Wake word is off.' },
    }));
  },
  clearConversation: () => set({ utterances: [], conversationId: null }),

  // ======================================================================
  async sleep() {
    try {
      const result = await api.post<any>('/presence/sleep', { reason: 'user' });
      set({ sleeping: true, state: 'sleep', panel: null });
      return result;
    } catch (error) {
      set({ toast: `Couldn't enter sleep mode: ${(error as Error).message}` });
    }
  },

  async wake() {
    try {
      const result = await api.post<any>('/presence/wake', { reason: 'user' });
      set({ sleeping: false, state: 'idle', wakeMessage: result?.message ?? '' });
      await Promise.all([get().refreshTasks(), get().refreshNotifications()]);
      window.setTimeout(() => set({ wakeMessage: '' }), 12000);
    } catch (error) {
      set({ toast: `Couldn't wake: ${(error as Error).message}` });
    }
  },

  // ======================================================================
  async resolveConfirmation(id, approved) {
    try {
      await api.post(`/confirmations/${id}`, { approved });
    } catch (error) {
      set({ toast: `Confirmation failed: ${(error as Error).message}` });
    }
    set((s) => ({ confirmations: s.confirmations.filter((c) => c.id !== id) }));
  },

  async refreshTasks() {
    try {
      const result = await api.get<{ tasks: TaskRecord[] }>('/tasks?limit=60');
      set({ tasks: result.tasks });
    } catch { /* transient */ }
  },

  async refreshNotifications() {
    try {
      const result = await api.get<{ notifications: NotificationRecord[] }>('/notifications?limit=50');
      set({ notifications: result.notifications });
    } catch { /* transient */ }
  },

  async stopAll() {
    try {
      const result = await api.post<{ count: number }>('/tasks/control/stop-all');
      set({ toast: `Stopped ${result.count} task${result.count === 1 ? '' : 's'}.` });
      await get().refreshTasks();
    } catch (error) {
      set({ toast: `Stop failed: ${(error as Error).message}` });
    }
  },

  async loadSettings() {
    try {
      const result = await api.get<{ settings: Record<string, any> }>('/settings');
      set({ settings: result.settings });
    } catch { /* transient */ }
  },

  async updateSetting(key, value) {
    set((s) => ({ settings: { ...s.settings, [key]: value } }));
    try {
      await api.put(`/settings/${key}`, { value });
    } catch (error) {
      set({ toast: `Couldn't save ${key}: ${(error as Error).message}` });
      await get().loadSettings();
    }
  },
}));

// ===========================================================================
function handleEvent(event: BusEvent, set: any, get: () => Store) {
  switch (event.type) {
    case 'jarvis.state':
      if (!get().sleeping) set({ state: event.data.state as FieldState });
      break;

    case 'system.metrics':
      set((s: Store) => ({
        metrics: event.data as Metrics,
        // Capped: this is a trend line a couple of hundred pixels wide, and
        // an unbounded array behind it would be a leak that grows all day.
        metricHistory: [...s.metricHistory, event.data as Metrics].slice(-METRIC_HISTORY),
      }));
      break;

    // Which skills are in use: the screen fades to the shown one's colour.
    case 'skills.active':
      set({ activeSkills: (event.data.active ?? []) as ActiveSkill[] });
      break;

    case 'task.created':
    case 'task.started':
    case 'task.completed':
    case 'task.failed':
    case 'task.cancelled':
    case 'task.paused':
    case 'task.resumed':
      get().refreshTasks();
      if (['task.completed', 'task.failed', 'task.cancelled'].includes(event.type)) {
        // Work that ended takes its colour with it, even if its own
        // goodbye was missed while the screen was reconnecting.
        set((s: Store) => ({
          activeSkills: s.activeSkills.filter((a) => a.id !== event.data.id),
        }));
      }
      if (event.type === 'task.completed') {
        set({ toast: `Task finished: ${event.data.name}` });
      } else if (event.type === 'task.failed') {
        set({ toast: `Task failed: ${event.data.name}` });
      }
      break;

    case 'task.progress':
      set((s: Store) => ({
        tasks: s.tasks.map((t) =>
          t.id === event.data.id
            ? { ...t, progress: event.data.progress ?? t.progress,
                current_step: event.data.current_step ?? t.current_step,
                eta: event.data.eta ?? t.eta }
            : t,
        ),
      }));
      break;

    // One of this computer's own notifications, read from the Action
    // Center. The content is not put in the store: it lives in the backend's
    // memory for as long as it is current, and the panel fetches it. This
    // only tells the panel to look again.
    case 'notification.desktop':
      window.dispatchEvent(new CustomEvent('jarvis:desktop-notification'));
      break;

    // Pack rendering. Minutes of work, so the request that starts it returns
    // at once and the progress comes this way -- which also means a phone
    // that locked its screen mid-build reconnects and picks it straight back
    // up, rather than holding a dead request with nothing to show.
    // An unattended run, reported live rather than polled. The point of
    // leaving it working is watching from somewhere else.
    case 'autonomy.progress':
      window.dispatchEvent(new CustomEvent('jarvis:autonomy',
                                           { detail: event.data }));
      break;

    // A setting changed by asking ("switch to the Ember theme").
    case 'settings.changed':
      void get().loadSettings();
      break;

    // "Open my reminders": the backend says which, this opens it.
    case 'ui.open_panel':
      set({ panel: (event.data.panel ?? null) as Panel });
      break;

    case 'schedule.changed':
      window.dispatchEvent(new CustomEvent('jarvis:reminders'));
      break;

    case 'countdowns.changed':
      window.dispatchEvent(new CustomEvent('jarvis:countdowns'));
      break;

    case 'business.progress':
      window.dispatchEvent(new CustomEvent('jarvis:business-progress',
                                           { detail: event.data }));
      break;

    case 'business.built':
      window.dispatchEvent(new CustomEvent('jarvis:business-built',
                                           { detail: event.data }));
      set({ toast: `Finished '${event.data.name}' — ${event.data.files} files.` });
      break;

    case 'business.failed':
      window.dispatchEvent(new CustomEvent('jarvis:business-failed',
                                           { detail: event.data }));
      set({ toast: `The pack build failed: ${event.data.error}` });
      break;

    case 'notification.new':
      set((s: Store) => ({
        notifications: [event.data as NotificationRecord, ...s.notifications].slice(0, 60),
        toast: event.data.priority === 'critical' || event.data.priority === 'high'
          ? `${event.data.title}` : s.toast,
      }));
      break;

    case 'confirmation.requested':
      set((s: Store) => ({
        confirmations: [...s.confirmations, event.data as ConfirmationRecord],
      }));
      break;

    case 'confirmation.resolved':
      set((s: Store) => ({
        confirmations: s.confirmations.filter((c) => c.id !== event.data.id),
      }));
      break;

    case 'activity.new':
      set((s: Store) => ({
        activity: [{ ts: event.ts, ...event.data } as any, ...s.activity].slice(0, 200),
      }));
      break;

    case 'system.health':
      set({ health: event.data.checks ?? {}, overall: event.data.overall ?? 'unknown' });
      break;

    case 'awareness.focus':
    case 'awareness.title':
      set({
        focus: {
          active_app: event.data.app,
          window_title: event.data.title,
          category: event.data.category,
        },
      });
      break;

    case 'awareness.idle':
      set({ focus: null });
      break;

    // -- Field Dynamics: context follows the work actually happening -------
    case 'research.started':
      set({ fieldContext: 'research' as FieldContext });
      break;

    case 'research.completed':
      set({ fieldContext: 'neutral' as FieldContext });
      break;

    case 'agent.started':
    case 'agent.progress':
    case 'agent.finished': {
      // Agent colour and activity come straight from the backend's registry,
      // so a cluster's tint always matches the agent it represents.
      const current = get().agents.filter((a) => a.id !== event.data.id);
      if (event.type !== 'agent.finished') {
        current.push({
          id: event.data.id,
          label: event.data.label ?? event.data.agent,
          colour: event.data.colour ?? '#5ad2ff',
          activity: event.type === 'agent.progress'
            ? Math.max(0.35, event.data.progress ?? 0.5) : 0.8,
        });
      }
      set({ agents: current.slice(0, 6) });
      // A coding or research agent reshapes the whole field.
      if (event.type === 'agent.started') {
        const shape: Record<string, FieldContext> = {
          researcher: 'research', coder: 'coding', meeting: 'meeting',
        };
        const next = shape[event.data.agent];
        if (next) set({ fieldContext: next });
      } else if (event.type === 'agent.finished' && current.length === 0) {
        set({ fieldContext: 'neutral' as FieldContext });
      }
      break;
    }

    case 'mode.changed': {
      const modeShape: Record<string, FieldContext> = {
        focus: 'focus', meeting: 'meeting', developer: 'coding', study: 'research',
      };
      set({ mode: event.data.mode,
            fieldContext: modeShape[event.data.mode] ?? ('neutral' as FieldContext) });
      break;
    }

    case 'goal.needs_input':
      // Waiting on the user is genuine uncertainty; show it.
      set({ confidence: 0.3 });
      break;

    case 'goal.planned':
      set({ confidence: 0.85 });
      break;

    case 'system.alert':
      set({ toast: event.data.message });
      break;

    case 'jarvis.sleep':
      set({ sleeping: true, state: 'sleep' });
      break;

    case 'jarvis.wake':
      set({ sleeping: false, state: 'idle', wakeMessage: event.data.message ?? '' });
      window.setTimeout(() => set({ wakeMessage: '' }), 12000);
      break;

    // The local spotter heard the wake word. The page opens a command
    // session in response -- the backend never listens *for a command*, only
    // for the word, so nothing is recorded until the user is addressing it.
    case 'jarvis.wakeword.detected':
      set((s: Store) => ({
        wakeStatus: {
          ...s.wakeStatus,
          phase: 'triggered',
          engine: 'local',
          lastTrigger: Date.now(),
          score: event.data.score ?? 0,
          device: event.data.device ?? s.wakeStatus.device,
          detail: '',
        },
      }));
      window.dispatchEvent(new CustomEvent('jarvis:wakeword', { detail: event.data }));
      break;

    // The backend captured and transcribed the command itself. It heard the
    // wake word on a device it knows carries audio, so it keeps the sentence
    // after it on the same device rather than handing it to the browser's
    // default input -- which may be a different, silent one.
    case 'jarvis.wakeword.command':
      window.dispatchEvent(new CustomEvent('jarvis:wakecommand', { detail: event.data }));
      break;

    // A conversation the backend is driving, rather than this tab. Spoken
    // commands run there so that one sentence produces one answer no matter
    // how many tabs are open -- which also means the page has to render an
    // exchange it did not start.
    case 'conversation.message': {
      const conversationId = event.data.conversation_id as string;
      // Our own streamed conversation already renders locally; rendering the
      // broadcast too would double every message.
      // A report that finished long after the reply (research) is not a
      // duplicate of anything streamed here, so it always shows.
      if (ownConversations.has(conversationId) && !event.data.late) break;
      const text = (event.data.text ?? '').trim();
      if (!text) break;

      // ...and the id check alone is not enough on a *new* conversation.
      //
      // The id only becomes ours when the stream's `start` event arrives,
      // and the backend persists and broadcasts the user's turn before
      // that. Lose the race and the first message of every new thread
      // renders twice — visibly, with the reply sandwiched between the two
      // copies. Matching on content as well closes it whichever order the
      // two arrive in.
      if (justSaid(get().utterances, event.data.role as string, text)) break;

      if (event.data.role === 'user') {
        set((s: Store) => ({
          conversationId,
          state: 'thinking' as FieldState,
          utterances: [...s.utterances, {
            id: `u_remote_${conversationId}_${s.utterances.length}`,
            role: 'user' as const, text, ts: Date.now(),
          }].slice(-MAX_UTTERANCES),
        }));
      } else {
        set((s: Store) => ({
          state: 'idle' as FieldState,
          utterances: [...s.utterances, {
            id: `j_remote_${conversationId}_${s.utterances.length}`,
            role: 'jarvis' as const, text, ts: Date.now(),
          }].slice(-MAX_UTTERANCES),
        }));
        // Spoken to, so answer aloud. Guarded inside the voice controller
        // against repeating itself.
        window.dispatchEvent(new CustomEvent('jarvis:spoke', { detail: text }));
      }
      break;
    }

    case 'jarvis.wakeword.state':
      set((s: Store) => ({
        wakeStatus: {
          ...s.wakeStatus,
          phase: event.data.phase ?? s.wakeStatus.phase,
          detail: event.data.detail ?? '',
        },
      }));
      break;

    case 'boot.step':
      set((s: Store) => ({ bootSteps: [...s.bootSteps, event.data as BootStep] }));
      break;

    case 'boot.complete':
      set({ booted: true });
      break;

    // The wake word was heard over the top of a reply. Stop talking.
    case 'jarvis.speaking.stop':
      window.dispatchEvent(new CustomEvent('jarvis:silence'));
      break;

    case 'jarvis.speaking.start':
      // Automations and schedules can ask JARVIS to speak; the browser does it.
      //
      // Only act on an actual *request*. The browser also reports its own
      // speaking state through this same event, and treating that report as a
      // request makes every spoken reply trigger itself: speak -> report ->
      // broadcast -> speak. That is an infinite loop, and it is audible.
      if (event.data.text && event.data.source !== 'browser') {
        window.dispatchEvent(new CustomEvent('jarvis:speak', { detail: event.data.text }));
      }
      break;
  }
}
