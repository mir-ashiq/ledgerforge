import { describe, expect, it } from 'vitest';
import { rankEntries } from '../src/voucher/pickerFilter';

const LEDGERS = [
  { id: 1, name: 'Cash' },
  { id: 2, name: 'Cash - Drawer 2' },
  { id: 3, name: 'Local Sales' },
  { id: 4, name: 'HDFC Current A/c' },
  { id: 5, name: 'Petty Cash Expenses' },
  { id: 6, name: 'Sales Returns' },
];

const names = (q: string) => rankEntries(LEDGERS, q).map((l) => l.name);

describe('picker ranking (exact → prefix → substring)', () => {
  it('empty query returns everything, alphabetical', () => {
    expect(names('')).toEqual([
      'Cash',
      'Cash - Drawer 2',
      'HDFC Current A/c',
      'Local Sales',
      'Petty Cash Expenses',
      'Sales Returns',
    ]);
  });

  it('exact match ranks first', () => {
    expect(names('cash')[0]).toBe('Cash');
  });

  it('prefix beats word-boundary substring beats mid-word substring', () => {
    const ranked = names('sales'); // 'Sales Returns' prefix vs 'Local Sales' word-boundary
    expect(ranked).toEqual(['Sales Returns', 'Local Sales']);
  });

  it('word-boundary substring beats mid-word substring', () => {
    // 'Cash - Drawer 2' has 'cash' as prefix; 'Petty Cash Expenses' at a word
    // boundary; nothing matches mid-word here except those two.
    expect(names('cash')).toEqual(['Cash', 'Cash - Drawer 2', 'Petty Cash Expenses']);
  });

  it('matching is case-insensitive and trims the query', () => {
    expect(names('  CASH  ')).toEqual(['Cash', 'Cash - Drawer 2', 'Petty Cash Expenses']);
  });

  it('no match yields an empty list', () => {
    expect(names('zzz')).toEqual([]);
  });

  it('caps the result list', () => {
    const many = Array.from({ length: 120 }, (_, i) => ({ id: i, name: `Ledger ${i}` }));
    expect(rankEntries(many, 'Ledger').length).toBe(50);
  });
});
