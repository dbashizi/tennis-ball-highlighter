// Overlay settings, stored in chrome.storage.sync under one key.
import { SETTINGS_LIMITS as L, clamp } from "./geometry.js";

export const SETTINGS_KEY = "settings";

export const DEFAULT_SETTINGS = Object.freeze({
  enabled: true,
  ratio: L.ratio.def,
  minStroke: L.minStroke.def,
  minConf: L.minConf.def,
  debug: false,
  autoOffer: true,
});

function numOr(v, def, lim) {
  const n = Number(v);
  return Number.isFinite(n) ? clamp(n, lim.min, lim.max) : def;
}

/** Fill defaults and clamp to the allowed ranges. Pure. */
export function normalizeSettings(raw) {
  const s = raw && typeof raw === "object" ? raw : {};
  return {
    enabled: typeof s.enabled === "boolean" ? s.enabled : DEFAULT_SETTINGS.enabled,
    ratio: numOr(s.ratio, DEFAULT_SETTINGS.ratio, L.ratio),
    minStroke: numOr(s.minStroke, DEFAULT_SETTINGS.minStroke, L.minStroke),
    minConf: numOr(s.minConf, DEFAULT_SETTINGS.minConf, L.minConf),
    debug: typeof s.debug === "boolean" ? s.debug : DEFAULT_SETTINGS.debug,
    autoOffer: typeof s.autoOffer === "boolean" ? s.autoOffer : DEFAULT_SETTINGS.autoOffer,
  };
}

export async function loadSettings() {
  try {
    const got = await chrome.storage.sync.get(SETTINGS_KEY);
    return normalizeSettings(got[SETTINGS_KEY]);
  } catch {
    return normalizeSettings(null);
  }
}

export async function saveSettings(patch) {
  const cur = await loadSettings();
  const next = normalizeSettings({ ...cur, ...patch });
  await chrome.storage.sync.set({ [SETTINGS_KEY]: next });
  return next;
}

/** Calls `fn(settings)` whenever the stored settings change. Returns an unsubscribe. */
export function onSettingsChanged(fn) {
  const listener = (changes, area) => {
    if (area === "sync" && changes[SETTINGS_KEY]) fn(normalizeSettings(changes[SETTINGS_KEY].newValue));
  };
  chrome.storage.onChanged.addListener(listener);
  return () => chrome.storage.onChanged.removeListener(listener);
}
