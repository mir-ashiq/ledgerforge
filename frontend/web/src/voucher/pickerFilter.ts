/**
 * Type-ahead ranking for the ledger picker (docs/keyboard-ux.md §4):
 * exact match first, then prefix, then word-boundary substring, then plain
 * substring — case-insensitive, over the prefetched in-memory index.
 */

export interface PickEntry {
  name: string;
}

/** Lower score = better match; null = no match. */
function score(name: string, query: string): number | null {
  const n = name.toLowerCase();
  const q = query.trim().toLowerCase();
  if (q === '') return 50; // no filter: everything matches equally
  if (n === q) return 0;
  if (n.startsWith(q)) return 10;
  const idx = n.indexOf(q);
  if (idx === -1) return null;
  return idx > 0 && n[idx - 1] === ' ' ? 20 : 30; // word-boundary beats mid-word
}

/** Ranked, deterministic slice of `entries` for the typed query. */
export function rankEntries<T extends PickEntry>(entries: readonly T[], query: string, limit = 50): T[] {
  return entries
    .map((entry) => ({ entry, s: score(entry.name, query) }))
    .filter((r): r is { entry: T; s: number } => r.s !== null)
    .sort((a, b) => a.s - b.s || a.entry.name.localeCompare(b.entry.name))
    .slice(0, limit)
    .map((r) => r.entry);
}
