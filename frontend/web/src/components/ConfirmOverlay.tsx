/** Two-key confirmation for destructive shortcuts (currently Alt+N on a
 *  dirty draft): Enter confirms, Esc keeps the draft. */

import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';

interface ConfirmOverlayProps {
  message: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmOverlay({ message, confirmLabel, onConfirm, onCancel }: ConfirmOverlayProps) {
  useFocusScope('confirmNew', () => {
    onCancel();
    return true;
  });

  useCommand(
    { id: 'confirm.accept', title: confirmLabel, run: onConfirm },
    'Enter',
    (ctx) => ctx.scopeId === 'confirmNew',
  );

  return (
    <div className="overlay" role="presentation">
      <div className="overlay-card confirm" role="alertdialog" aria-label={message}>
        <p>{message}</p>
        <div className="overlay-actions">
          <button type="button" autoFocus onClick={onConfirm}>{confirmLabel}</button>
          <button type="button" onClick={onCancel}>Keep editing</button>
        </div>
        <p className="overlay-foot">Enter {confirmLabel.toLowerCase()} · Esc keep editing</p>
      </div>
    </div>
  );
}
