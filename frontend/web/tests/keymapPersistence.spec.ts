/**
 * keyboard-kit keymap persistence (keyboard-ux §8 phase 3) — the layer that
 * makes user rebindings real: defaults vs user bindings, the one-combo-one-
 * command steal rule, reset, persistence round-trips, change events and the
 * rebind capture mode. Pure core tests; no React involved.
 */

import { describe, expect, it, vi } from 'vitest';
import {
  KeymapService,
  type KeymapStore,
  parseBindingSpec,
  formatBinding,
} from '@ledgerforge/keyboard-kit';

function makeService(): KeymapService {
  return new KeymapService();
}

function register(service: KeymapService, id: string, spec: string, when?: Parameters<KeymapService['bind']>[2]): void {
  service.registerCommand({ id, title: id, run: () => undefined });
  service.bind(spec, id, when);
}

function fakeStore(initial: Record<string, string> = {}): KeymapStore & { saved: Record<string, string>[] } {
  const state = { ...initial };
  const saved: Record<string, string>[] = [];
  return {
    load: () => ({ ...state }),
    save(map) {
      saved.push({ ...map });
      Object.assign(state, map);
    },
    get saved() {
      return saved;
    },
  } as KeymapStore & { saved: Record<string, string>[] };
}

describe('keymap persistence — defaults and user bindings', () => {
  it('tracks defaults and reports them as non-user', () => {
    const km = makeService();
    register(km, 'voucher.accept', 'Primary+A');
    expect(km.bindingInfo('voucher.accept')).toEqual({ spec: 'Primary+A', isUser: false, displaced: false });
  });

  it('setKeybinding replaces the binding, marks it user, and persists', () => {
    const km = makeService();
    const store = fakeStore();
    register(km, 'voucher.accept', 'Primary+A');
    void km.attachPersistence(store);

    km.setKeybinding('Primary+J', 'voucher.accept');

    expect(km.bindingInfo('voucher.accept')).toEqual({ spec: 'Primary+J', isUser: true, displaced: false });
    expect(km.listBindings().find((b) => b.command === 'voucher.accept')?.combo).toEqual(
      parseBindingSpec('Primary+J'),
    );
    expect(store.saved.at(-1)).toEqual({ 'voucher.accept': 'Primary+J' });
  });

  it('throws on unknown commands and malformed specs without mutating', () => {
    const km = makeService();
    register(km, 'voucher.accept', 'Primary+A');
    expect(() => km.setKeybinding('Primary+J', 'nope.command')).toThrow();
    expect(() => km.setKeybinding('Ctrl+++', 'voucher.accept')).toThrow();
    expect(km.bindingInfo('voucher.accept')?.spec).toBe('Primary+A');
    expect(km.userBindings()).toEqual({});
  });

  it('a user binding survives unregister/re-register (screen remount)', () => {
    const km = makeService();
    register(km, 'voucher.accept', 'Primary+A');
    km.setKeybinding('Primary+J', 'voucher.accept');

    km.unregisterCommand('voucher.accept');
    expect(km.bindingInfo('voucher.accept')).toBeNull();
    register(km, 'voucher.accept', 'Primary+A'); // remount registers the default…

    expect(km.bindingInfo('voucher.accept')?.spec).toBe('Primary+J'); // …but the user binding wins
  });
});

describe('keymap persistence — one combo, one command', () => {
  it('rebinding onto another command’s combo displaces it (unbound, default recorded)', () => {
    const km = makeService();
    register(km, 'voucher.deleteLine', 'Alt+D');
    register(km, 'voucher.accept', 'Primary+A');

    km.setKeybinding('Alt+D', 'voucher.accept');

    expect(km.bindingInfo('voucher.accept')?.spec).toBe('Alt+D');
    const displaced = km.bindingInfo('voucher.deleteLine');
    expect(displaced?.isUser).toBe(false);
    expect(displaced?.displaced).toBe(true);
    expect(km.listBindings().some((b) => b.command === 'voucher.deleteLine')).toBe(false);
  });

  it('a displaced command comes back once the combo is freed', () => {
    const km = makeService();
    register(km, 'voucher.deleteLine', 'Alt+D');
    register(km, 'voucher.accept', 'Primary+A');
    km.setKeybinding('Alt+D', 'voucher.accept');

    km.resetKeybinding('voucher.accept');

    expect(km.bindingInfo('voucher.accept')?.spec).toBe('Primary+A');
    expect(km.bindingInfo('voucher.deleteLine')).toEqual({ spec: 'Alt+D', isUser: false, displaced: false });
  });

  it('late default registrations respect a reserved combo', () => {
    const km = makeService();
    register(km, 'voucher.accept', 'Primary+A');
    km.setKeybinding('F5', 'voucher.accept');

    register(km, 'voucher.setType.contra', 'F5'); // mounts after the user rebind

    expect(km.bindingInfo('voucher.setType.contra')?.displaced).toBe(true);
    expect(km.listBindings().filter((b) => b.command === 'voucher.setType.contra')).toHaveLength(0);
  });

  it('two user bindings never share a combo: the older one reverts', () => {
    const km = makeService();
    register(km, 'a.first', 'Primary+1');
    register(km, 'a.second', 'Primary+2');
    km.setKeybinding('Primary+9', 'a.first');
    km.setKeybinding('Primary+9', 'a.second');

    expect(km.bindingInfo('a.second')?.spec).toBe('Primary+9');
    expect(km.bindingInfo('a.first')?.spec).toBe('Primary+1'); // back to default
    expect(km.userBindings()).toEqual({ 'a.second': 'Primary+9' });
  });

  it('scope predicates travel with the binding', () => {
    const km = makeService();
    const when = (ctx: { scopeId: string | null }) => ctx.scopeId === 'voucherEditor';
    register(km, 'voucher.accept', 'Primary+A', when);
    km.setKeybinding('Primary+J', 'voucher.accept');

    const binding = km.listBindings().find((b) => b.command === 'voucher.accept');
    expect(binding?.when).toBe(when);
  });
});

