import { useSyncExternalStore } from 'react';

export type ThemeName = 'amber' | 'streamai';

export const THEMES: { id: ThemeName; label: string; blurb: string }[] = [
  { id: 'amber', label: 'Amber', blurb: 'Warm gold · IBC signature' },
  { id: 'streamai', label: 'StreamAI', blurb: 'Cool navy · teal (NAB)' },
];

const KEY = 'hpm-theme';
const listeners = new Set<() => void>();

function read(): ThemeName {
  const v = localStorage.getItem(KEY);
  return v === 'streamai' ? 'streamai' : 'amber';
}

function apply(theme: ThemeName) {
  // Amber is the default (no attribute); only set for streamai.
  if (theme === 'streamai') {
    document.documentElement.setAttribute('data-theme', 'streamai');
  } else {
    document.documentElement.removeAttribute('data-theme');
  }
}

export function setTheme(theme: ThemeName) {
  localStorage.setItem(KEY, theme);
  apply(theme);
  listeners.forEach((l) => l());
}

/** Apply the stored theme on first load (call once at startup). */
export function initTheme() {
  apply(read());
}

export function useTheme(): [ThemeName, (t: ThemeName) => void] {
  const theme = useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    read,
    () => 'amber' as ThemeName,
  );
  return [theme, setTheme];
}
