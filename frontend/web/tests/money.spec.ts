import { describe, expect, it } from 'vitest';
import { add, cmp, formatEnIn, isPositiveAmount, parseAmount, sub, toCanonical, toDisplay } from '../src/money/exact';

const p = (s: string) => {
  const r = parseAmount(s);
  if (!r.ok) throw new Error(`parse failed for ${s}: ${r.error}`);
  return r.value;
};

describe('parseAmount (Law 3 on the wire)', () => {
  it('accepts plain decimals and strips grouping commas', () => {
    expect(p('1180').int).toBe(1180n);
    expect(p('1250.00')).toEqual({ neg: false, int: 125000n, scale: 2 });
    expect(p('1,18,000.50')).toEqual({ neg: false, int: 11800050n, scale: 2 });
    expect(p('12.')).toEqual({ neg: false, int: 12n, scale: 0 });
    expect(p(' 42 ')).toEqual({ neg: false, int: 42n, scale: 0 });
  });

  it('rejects everything a JSON float would sneak in as', () => {
    for (const bad of ['', '-', '5.5.5', '1e5', 'abc', 'NaN', 'Infinity', '0x10', '12,5.5.5']) {
      expect(parseAmount(bad).ok, bad).toBe(false);
    }
    expect(parseAmount('-10').ok).toBe(false); // sign belongs to D/C columns
  });

  it('enforces the engine limits: max 8 decimal places, magnitude below 10^18', () => {
    expect(parseAmount('0.123456789').ok).toBe(false);
    expect(parseAmount('0.12345678').ok).toBe(true);
    expect(parseAmount('1000000000000000000').ok).toBe(false);
    expect(parseAmount('999999999999999999.99').ok).toBe(false);
  });

  it('positivity is a separate check (0 parses, is not positive)', () => {
    expect(parseAmount('0').ok).toBe(true);
    expect(isPositiveAmount('0')).toBe(false);
    expect(isPositiveAmount('0.00')).toBe(false);
    expect(isPositiveAmount('0.01')).toBe(true);
  });
});

describe('exact arithmetic (no floats, ever)', () => {
  it('0.1 + 0.2 is exactly 0.3', () => {
    const sum = add(p('0.1'), p('0.2'));
    expect(cmp(sum, p('0.3'))).toBe(0);
    expect(toDisplay(sum)).toBe('0.30');
  });

  it('sums sub-cent amounts without drift', () => {
    let total = p('0');
    for (let i = 0; i < 1000; i++) total = add(total, p('0.001'));
    expect(toCanonical(total)).toBe('1');
  });

  it('subtracts across sides with exact signs', () => {
    expect(cmp(sub(p('1180'), p('1000')), p('180'))).toBe(0);
    const neg = sub(p('90'), p('1180'));
    expect(neg.neg).toBe(true);
    expect(toCanonical(neg)).toBe('-1090');
    expect(cmp(sub(p('5'), p('5')), p('0'))).toBe(0);
  });

  it('compares values at different scales correctly', () => {
    expect(cmp(p('2.50'), p('2.5'))).toBe(0);
    expect(cmp(p('2.50'), p('2.51'))).toBe(-1);
    expect(cmp(p('10'), p('9.99999999'))).toBe(1);
  });

  it('handles 8-decimal precision end to end', () => {
    const sum = add(p('0.00000001'), p('0.00000002'));
    expect(toCanonical(sum)).toBe('0.00000003');
    expect(toDisplay(sum)).toBe('0.00000003'); // never rounded to 2dp
  });
});

describe('formatting', () => {
  it('canonical strings trim trailing zeros', () => {
    expect(toCanonical(p('1180.00'))).toBe('1180');
    expect(toCanonical(p('1180.50'))).toBe('1180.5');
    expect(toCanonical(p('0.00'))).toBe('0');
  });

  it('display keeps at least two decimals without rounding', () => {
    expect(toDisplay(p('1180'))).toBe('1180.00');
    expect(toDisplay(p('1180.5'))).toBe('1180.50');
    expect(toDisplay(p('0.125'))).toBe('0.125');
  });

  it('en-IN grouping for display only', () => {
    expect(formatEnIn(p('1234567.5'))).toBe('12,34,567.50');
    expect(formatEnIn(p('1180'))).toBe('1,180.00');
    expect(formatEnIn(p('100000000'))).toBe('10,00,00,000.00');
  });
});