describe('keymap persistence — reset & store', () => {
  it('resetAllKeybindings restores the shipped map and clears the store payload', () => {
    const km = makeService();
    const store = fakeStore();
    register(km, 'voucher.accept', 'Primary+A');
    register(km, 'voucher.deleteLine', 'Alt+D');
    void km.attachPersistence(store);
    km.setKeybinding('Alt+D', 'voucher.accept');

    km.resetAllKeybindings();

    expect(km.bindingInfo('voucher.accept')?.spec).toBe('Primary+A');
    expect(km.bindingInfo('voucher.deleteLine')?.displaced).toBe(false);
    expect(store.saved.at(-1)).toEqual({});
  });

  it('attachPersistence loads saved bindings and skips junk', async () => {
    const km = makeService();
    register(km, 'voucher.accept', 'Primary+A');
    register(km, 'voucher.deleteLine', 'Alt+D');

    await km.attachPersistence(
      fakeStore({
        'voucher.accept': 'Primary+J',
        'voucher.deleteLine': 'Ctrl+', // invalid: no key — dropped
        'voucher.new': '', // invalid: empty spec — dropped
        'gone.command': 'F9', // unknown command: kept pending, harmless
      }),
    );

    expect(km.bindingInfo('voucher.accept')?.spec).toBe('Primary+J');
    expect(km.bindingInfo('voucher.deleteLine')?.spec).toBe('Alt+D');
  });

  it('a broken store never breaks the keymap', async () => {
    const km = makeService();
    register(km, 'voucher.accept', 'Primary+A');
    await expect(
      km.attachPersistence({
        load: () => {
          throw new Error('boom');
        },
        save: () => {
          throw new Error('boom');
        },
      }),
    ).resolves.toBeUndefined();
    expect(km.bindingInfo('voucher.accept')?.spec).toBe('Primary+A');
  });

  it('onChange fires when bindings change', () => {
    const km = makeService();
    register(km, 'voucher.accept', 'Primary+A');
    const listener = vi.fn();
    km.onChange(listener);

    km.setKeybinding('Primary+J', 'voucher.accept');
    km.resetKeybinding('voucher.accept');

    expect(listener).toHaveBeenCalledTimes(2);
  });
});

describe('keymap persistence — rebind capture mode', () => {
  it('no command matches while capturing; matching resumes after', () => {
    const km = makeService();
    const runs: string[] = [];
    km.registerCommand({ id: 'voucher.accept', title: 'Accept', run: () => { runs.push('accept'); } });
    km.bind('Primary+A', 'voucher.accept');
    km.start();

    const event = new KeyboardEvent('keydown', { key: 'a', ctrlKey: true, bubbles: true, cancelable: true });

    km.beginCapture();
    expect(km.isCapturing()).toBe(true);
    expect(km.handleEvent(event)).toBe(false);
    expect(runs).toEqual([]);

    km.endCapture();
    expect(km.handleEvent(event)).toBe(true);
    expect(runs).toEqual(['accept']);

    km.stop();
  });
});

describe('keymap persistence — spec formatting round-trips', () => {
  it('specs survive save → load → parse', () => {
    const km = makeService();
    const store = fakeStore();
    register(km, 'voucher.accept', 'Primary+A');
    void km.attachPersistence(store);
    km.setKeybinding('Ctrl+Shift+P', 'voucher.accept');

    const spec = store.saved.at(-1)?.['voucher.accept'];
    expect(spec).toBe('Ctrl+Shift+P');
    expect(formatBinding(parseBindingSpec(spec!))).toBe('Ctrl+Shift+P');
  });
});
