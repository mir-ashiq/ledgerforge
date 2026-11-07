/** Two-key confirmation for destructive shortcuts (Alt+N on a dirty draft,
 * the daybook's reversal): Enter confirms, Esc keeps the status quo. */

import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';

interface ConfirmOverlayProps {
  message: string;
  confirmLabel: string;
  /** The cancel button's label — contexts differ ("Keep the draft",
   *  "Keep the voucher"); defaults to the original wording. */
  cancelLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmOverlay({
  message,
  confirmLabel,
  cancelLabel = 'Keep editing',
  onConfirm,
  onCancel,
}: ConfirmOverlayProps) {
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
          <button type="button" onClick={onCancel}>{cancelLabel}</button>
        </div>
        <p className="overlay-foot">Enter {confirmLabel.toLowerCase()} · Esc {cancelLabel.toLowerCase()}</p>
      </div>
    </div>
  );
}
