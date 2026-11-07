/**
 * The cost-allocation dialog (ADR-025, Phase 4): `Alt+K` in the voucher
 * screen. One row per complete draft line with its allocated-so-far and its
 * remainder — always visible, the roadmap's law — and for the picked line a
 * split editor (centre + amount per row). A line is either fully allocated
 * or left unallocated; the dialog proves it client-side with the same exact
 * BigInt arithmetic (Law 3) the engine re-proves at posting time, and a
 * refusal keeps the draft intact. Esc cancels.
 *
 * Centres can be created inline (the Alt+C philosophy): name + parent +
 * category — no detour out of the entry flow.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';
import { api } from '../api/client';
import type { CostAllocationIn, CostCategoryOut, CostCentreOut, Dc } from '../api/types';
import { add, cmp, isZero, parseAmount, sub, toCanonical, ZERO, type Exact } from '../money/exact';

/** One complete draft line, as the dialog sees it. */
export interface AllocatableLine {
  ledgerName: string;
  dc: Dc;
  amount: Exact;
}

interface CostAllocationDialogProps {
  lines: readonly AllocatableLine[];
  centres: readonly CostCentreOut[];
  categories: readonly CostCategoryOut[];
  onApply: (splits: CostAllocationIn[][]) => void;
  onCancel: () => void;
}

/** One editable split row of the picked line. */
interface SplitRow {
  centreId: number | null;
  amount: string;
}

function centreLabel(centre: CostCentreOut): string {
  return centre.category_name ? `${centre.name} (${centre.category_name})` : centre.name;
}

