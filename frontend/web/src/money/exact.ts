/**
 * Exact decimal arithmetic on strings, built on BigInt — financial Law 3 on
 * the client. No `number` is ever constructed: values live as decimal
 * strings on the wire and as { neg, int, scale } pairs here. This mirrors
 * the server's rules (src/ledgerforge/schemas.py): positive values, at most
 * 8 decimal places, magnitude below 10^18.
 */

export const MAX_SCALE = 8;
const MAX_MAGNITUDE = 10n ** 18n;

/** value = neg ? -(int / 10^scale) : int / 10^scale, with int >= 0n. */
export interface Exact {
  neg: boolean;
  int: bigint;
  scale: number;
}

export type ParseResult = { ok: true; value: Exact } | { ok: false; error: string };

const PLAIN_DECIMAL = /^\d+(\.\d*)?$/;

/**
 * Parse a human-entered amount. Grouping commas ("1,18,000.50") are accepted
 * and stripped — they are presentation, not value.
 */
export function parseAmount(raw: string): ParseResult {
  const s = raw.trim().replace(/,/g, '');
  if (s === '') return { ok: false, error: 'amount is empty' };
  if (!PLAIN_DECIMAL.test(s)) {
    return {
      ok: false,
      error: 'amount must be a plain decimal like 1250.00 (no sign, no exponents)',
    };
  }
  const parts = s.split('.');
  const ip = parts[0] ?? '';
  const fp = parts[1] ?? '';
  if (fp.length > MAX_SCALE) {
    return { ok: false, error: `amount supports at most ${MAX_SCALE} decimal places` };
  }
  const int = BigInt(ip + fp);
  if (int >= MAX_MAGNITUDE) return { ok: false, error: 'amount is too large (max 10^18)' };
  // "0" and "0.00" parse fine; positivity is checked by callers where required
  return { ok: true, value: { neg: false, int, scale: fp.length } };
}

/** True when the value parses and is strictly greater than zero. */
export function isPositiveAmount(raw: string): boolean {
  const parsed = parseAmount(raw);
  return parsed.ok && parsed.value.int > 0n;
}

function align(a: Exact, b: Exact): [bigint, bigint, number] {
  const scale = Math.max(a.scale, b.scale);
  return [
    a.int * 10n ** BigInt(scale - a.scale),
    b.int * 10n ** BigInt(scale - b.scale),
    scale,
  ];
}

/** Signed exact addition. */
export function add(a: Exact, b: Exact): Exact {
  const [ai, bi, scale] = align(a, b);
  if (a.neg === b.neg) return { neg: a.neg, int: ai + bi, scale };
  if (ai === bi) return { neg: false, int: 0n, scale };
  if (ai > bi) return { neg: a.neg, int: ai - bi, scale };
  return { neg: b.neg, int: bi - ai, scale };
}

/** Signed exact subtraction (a - b). */
export function sub(a: Exact, b: Exact): Exact {
  return add(a, { neg: !b.neg, int: b.int, scale: b.scale });
}

export function isZero(a: Exact): boolean {
  return a.int === 0n;
}

/** Exact comparison; -1 | 0 | 1. */
export function cmp(a: Exact, b: Exact): -1 | 0 | 1 {
  if (a.neg !== b.neg) return a.neg ? -1 : 1;
  const [ai, bi] = align(a, b);
  const m = ai < bi ? -1 : ai > bi ? 1 : 0;
  return a.neg ? (-m as -1 | 0 | 1) : m;
}

/**
 * Canonical wire string: no grouping, "0" for zero, trailing zeros trimmed
 * ("1180.00" -> "1180", "1180.50" -> "1180.5"). The server accepts any
 * <=8-dp string; this keeps payloads tidy and diffs readable.
 */
export function toCanonical(a: Exact): string {
  const sign = a.neg && a.int !== 0n ? '-' : '';
  if (a.int === 0n) return '0';
  if (a.scale === 0) return sign + a.int.toString();
  const digits = a.int.toString().padStart(a.scale + 1, '0');
  const ip = digits.slice(0, digits.length - a.scale);
  const fp = digits.slice(digits.length - a.scale).replace(/0+$/, '');
  return fp ? `${sign}${ip}.${fp}` : `${sign}${ip}`;
}

/**
 * Display string with at least 2 decimals (more if the value genuinely has
 * them — never rounded): "1180" -> "1180.00", "0.125" -> "0.125".
 */
export function toDisplay(a: Exact): string {
  return format(a, false);
}

/** en-IN digit grouping ("12,34,567.50") over the exact value — display only. */
export function formatEnIn(a: Exact): string {
  return format(a, true);
}

function format(a: Exact, group: boolean): string {
  const sign = a.neg && a.int !== 0n ? '-' : '';
  const digits = a.int.toString().padStart(a.scale + 1, '0');
  const ip = a.scale > 0 ? digits.slice(0, digits.length - a.scale) : digits;
  let fp = a.scale > 0 ? digits.slice(digits.length - a.scale).replace(/0+$/, '') : '';
  if (fp.length < 2) fp = fp.padEnd(2, '0');
  const ipText = group ? new Intl.NumberFormat('en-IN').format(BigInt(ip)) : ip;
  return `${sign}${ipText}.${fp}`;
}
