/** Registry of commands — the currency of the interaction model. */

import type { Command } from './types.js';

export class CommandRegistry {
  private commands = new Map<string, Command>();

  register(command: Command): void {
    if (!command.id || !command.title) {
      throw new Error('Command requires id and title');
    }
    if (this.commands.has(command.id)) {
      throw new Error(`Duplicate command id: ${command.id}`);
    }
    this.commands.set(command.id, command);
  }

  unregister(id: string): void {
    this.commands.delete(id);
  }

  get(id: string): Command | undefined {
    return this.commands.get(id);
  }

  /** All commands (palette source); each carries a human title. */
  list(): Command[] {
    return [...this.commands.values()];
  }
}
