/**
 * The bill-allocation dialog (ADR-023, Phase 2): `Alt+L` on a voucher whose
 * cursor line is a party ledger (Sundry Debtors / Creditors). Lists the
 * party's open bills from the engine's `bills-open` report, takes an amount
 * per bill, and offers the unallocated remainder as on-account (the
 * TallyPrime flow — the excess always has somewhere honest to go) or as a
 * new ref. Enter applies: the refs ride the post payload as `meta.bills`
 * and the engine enforces the conservation law (a 422 keeps the draft
 * intact). Esc cancels. All arithmetic is exact string/BigInt (Law 3) —
 * the dialog suggests, the engine decides.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';
import { api } from '../api/client';
import type { BillRef, OpenBill } from '../api/types';
import { add, cmp, isZero, parseAmount, sub, toCanonical, type Exact, ZERO } from '../money/exact';

interface BillAllocationDialogProps {
  party: string;
  /** The party's signed net movement in the draft (Σ Dr − Σ Cr), exact. */
  partyNet: Exact;
  onApply: (refs: BillRef[]) => void;
  onCancel: () => void;
}

type LoadState = 'loading' | 'ready' | 'error';

export function BillAllocationDialog({ party, partyNet, onApply, onCancel }: BillAllocationDialogProps) {
  const [bills, setBills] = useState<readonly OpenBill[]>([]);
  const [onAccount, setOnAccount] = useState('0.00');
  const [loadState, setLoadState] = useState<LoadState>('loading');
  const [amounts, setAmounts] = useState<Record<string, string>>({});
  const [remainderMode, setRemainderMode] = useState<'on_account' | 'new'>('on_account');
  const [newRef, setNewRef] = useState('');
  const [newDueDays, setNewDueDays] = useState('30');
  const [error, setError] = useState('');
  const firstInputRef = useRef<HTMLInputElement | null>(null);

  // a credit-moving voucher (receipt: Cr party) settles receivable (dr)
  // bills; a debit-moving one (payment) settles payable (cr) bills — the
  // direction of every ref this dialog builds follows the movement
  const creditMoves = partyNet.neg;
  const refSide = creditMoves ? 'cr' : 'dr';

  useFocusScope('billDialog', () => {
    onCancel();
    return true;
  });

  useEffect(() => {
    let cancelled = false;
    api
      .openBills(party)
      .then((data) => {
        if (cancelled) return;
        // settleable bills sit on the side opposite to the movement; the
        // same-side ones (advances against a receipt) are shown greyed so
        // the accountant always sees the whole position
        setBills(data.bills);
        setOnAccount(data.on_account);
        setLoadState('ready');
      })
      .catch(() => {
        if (!cancelled) setLoadState('error');
      });
    return () => {
      cancelled = true;
    };
  }, [party]);

  useEffect(() => {
    firstInputRef.current?.focus();
  }, [loadState]);

  const settleable = useMemo(
    () =>
      bills.filter((b) =>
        creditMoves ? Number(b.outstanding) > 0 : Number(b.outstanding) < 0,
      ),
    [bills, creditMoves],
  );
  const sameSide = useMemo(
    () =>
      bills.filter((b) =>
        creditMoves ? Number(b.outstanding) < 0 : Number(b.outstanding) > 0,
      ),
    [bills, creditMoves],
  );

  const magnitude = useMemo<Exact>(
    () => ({ neg: false, int: partyNet.int, scale: partyNet.scale }),
    [partyNet],
  );

  /** Σ valid allocations, exact. */
  const allocated = useMemo(() => {
    let sum = ZERO;
    for (const bill of settleable) {
      const raw = (amounts[bill.ref] ?? '').trim();
      if (!raw) continue;
      const parsed = parseAmount(raw);
      if (parsed.ok) sum = add(sum, parsed.value);
    }
    return sum;
  }, [amounts, settleable]);

  const remainder = useMemo(() => sub(magnitude, allocated), [magnitude, allocated]);

  function billProblem(bill: OpenBill): string | null {
    const raw = (amounts[bill.ref] ?? '').trim();
    if (!raw) return null;
    const parsed = parseAmount(raw);
    if (!parsed.ok) return parsed.error;
    const outstanding = parseAmount(bill.outstanding.replace('-', ''));
    if (outstanding.ok && cmp(parsed.value, outstanding.value) > 0) {
      return 'more than the bill\'s outstanding — the excess belongs to on-account';
    }
    return null;
  }

  function accept(): void {
    setError('');
    if (cmp(allocated, magnitude) > 0) {
      setError('Allocations exceed the voucher\'s party movement');
      return;
    }
    const refs: BillRef[] = [];
    for (const bill of settleable) {
      const raw = (amounts[bill.ref] ?? '').trim();
      if (!raw) continue;
      const parsed = parseAmount(raw);
      if (!parsed.ok || parsed.value.int === 0n) continue;
      const problem = billProblem(bill);
      if (problem) {
        setError(`Bill ${bill.ref}: ${problem}`);
        return;
      }
      refs.push({ kind: 'against', ref: bill.ref, [refSide]: toCanonical(parsed.value) } as BillRef);
    }
    if (!isZero(remainder)) {
      if (remainder.neg) {
        setError('Allocations exceed the voucher\'s party movement');
        return;
      }
      if (remainderMode === 'on_account') {
        refs.push({ kind: 'on_account', [refSide]: toCanonical(remainder) } as BillRef);
      } else {
        const name = newRef.trim();
        if (!name) {
          setError('Name the new bill (the ref) or choose on-account for the remainder');
          return;
        }
        const dueDays = Number(newDueDays.trim() || '0');
        if (!Number.isInteger(dueDays) || dueDays < 0) {
          setError('Due days must be a whole number ≥ 0');
          return;
        }
        refs.push({
          kind: 'new',
          ref: name,
          [refSide]: toCanonical(remainder),
          due_days: dueDays,
        } as BillRef);
      }
    }
    onApply(refs);
  }

  useCommand(
    { id: 'dialog.bills.accept', title: 'Apply bill allocation', run: accept },
    'Enter',
    (ctx) => ctx.scopeId === 'billDialog',
  );

  return (
    <div className="overlay" role="presentation">
      <div className="overlay-card overlay-wide" role="dialog" aria-label="Allocate to bills">
        <h2>Bills — {party}</h2>
        <p className="hint">
          This voucher {creditMoves ? 'credits' : 'debits'} <strong>{party}</strong> by{' '}
          <strong>{toCanonical(magnitude)}</strong>
          {creditMoves ? '' : ''} · on account today:{' '}
          <strong>{onAccount}</strong>
        </p>

        {loadState === 'loading' && <p className="hint">Reading open bills…</p>}
        {loadState === 'error' && (
          <p className="field-error" role="alert">
            Could not read the party's open bills — the engine may not be reachable.
          </p>
        )}

        {loadState === 'ready' && (
          <table className="alloc-table" aria-label="Open bills">
            <thead>
              <tr>
                <th>Ref</th>
                <th>Due</th>
                <th className="num">Outstanding</th>
                <th>Ageing</th>
                <th className="num">Allocate</th>
              </tr>
            </thead>
            <tbody>
              {settleable.map((bill, i) => (
                <tr key={bill.ref}>
                  <td>{bill.ref}</td>
                  <td>{bill.due_on}</td>
                  <td className="num">{bill.outstanding}</td>
                  <td>{bill.bucket}</td>
                  <td className="num">
                    <input
                      ref={i === 0 ? firstInputRef : undefined}
                      aria-label={`Allocate to ${bill.ref}`}
                      className="alloc-input"
                      inputMode="decimal"
                      placeholder="0.00"
                      value={amounts[bill.ref] ?? ''}
                      onChange={(e) => {
                        setAmounts((prev) => ({ ...prev, [bill.ref]: e.target.value }));
                        setError('');
                      }}
                      aria-invalid={billProblem(bill) !== null}
                    />
                  </td>
                </tr>
              ))}
              {sameSide.map((bill) => (
                <tr key={bill.ref} className="row-dim">
                  <td>{bill.ref}</td>
                  <td>{bill.due_on}</td>
                  <td className="num">{bill.outstanding}</td>
                  <td>{bill.bucket}</td>
                  <td className="num">—</td>
                </tr>
              ))}
              {!settleable.length && (
                <tr>
                  <td colSpan={5} className="empty-note">
                    No open bills on this side — the full amount can go on account or open a new ref.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}

        <div className="alloc-remainder">
          <span>
            Allocated <strong>{toCanonical(allocated)}</strong> · Remainder{' '}
            <strong>{toCanonical(remainder)}</strong>
          </span>
          <label>
            <input
              type="radio"
              name="remainder"
              checked={remainderMode === 'on_account'}
              onChange={() => setRemainderMode('on_account')}
            />{' '}
            remainder on account
          </label>
          <label>
            <input
              type="radio"
              name="remainder"
              checked={remainderMode === 'new'}
              onChange={() => setRemainderMode('new')}
            />{' '}
            open new ref
          </label>
          {remainderMode === 'new' && (
            <span className="alloc-newref">
              <input
                aria-label="New bill ref"
                placeholder="ref (e.g. INV-42)"
                value={newRef}
                onChange={(e) => {
                  setNewRef(e.target.value);
                  setError('');
                }}
              />
              <input
                aria-label="New bill due days"
                className="alloc-input"
                inputMode="numeric"
                placeholder="30"
                value={newDueDays}
                onChange={(e) => setNewDueDays(e.target.value)}
              />
              <span className="hint">due days</span>
            </span>
          )}
        </div>

        {error && <p className="field-error" role="alert">{error}</p>}
        <p className="overlay-foot">
          Enter apply · Esc cancel — the engine re-checks every allocation (a 422 keeps the draft)
        </p>
      </div>
    </div>
  );
}
