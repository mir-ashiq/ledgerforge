/**
 * localStorage-backed KeymapStore — user keybindings survive reloads
 * (keyboard-ux §8 phase 3). Everything is defensive: a corrupted or
 * unwritable store must never break the keymap.
 */

import { parseBindingSpec, type KeymapStore } from '@ledgerforge/keyboard-kit';

const STORAGE_KEY = 'ledgerforge.keymap.v1';

function isValidSnapshot(value: unknown): value is Record<string, string> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  return Object.entries(value).every(
    ([commandId, spec]) => typeof commandId === 'string' && typeof spec === 'string',
  );
}

export const localStorageKeymapStore: KeymapStore = {
  load(): Record<string, string> {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return {};
      const parsed: unknown = JSON.parse(raw);
      if (!isValidSnapshot(parsed)) return {};
      // drop entries whose spec no longer parses (format drift, typos)
      return Object.fromEntries(
        Object.entries(parsed).filter(([, spec]) => {
          try {
            parseBindingSpec(spec);
            return true;
          } catch {
            return false;
          }
        }),
      );
    } catch {
      return {};
    }
  },

  save(userBindings: Record<string, string>): void {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(userBindings));
    } catch {
      /* private mode / quota: persistence is best-effort */
    }
  },
};

/** Test helper: forget persisted bindings between cases. */
export function clearPersistedKeymap(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* ignore */
  }
}
