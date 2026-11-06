/**
 * Alt+C — create the missing ledger without leaving the voucher (the Tally
 * contract: masters are created mid-flight). Name first, group second; the
 * group field reuses the same type-ahead listbox.
 */

import { useEffect, useRef, useState } from 'react';
import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';
import type { GroupOut } from '../api/types';
import { rankEntries } from '../voucher/pickerFilter';
import type { CreateLedgerInput } from '../api/client';
import { LedgerPicker } from './LedgerPicker';

interface LedgerCreateDialogProps {
  seedName: string;
  defaultGroupName: string;
  groups: readonly GroupOut[];
  onSubmit: (input: CreateLedgerInput) => Promise<void>;
  onCancel: () => void;
}

const GROUP_LISTBOX_ID = 'group-picker';

export function LedgerCreateDialog({
  seedName,
  defaultGroupName,
  groups,
  onSubmit,
  onCancel,
}: LedgerCreateDialogProps) {
  const [name, setName] = useState(seedName);
  const [groupQuery, setGroupQuery] = useState(defaultGroupName);
  const [highlight, setHighlight] = useState(0);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);
  const groupRef = useRef<HTMLInputElement>(null);

  const selectable = groups.filter((g) => !g.is_root);
  const entries = rankEntries(
    selectable.map((g) => ({ id: g.id, name: g.name, context: g.nature })),
    groupQuery,
  );

  useFocusScope('ledgerCreateDialog', () => {
    onCancel();
    return true;
  });

  useEffect(() => {
    nameRef.current?.focus();
    nameRef.current?.select();
  }, []);

  useCommand(
    {
      id: 'dialog.create.nameNext',
      title: 'Ledger name done — go to group',
      run: () => {
        if (document.activeElement === nameRef.current) {
          groupRef.current?.focus();
          setPickerOpen(true);
        } else {
          void submit();
        }
      },
    },
    'Enter',
    (ctx) => ctx.scopeId === 'ledgerCreateDialog',
  );

  useCommand(
    {
      id: 'selection.groupUp',
      title: 'Group list up',
      run: () => setHighlight((h) => (entries.length ? (h - 1 + entries.length) % entries.length : 0)),
    },
    'ArrowUp',
    (ctx) => ctx.scopeId === 'groupPicker',
  );

  useCommand(
    {
      id: 'selection.groupDown',
      title: 'Group list down',
      run: () => setHighlight((h) => (entries.length ? (h + 1) % entries.length : 0)),
    },
    'ArrowDown',
    (ctx) => ctx.scopeId === 'groupPicker',
  );

  useCommand(
    {
      id: 'selection.groupAccept',
      title: 'Accept highlighted group',
      run: () => {
        const entry = entries[highlight];
        if (entry) setGroupQuery(entry.name);
        setPickerOpen(false);
      },
    },
    'Enter',
    (ctx) => ctx.scopeId === 'groupPicker',
  );

  async function submit() {
    if (submitting) return;
    const trimmed = name.trim();
    if (trimmed === '') {
      setError('Ledger name is required');
      nameRef.current?.focus();
      return;
    }
    const group = selectable.find((g) => g.name === groupQuery.trim());
    if (!group) {
      setError(`Pick a group from the list (e.g. "${defaultGroupName}")`);
      setPickerOpen(true);
      groupRef.current?.focus();
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      await onSubmit({ name: trimmed, group_name: group.name });
      // parent closes the dialog on success
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not create the ledger');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="overlay" role="presentation">
      <div className="overlay-card" role="dialog" aria-label="Create ledger">
        <h2>Create ledger</h2>
        <label className="field">
          <span>Name</span>
          <input
            ref={nameRef}
            value={name}
            onChange={(e) => setName(e.target.value)}
            aria-label="Ledger name"
          />
        </label>
        <label className="field">
          <span>Under group</span>
          <span className="field-picker">
            <input
              ref={groupRef}
              value={groupQuery}
              role="combobox"
              aria-expanded={pickerOpen}
              aria-controls={GROUP_LISTBOX_ID}
              aria-autocomplete="list"
              onChange={(e) => {
                setGroupQuery(e.target.value);
                setHighlight(0);
                setPickerOpen(true);
              }}
              onFocus={() => {
                setPickerOpen(true);
                // select the default so typing replaces it outright
                groupRef.current?.select();
              }}
              aria-label="Parent group"
            />
            {pickerOpen && (
              <LedgerPicker
                scopeId="groupPicker"
                listboxId={GROUP_LISTBOX_ID}
                entries={entries}
                highlight={highlight}
                onPick={(i) => {
                  const entry = entries[i];
                  if (entry) setGroupQuery(entry.name);
                  setPickerOpen(false);
                  groupRef.current?.focus();
                }}
                onClose={() => setPickerOpen(false)}
                emptyText="No group matches — keep typing"
              />
            )}
          </span>
        </label>
        {error && <p className="field-error" role="alert">{error}</p>}
        <div className="overlay-actions">
          <button type="button" disabled={submitting} onClick={() => void submit()}>
            {submitting ? 'Creating…' : 'Create'}
          </button>
          <button type="button" onClick={onCancel}>Cancel</button>
        </div>
        <p className="overlay-foot">
          Enter next field · Enter again create · Esc cancel — focus returns to the grid
        </p>
      </div>
    </div>
  );
}
