/**
 * Keybinding parsing and matching (platform-normalized).
 *
 * Examples: 'Ctrl+A' · 'Alt+C' · 'F5' · 'Ctrl+Enter' · 'K K' (chord).
 * 'Primary' maps to Cmd on macOS and Ctrl elsewhere.
 */

import type { KeyCombo } from './types.js';

const KEY_ALIASES: Record<string, string> = {
  ctrl: 'ctrl',
  control: 'ctrl',
  cmd: 'meta',
  cmdorctrl: 'primary',
  ctrlorcmd: 'primary',
  primary: 'primary',
  meta: 'meta',
  command: 'meta',
  win: 'meta',
  alt: 'alt',
  option: 'alt',
  shift: 'shift',
  esc: 'Escape',
  escape: 'Escape',
  enter: 'Enter',
  return: 'Enter',
  space: ' ',
  up: 'ArrowUp',
  down: 'ArrowDown',
  left: 'ArrowLeft',
  right: 'ArrowRight',
  del: 'Delete',
  ins: 'Insert',
};

export function isMacPlatform(): boolean {
  return typeof navigator !== 'undefined' && /mac|iphone|ipad/i.test(navigator.platform ?? '');
}

/** Parse a single combo string like 'Ctrl+Alt+A' into a KeyCombo. */
export function parseCombo(spec: string): KeyCombo {
  const parts = spec.split('+').map((p) => p.trim()).filter(Boolean);
  if (parts.length === 0) throw new Error(`Empty key combo: ${spec!}`);
  const combo: KeyCombo = { key: '', ctrl: false, meta: false, alt: false, shift: false };
  for (const raw of parts) {
    const part = KEY_ALIASES[raw.toLowerCase()] ?? raw;
    switch (part) {
      case 'ctrl': combo.ctrl = true; break;
      case 'meta': combo.meta = true; break;
      case 'alt': combo.alt = true; break;
      case 'shift': combo.shift = true; break;
      case 'primary':
        if (isMacPlatform()) combo.meta = true;
        else combo.ctrl = true;
        break;
      default:
        if (combo.key) throw new Error(`Multiple non-modifier keys in combo: ${spec}`);
        combo.key = part.length === 1 ? part.toLowerCase() : part;
    }
  }
  if (!combo.key) throw new Error(`No key in combo: ${spec}`);
  return combo;
}

/** Parse a binding spec; a space-separated pair ('K K') is a chord. */
export function parseBindingSpec(spec: string): KeyCombo[] {
  return spec.split(/\s+/).filter(Boolean).map(parseCombo);
}

/** True while an IME composition is active (never steal these events). */
export function isComposing(event: KeyboardEvent): boolean {
  return event.isComposing || event.keyCode === 229;
}

/** Build the KeyCombo a raw KeyboardEvent represents. */
export function comboFromEvent(event: KeyboardEvent): KeyCombo {
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  return { key, ctrl: event.ctrlKey, meta: event.metaKey, alt: event.altKey, shift: event.shiftKey };
}

/** Exact match of a combo against an event's combo. */
export function comboMatches(a: KeyCombo, b: KeyCombo): boolean {
  // Shift is only significant for non-printing keys: 'A' with shift should
  // still match a binding on 'a' (the text case belongs to the input).
  const shiftRelevant = a.key.length > 1;
  return (
    a.key === b.key &&
    a.ctrl === b.ctrl &&
    a.meta === b.meta &&
    a.alt === b.alt &&
    (!shiftRelevant || a.shift === b.shift)
  );
}

/** Human-readable form, e.g. 'Ctrl+A' (for the palette). */
export function formatCombo(combo: KeyCombo): string {
  const parts: string[] = [];
  if (combo.ctrl) parts.push('Ctrl');
  if (combo.meta) parts.push(isMacPlatform() ? 'Cmd' : 'Meta');
  if (combo.alt) parts.push('Alt');
  if (combo.shift && combo.key.length > 1) parts.push('Shift');
  parts.push(combo.key.length === 1 ? combo.key.toUpperCase() : combo.key);
  return parts.join('+');
}

export function formatBinding(combo: KeyCombo[]): string {
  return combo.map(formatCombo).join(' ');
}
