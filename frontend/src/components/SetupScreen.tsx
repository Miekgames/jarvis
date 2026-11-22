import { useEffect, useMemo, useState } from 'react';

import { api, ApiError } from '../lib/api';
import { useStore } from '../state/store';

/**
 * First-run setup: your name, which AI to use, your API key, what JARVIS may do.
 *
 * Shown automatically until it has been completed once, and again whenever
 * Settings asks for it (to change provider, model or key). Everything here
 * is real: "Load models" asks the provider which models the key can use,
 * "Test" sends one tiny message and reports exactly what came back, and the
 * key goes to this computer's JARVIS server once -- it is sealed there and
 * never sent back to the page.
 */

interface ProviderInfo {
  id: string;
  label: string;
  key_url: string;
  env: string;
  needs_base_url: boolean;
  key_optional: boolean;
  default_model: string;
}

interface KeyInfo { has_key: boolean; source: 'saved' | 'environment' | null; hint: string }

interface SetupState {
  complete: boolean;
  owner_name: string;
  assistant_name: string;
  tone: string;
  weather_location: string;
  providers: ProviderInfo[];
  keys: Record<string, KeyInfo>;
  cloud: { provider?: string; model?: string; fast_model?: string; base_url?: string };
  ollama: { available: boolean };
  local_only: boolean;
  can_store_keys: boolean;
  tones: string[];
}

interface TestResult { ok: boolean; reply?: string; model?: string; latency_ms?: number; error?: string }

const TONE_LABEL: Record<string, string> = {
  composed: 'Composed', warm: 'Warm', terse: 'Brief', playful: 'Playful',
};

const PRESETS = [
  {
    id: 'careful', label: 'Careful',
    help: 'Looks things up and reads — files, the web, your calendar. Changes nothing on its own.',
  },
  {
    id: 'recommended', label: 'Recommended',
    help: 'Also writes files, opens apps and runs automations. Deleting, shell commands, power and email stay off until you switch them on.',
  },
  {
    id: 'everything', label: 'Everything',
    help: 'Every capability on, including running commands and deleting files. Risky actions still ask you first.',
  },
];

const STEPS = ['You', 'Your AI', 'Permissions', 'Ready'];

