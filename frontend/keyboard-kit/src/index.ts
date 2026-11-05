/** @ledgerforge/keyboard-kit — framework-agnostic keyboard command framework. */

export * from './types.js';
export { CommandRegistry } from './commandRegistry.js';
export { FocusStack, type FocusScope } from './focusStack.js';
export {
  comboFromEvent,
  comboMatches,
  formatBinding,
  formatCombo,
  isComposing,
  isMacPlatform,
  parseBindingSpec,
  parseCombo,
} from './keybindings.js';
export { KeymapService, type KeymapOptions } from './keymapService.js';
export { tallyKeymap, TALLY_PRESETS } from './presets/tallyKeymap.js';
