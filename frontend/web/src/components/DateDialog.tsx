/** F2 — change the voucher date. Enter accepts, Esc keeps the old date. */

import { useEffect, useRef, useState } from 'react';
import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';
import { isValidIsoDate } from '../voucher/model';

interface DateDialogProps {
  date: string;
  onAccept: (date: string) => void;
  onCancel: () => void;
}

export function DateDialog({ date, onAccept, onCancel }: DateDialogProps) {
  const [text, setText] = useState(date);
  const [error, setError] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  useFocusScope('dateDialog', () => {
    onCancel();
    return true;
  });

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  useCommand(
    {
      id: 'dialog.date.accept',
      title: 'Accept voucher date',
      run: () => {
        const value = text.trim();
        if (!isValidIsoDate(value)) {
          setError('Enter a valid calendar date as YYYY-MM-DD');
          return;
        }
        onAccept(value);
      },
    },
    'Enter',
    (ctx) => ctx.scopeId === 'dateDialog',
  );

  return (
    <div className="overlay" role="presentation">
      <div className="overlay-card" role="dialog" aria-label="Change voucher date">
        <h2>Voucher date</h2>
        <input
          ref={inputRef}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setError('');
          }}
          aria-label="Voucher date (YYYY-MM-DD)"
          placeholder="YYYY-MM-DD"
          aria-invalid={error !== ''}
        />
        {error && <p className="field-error" role="alert">{error}</p>}
        <p className="overlay-foot">Enter accept · Esc cancel</p>
      </div>
    </div>
  );
}