export function SetupScreen() {
  const open = useStore((s) => s.setupOpen);
  const setOpen = useStore((s) => s.setSetupOpen);
  const authRequired = useStore((s) => s.authRequired);

  const [state, setState] = useState<SetupState | null>(null);
  const [step, setStep] = useState(0);
  const [loadError, setLoadError] = useState('');

  // the answers
  const [ownerName, setOwnerName] = useState('');
  const [assistantName, setAssistantName] = useState('JARVIS');
  const [tone, setTone] = useState('composed');
  const [provider, setProvider] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [weather, setWeather] = useState('');
  const [preset, setPreset] = useState('recommended');

  // live results
  const [models, setModels] = useState<string[]>([]);
  const [modelsBusy, setModelsBusy] = useState(false);
  const [modelsError, setModelsError] = useState('');
  const [test, setTest] = useState<TestResult | null>(null);
  const [testBusy, setTestBusy] = useState(false);
  const [saveBusy, setSaveBusy] = useState(false);
  const [saveError, setSaveError] = useState('');

  // Load once signed in; open by itself on a first run.
  useEffect(() => {
    if (authRequired) return;
    let cancelled = false;
    api.get<SetupState>('/setup')
      .then((loaded) => {
        if (cancelled) return;
        setState(loaded);
        setOwnerName(loaded.owner_name || '');
        setAssistantName(loaded.assistant_name || 'JARVIS');
        setTone(loaded.tone || 'composed');
        setWeather(loaded.weather_location || '');
        setProvider(loaded.cloud.provider || '');
        setModel(loaded.cloud.model || '');
        setBaseUrl(loaded.cloud.base_url || '');
        if (!loaded.complete) setOpen(true);
      })
      .catch((exc) => setLoadError(exc instanceof ApiError ? exc.message : 'Setup could not load.'));
    return () => { cancelled = true; };
  }, [authRequired, open, setOpen]);

  const info = useMemo(
    () => state?.providers.find((p) => p.id === provider) ?? null, [state, provider]);
  const keyInfo = (state && provider && state.keys[provider]) || null;
  const isCloud = !!info;
  const localChosen = provider === 'ollama';
  const keyReady = !isCloud || !!apiKey.trim() || !!keyInfo?.has_key || !!info?.key_optional;
  const aiReady = localChosen
    || (isCloud && keyReady && !!model.trim() && (!info?.needs_base_url || !!baseUrl.trim()));

  const choose = (id: string) => {
    if (id === provider) return;
    setProvider(id);
    setModels([]);
    setModelsError('');
    setTest(null);
    setApiKey('');
    const chosen = state?.providers.find((p) => p.id === id);
    setModel(id === state?.cloud.provider ? state?.cloud.model || '' : chosen?.default_model || '');
  };

  const loadModels = async () => {
    if (!info) return;
    setModelsBusy(true);
    setModelsError('');
    try {
      const result = await api.post<{ models: string[] }>('/setup/models',
        { provider, api_key: apiKey, base_url: baseUrl });
      setModels(result.models);
      if (!result.models.length) {
        setModelsError('The provider listed no chat models for this key. You can still type a model name.');
      } else if (!model || !result.models.includes(model)) {
        const preferred = info.default_model && result.models.includes(info.default_model)
          ? info.default_model : result.models[0];
        setModel(preferred);
      }
    } catch (exc) {
      setModelsError(exc instanceof ApiError ? exc.message : 'Could not load models.');
    } finally {
      setModelsBusy(false);
    }
  };

  const runTest = async () => {
    setTestBusy(true);
    setTest(null);
    try {
      setTest(await api.post<TestResult>('/setup/test',
        { provider, api_key: apiKey, base_url: baseUrl, model }));
    } catch (exc) {
      setTest({ ok: false, error: exc instanceof ApiError ? exc.message : 'The test failed.' });
    } finally {
      setTestBusy(false);
    }
  };

  const finish = async () => {
    setSaveBusy(true);
    setSaveError('');
    try {
      const saved = await api.post<SetupState>('/setup/save', {
        owner_name: ownerName, assistant_name: assistantName, tone,
        provider, api_key: apiKey, model, base_url: baseUrl,
        weather_location: weather, capabilities: preset,
      });
      setState(saved);
      setApiKey('');
      await useStore.getState().loadSettings();
      setOpen(false);
      setStep(0);
      useStore.getState().notify(
        `${saved.assistant_name} is ready${saved.owner_name ? `, ${saved.owner_name}` : ''}.`);
    } catch (exc) {
      setSaveError(exc instanceof ApiError ? exc.message : 'Could not save.');
    } finally {
      setSaveBusy(false);
    }
  };

  if (authRequired || !open) return null;

  if (!state) {
    return (
      <div className="setup" role="dialog" aria-modal="true" aria-label="Setup">
        <div className="setup__box">
          <div className="setup__mark">JARVIS</div>
          <p className="setup__hint">{loadError || 'Loading setup…'}</p>
        </div>
      </div>
    );
  }

  const canLeave = state.complete;

  return (
    <div className="setup" role="dialog" aria-modal="true" aria-labelledby="setup-title">
      <div className="setup__box">
        <div className="setup__top">
          <div className="setup__mark">{(assistantName || 'JARVIS').toUpperCase()}</div>
          {canLeave && (
            <button className="btn btn--small" onClick={() => { setOpen(false); setStep(0); }}>
              Close
            </button>
          )}
        </div>

        <ol className="setup__steps" aria-label="Setup steps">
          {STEPS.map((label, index) => (
            <li key={label} className={`setup__step ${index === step ? 'setup__step--now' : ''}
                                        ${index < step ? 'setup__step--done' : ''}`}>
              {label}
            </li>
          ))}
        </ol>

        {step === 0 && (
          <section className="setup__page">
            <h2 id="setup-title" className="setup__title">
              {state.complete ? 'Change your setup' : 'Hello. Let’s get you set up.'}
            </h2>
            <label className="setup__field">
              <span>What should I call you?</span>
              <input className="setup__input" value={ownerName} maxLength={60} autoFocus
                     placeholder="Your name" onChange={(e) => setOwnerName(e.target.value)} />
            </label>
            <label className="setup__field">
              <span>What should I be called?</span>
              <input className="setup__input" value={assistantName} maxLength={30}
                     onChange={(e) => setAssistantName(e.target.value)} />
            </label>
            <div className="setup__field">
              <span>How should I talk?</span>
              <div className="setup__chips">
                {state.tones.map((t) => (
                  <button key={t} type="button"
                          className={`setup__chip ${tone === t ? 'setup__chip--on' : ''}`}
                          aria-pressed={tone === t} onClick={() => setTone(t)}>
                    {TONE_LABEL[t] ?? t}
                  </button>
                ))}
              </div>
            </div>
          </section>
        )}

        {step === 1 && (
          <section className="setup__page">
            <h2 id="setup-title" className="setup__title">Which AI should I run on?</h2>
            <p className="setup__hint">
              Use your own API key from any of these. You pay the provider directly for what
              you use; nothing goes through anyone else.
            </p>
            {state.local_only && (
              <p className="setup__warn">
                This copy is set to local-only (JARVIS_AI_PROVIDER=ollama in .env), so cloud
                providers are switched off.
              </p>
            )}
            <div className="setup__providers">
              {state.providers.map((p) => (
                <button key={p.id} type="button" disabled={state.local_only}
                        className={`setup__provider ${provider === p.id ? 'setup__provider--on' : ''}`}
                        aria-pressed={provider === p.id} onClick={() => choose(p.id)}>
                  <span>{p.label}</span>
                  {state.keys[p.id]?.has_key && <small>key saved</small>}
                </button>
              ))}
              <button type="button"
                      className={`setup__provider ${localChosen ? 'setup__provider--on' : ''}`}
                      aria-pressed={localChosen} onClick={() => choose('ollama')}>
                <span>Local only (Ollama)</span>
                <small>{state.ollama.available ? 'running on this PC' : 'not detected'}</small>
              </button>
            </div>

            {localChosen && (
              <p className="setup__hint">
                {state.ollama.available
                  ? 'Ollama is running here, so I’ll use your local models. Nothing leaves this computer.'
                  : 'Ollama isn’t running on this computer. Install it from ollama.com and pull a model (for example “ollama pull qwen2.5”), or pick a cloud provider.'}
              </p>
            )}

            {info && (
              <div className="setup__detail">
                {info.needs_base_url && (
                  <label className="setup__field">
                    <span>API address</span>
                    <input className="setup__input" value={baseUrl} spellCheck={false}
                           placeholder="https://example.com/v1"
                           onChange={(e) => { setBaseUrl(e.target.value); setModels([]); setTest(null); }} />
                  </label>
                )}

                {state.can_store_keys ? (
                  <label className="setup__field">
                    <span>
                      API key{info.key_optional ? ' (if the service needs one)' : ''}
                      {info.key_url && (
                        <a href={info.key_url} target="_blank" rel="noreferrer noopener"
                           className="setup__link">Get a key ↗</a>
                      )}
                    </span>
                    <input className="setup__input setup__input--key" type="password"
                           value={apiKey} autoComplete="off" spellCheck={false}
                           placeholder={keyInfo?.has_key
                             ? `Saved (${keyInfo.source === 'environment' ? 'from .env' : keyInfo.hint}) — leave blank to keep it`
                             : 'Paste your key'}
                           onChange={(e) => { setApiKey(e.target.value); setTest(null); }} />
                    <small className="setup__note">
                      Sealed on this computer with Windows’ own encryption and never shown again.
                    </small>
                  </label>
                ) : (
                  <p className="setup__warn">
                    This computer can’t seal a key, so JARVIS won’t store one in a file. Add{' '}
                    <code>{info.env}=your-key</code> to the <code>.env</code> file next to JARVIS
                    and restart it.{keyInfo?.source === 'environment' && ' (A key is already set there.)'}
                  </p>
                )}

                <div className="setup__field">
                  <span>Model</span>
                  <div className="setup__row">
                    <input className="setup__input" list="setup-models" value={model}
                           spellCheck={false} placeholder="Load the list, or type a model name"
                           onChange={(e) => { setModel(e.target.value); setTest(null); }} />
                    <datalist id="setup-models">
                      {models.map((m) => <option key={m} value={m} />)}
                    </datalist>
                    <button type="button" className="btn btn--small"
                            disabled={modelsBusy || !keyReady || (info.needs_base_url && !baseUrl.trim())}
                            onClick={() => void loadModels()}>
                      {modelsBusy ? 'Asking…' : models.length ? `${models.length} found` : 'Load models'}
                    </button>
                  </div>
                  {modelsError && <small className="setup__error">{modelsError}</small>}
                </div>

                <div className="setup__row">
                  <button type="button" className="btn btn--small"
                          disabled={testBusy || !aiReady} onClick={() => void runTest()}>
                    {testBusy ? 'Testing…' : 'Test it'}
                  </button>
                  {test && (test.ok ? (
                    <span className="setup__ok">
                      ✓ {test.model} answered “{(test.reply || '').slice(0, 40) || '(empty)'}” in{' '}
                      {test.latency_ms} ms
                    </span>
                  ) : (
                    <span className="setup__error">{test.error}</span>
                  ))}
                </div>
              </div>
            )}
          </section>
        )}

        {step === 2 && (
          <section className="setup__page">
            <h2 id="setup-title" className="setup__title">What may I do on this computer?</h2>
            <div className="setup__presets">
              {PRESETS.map((p) => (
                <button key={p.id} type="button"
                        className={`setup__preset ${preset === p.id ? 'setup__preset--on' : ''}`}
                        aria-pressed={preset === p.id} onClick={() => setPreset(p.id)}>
                  <strong>{p.label}</strong>
                  <span>{p.help}</span>
                </button>
              ))}
            </div>
            <p className="setup__hint">
              Each capability has its own switch in Settings, so you can change any of this later.
            </p>
            <label className="setup__field">
              <span>Your town or city, for the weather (optional)</span>
              <input className="setup__input" value={weather} maxLength={80}
                     placeholder="e.g. Manchester, UK" onChange={(e) => setWeather(e.target.value)} />
            </label>
          </section>
        )}

        {step === 3 && (
          <section className="setup__page">
            <h2 id="setup-title" className="setup__title">Ready when you are.</h2>
            <dl className="setup__summary">
              <dt>You</dt><dd>{ownerName.trim() || '—'}</dd>
              <dt>Me</dt><dd>{assistantName.trim() || 'JARVIS'}, {TONE_LABEL[tone]?.toLowerCase()}</dd>
              <dt>AI</dt>
              <dd>{localChosen ? 'Local models (Ollama)' : `${info?.label ?? '—'} · ${model || '—'}`}</dd>
              <dt>Key</dt>
              <dd>
                {localChosen ? 'Not needed'
                  : apiKey.trim() ? 'New key — sealed when you start'
                  : keyInfo?.has_key ? `Keeping the saved one (${keyInfo.source === 'environment' ? '.env' : keyInfo.hint})`
                  : info?.key_optional ? 'None' : 'Missing'}
              </dd>
              <dt>Permissions</dt><dd>{PRESETS.find((p) => p.id === preset)?.label}</dd>
              <dt>Tested</dt>
              <dd>{localChosen ? '—' : test?.ok ? 'Yes, it answered' : test ? 'Failed — see the AI step' : 'Not yet (optional)'}</dd>
            </dl>
            {saveError && <p className="setup__error">{saveError}</p>}
          </section>
        )}

        <div className="setup__nav">
          <button type="button" className="btn" disabled={step === 0}
                  onClick={() => setStep(step - 1)}>Back</button>
          {step < STEPS.length - 1 ? (
            <button type="button" className="btn btn--primary"
                    disabled={step === 1 && !aiReady}
                    onClick={() => setStep(step + 1)}>Next</button>
          ) : (
            <button type="button" className="btn btn--primary"
                    disabled={saveBusy || !aiReady} onClick={() => void finish()}>
              {saveBusy ? 'Saving…' : state.complete ? 'Save' : 'Start'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