export function CostAllocationDialog({
  lines,
  centres,
  categories,
  onApply,
  onCancel,
}: CostAllocationDialogProps) {
  const realCentres = useMemo(() => centres.filter((c) => !c.is_root), [centres]);
  const realCategories = useMemo(() => categories.filter((c) => !c.is_root), [categories]);
  const [picked, setPicked] = useState(0);
  /** per line: the split rows — [] is the explicit "left unallocated". */
  const [splits, setSplits] = useState<SplitRow[][]>(() => lines.map(() => []));
  const [error, setError] = useState('');
  const [newName, setNewName] = useState('');
  const [newParent, setNewParent] = useState('');
  const [newCategory, setNewCategory] = useState('');
  const [extraCentres, setExtraCentres] = useState<CostCentreOut[]>([]);
  const firstInputRef = useRef<HTMLSelectElement | null>(null);

  const allCentres = useMemo(() => [...realCentres, ...extraCentres], [realCentres, extraCentres]);

  useFocusScope('costDialog', () => {
    onCancel();
    return true;
  });

  useEffect(() => {
    firstInputRef.current?.focus();
  }, [picked]);

  // ------------------------------------------------------------ arithmetic
  /** Σ of one line's parseable split amounts, exact. */
  function lineAllocated(rows: readonly SplitRow[]): Exact {
    let sum: Exact = ZERO;
    for (const row of rows) {
      const parsed = parseAmount(row.amount);
      if (parsed.ok) sum = add(sum, parsed.value);
    }
    return sum;
  }

  const pickedLine = lines[picked] ?? null;
  const pickedAllocated = pickedLine
    ? lineAllocated(splits[picked] ?? [])
    : ZERO;
  const pickedRemainder = pickedLine ? sub(pickedLine.amount, pickedAllocated) : ZERO;

  // ------------------------------------------------------------ editing
  function updateSplit(rowIndex: number, patch: Partial<SplitRow>): void {
    setSplits((prev) =>
      prev.map((rows, i) =>
        i === picked ? rows.map((row, j) => (j === rowIndex ? { ...row, ...patch } : row)) : rows,
      ),
    );
  }

  function addSplit(): void {
    if (!allCentres.length) {
      setError('No cost centre exists yet — create one below, or on the Masters page first.');
      return;
    }
    setError('');
    const seed =
      pickedLine && !isZero(pickedRemainder) && !pickedRemainder.neg
        ? toCanonical(pickedRemainder)
        : '';
    setSplits((prev) =>
      prev.map((rows, i) =>
        i === picked ? [...rows, { centreId: allCentres[0]!.id, amount: seed }] : rows,
      ),
    );
  }

  function removeSplit(rowIndex: number): void {
    setSplits((prev) =>
      prev.map((rows, i) => (i === picked ? rows.filter((_r, j) => j !== rowIndex) : rows)),
    );
  }

  function clearLine(): void {
    setSplits((prev) => prev.map((rows, i) => (i === picked ? [] : rows)));
  }

  async function createCentre(): Promise<void> {
    const name = newName.trim();
    if (!name) {
      setError('The new centre needs a name.');
      return;
    }
    const parent = newParent || centres.find((c) => c.is_root)?.name || 'Primary';
    const category = newCategory || realCategories[0]?.name;
    if (!category) {
      setError('No cost category exists — create one first (every centre answers to a category).');
      return;
    }
    try {
      const created = await api.createCostCentre({ name, parent_name: parent, category_name: category });
      setExtraCentres((prev) => [...prev, created]);
      setNewName('');
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not create the centre');
    }
  }

  // ------------------------------------------------------------ accept
  function accept(): void {
    for (let i = 0; i < lines.length; i++) {
      const rows = splits[i] ?? [];
      if (!rows.length) continue; // explicitly left unallocated
      const line = lines[i]!;
      const allocated = lineAllocated(rows);
      if (isZero(allocated)) continue; // all rows emptyed — unallocated
      if (cmp(allocated, line.amount) !== 0) {
        setError(
          `Line ${i + 1} (${line.ledgerName}): ${toCanonical(allocated)} allocated against ` +
            `${toCanonical(line.amount)} — a line is fully allocated or left unallocated, never partly.`,
        );
        return;
      }
      for (const row of rows) {
        if (row.centreId === null) {
          setError(`Line ${i + 1}: every split needs a centre.`);
          return;
        }
      }
    }
    const payload: CostAllocationIn[][] = splits.map((rows) =>
      rows
        .filter((row) => row.centreId !== null)
        .map((row) => {
          const parsed = parseAmount(row.amount);
          return {
            centre_id: row.centreId!,
            amount: parsed.ok ? toCanonical(parsed.value) : '0',
          };
        })
        .filter((alloc) => alloc.amount !== '0'),
    );
    onApply(payload);
  }

  useCommand(
    { id: 'dialog.cost.accept', title: 'Arm cost allocations', run: accept },
    'Enter',
    (ctx) => ctx.scopeId === 'costDialog',
  );

  // ------------------------------------------------------------ render
  return (
    <div className="overlay" role="presentation">
      <div className="overlay-card overlay-wide" role="dialog" aria-label="Allocate to cost centres">
        <h2>Cost centres</h2>
        <p className="hint">
          Every line is <strong>fully allocated or left unallocated</strong> — the remainder is
          always visible. The engine re-proves each split at posting time (exact decimals).
        </p>

        <table className="alloc-table" aria-label="Draft lines">
          <thead>
            <tr>
              <th>Ledger</th>
              <th>D/C</th>
              <th className="num">Amount</th>
              <th className="num">Allocated</th>
              <th className="num">Remainder</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((line, i) => {
              const allocated = lineAllocated(splits[i] ?? []);
              const remainder = sub(line.amount, allocated);
              const armed = (splits[i] ?? []).length > 0 && !isZero(allocated);
              return (
                <tr
                  key={`${line.ledgerName}:${i}`}
                  className={i === picked ? 'picked' : undefined}
                  onClick={() => setPicked(i)}
                >
                  <td>{line.ledgerName}</td>
                  <td>{line.dc === 'D' ? 'Debit' : 'Credit'}</td>
                  <td className="num">{toCanonical(line.amount)}</td>
                  <td className="num">{toCanonical(allocated)}</td>
                  <td className="num">
                    {armed && !isZero(remainder) ? (
                      <span className="field-error">{toCanonical(remainder)}</span>
                    ) : (
                      toCanonical(remainder)
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>

        <h3>
          Splits — {pickedLine ? pickedLine.ledgerName : '—'}
        </h3>
        <table className="alloc-table" aria-label="Cost centre splits">
          <thead>
            <tr>
              <th>Cost centre</th>
              <th className="num">Amount</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(splits[picked] ?? []).map((row, rowIndex) => (
              <tr key={rowIndex}>
                <td>
                  <select
                    ref={rowIndex === 0 ? firstInputRef : undefined}
                    aria-label="Cost centre"
                    value={row.centreId ?? ''}
                    onChange={(e) =>
                      updateSplit(rowIndex, {
                        centreId: e.target.value === '' ? null : Number(e.target.value),
                      })
                    }
                  >
                    {allCentres.map((centre) => (
                      <option key={centre.id} value={centre.id}>
                        {centreLabel(centre)}
                      </option>
                    ))}
                  </select>
                </td>
                <td className="num">
                  <input
                    aria-label={`Split amount ${rowIndex + 1}`}
                    className="alloc-input"
                    inputMode="decimal"
                    placeholder="0.00"
                    value={row.amount}
                    onChange={(e) => updateSplit(rowIndex, { amount: e.target.value })}
                  />
                </td>
                <td>
                  <button type="button" className="chip" onClick={() => removeSplit(rowIndex)}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
            {(splits[picked] ?? []).length === 0 && (
              <tr>
                <td colSpan={3} className="hint">
                  No splits — this line is left unallocated.
                </td>
              </tr>
            )}
          </tbody>
        </table>

        <p className="actions">
          <button type="button" className="chip" onClick={addSplit}>
            Add split
          </button>
          <button type="button" className="chip" onClick={clearLine}>
            Leave unallocated
          </button>
          <span className="hint" style={{ marginLeft: 'auto' }}>
            {pickedLine
              ? isZero(pickedRemainder)
                ? 'Fully allocated'
                : pickedRemainder.neg
                  ? `Over-allocated by ${toCanonical({ ...pickedRemainder, neg: false })}`
                  : `Remainder ${toCanonical(pickedRemainder)}`
              : ''}
          </span>
        </p>

        <details>
          <summary className="hint">New cost centre…</summary>
          <p className="actions">
            <input
              aria-label="New centre name"
              placeholder="e.g. Mumbai branch"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
            />
            <select
              aria-label="Parent centre"
              value={newParent}
              onChange={(e) => setNewParent(e.target.value)}
            >
              <option value="">under (root)</option>
              {centres.map((centre) => (
                <option key={centre.id} value={centre.name}>
                  {centre.name}
                </option>
              ))}
            </select>
            <select
              aria-label="Category"
              value={newCategory}
              onChange={(e) => setNewCategory(e.target.value)}
            >
              {realCategories.map((category) => (
                <option key={category.id} value={category.name}>
                  {category.name}
                </option>
              ))}
            </select>
            <button type="button" className="chip" onClick={() => void createCentre()}>
              Create
            </button>
          </p>
        </details>

        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}

        <p className="actions">
          <button type="button" className="primary" onClick={accept}>
            Arm allocations <kbd>Enter</kbd>
          </button>
          <button type="button" onClick={onCancel}>
            Cancel <kbd>Esc</kbd>
          </button>
        </p>
      </div>
    </div>
  );
}
