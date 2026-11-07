/**
 * The full keyboard flow, in real Chromium, zero mouse input (ADR-004's
 * promise verified end-to-end): type-ahead ledger picking, the F-key type
 * switches, the F2 date dialog, Ctrl+Enter balance filling, Ctrl+A accept,
 * the Alt+B daybook browse, and Alt+C ledger creation mid-flight — against
 * the live engine and PostgreSQL, through the vite /api proxy exactly like
 * `make dev`.
 *
 * The database is a fresh lf_e2e (scripts/e2e_setup_db.py) whose chart has
 * exactly two ledgers the flow cares about: "Cash" and "E2E Sales".
 */

import { expect, test } from '@playwright/test';

test.describe('the voucher-entry keyboard flow', () => {
  test('keyboard alone posts, browses and creates', async ({ page }) => {
    test.setTimeout(90_000);
    await page.goto('/');

    const cell = (row: number, col: string) => page.locator(`[data-cell="${row}:${col}"]`);
    const notice = page.locator('.banner.ok');

    // Boot: masters load, focus lands in the first particulars cell —
    // no click anywhere in this file, by design.
    await expect(page.getByText('Loading chart of accounts…')).toBeHidden();
    await expect(cell(0, 'ledger')).toBeFocused();

    // F-key type switching: Payment (F5), back to Sales (F8).
    await page.keyboard.press('F5');
    await expect(page.getByRole('tab', { name: /Payment/ })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    await page.keyboard.press('F8');
    await expect(page.getByRole('tab', { name: /Sales/ })).toHaveAttribute(
      'aria-selected',
      'true',
    );

    // F2 date dialog opens; Esc keeps the old date (dialog path exercised).
    await page.keyboard.press('F2');
    await expect(page.getByText('Enter accept · Esc cancel')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByText('Enter accept · Esc cancel')).toBeHidden();

    // Line 1: type-ahead picks "Cash", 100 debit.
    await page.keyboard.type('cash');
    await expect(page.getByRole('combobox', { name: 'Line 1 particulars' })).toHaveValue('cash');
    await page.keyboard.press('Enter'); // accept the highlighted picker match
    await expect(cell(0, 'ledger')).toHaveValue('Cash');
    await expect(cell(0, 'debit')).toBeFocused();
    await page.keyboard.type('100');
    await page.keyboard.press('Enter'); // → credit
    await expect(cell(0, 'credit')).toBeFocused();
    await page.keyboard.press('Enter'); // empty credit → narration → empty, skipped → new row

    // Line 2: "E2E Sales" credited with the balancing amount (Ctrl+Enter).
    await expect(cell(1, 'ledger')).toBeFocused();
    await page.keyboard.type('e2e sales');
    await page.keyboard.press('Enter');
    await expect(cell(1, 'ledger')).toHaveValue('E2E Sales');
    await page.keyboard.press('Enter'); // skip debit → credit
    await expect(cell(1, 'credit')).toBeFocused();
    await page.keyboard.press('Control+Enter'); // fill the balancing 100
    await expect(cell(1, 'credit')).toHaveValue('100');

    // Accept (Ctrl+A): the engine posts it; the banner carries the number.
    await page.keyboard.press('Control+A');
    await expect(notice).toContainText(/Voucher SAL-[0-9-]+ posted/);
    const voucherNo =
      (await notice.textContent())?.match(/Voucher (SAL-[0-9-]+) posted/)?.[1] ?? '';
    expect(voucherNo).not.toBe('');

    // Alt+B: the daybook lists it; the detail pane shows both lines.
    await page.keyboard.press('Alt+B');
    const rows = page.getByRole('grid', { name: 'Posted vouchers' });
    await expect(rows).toBeVisible();
    await expect(rows.getByText(voucherNo)).toBeVisible();
    const detail = page.getByLabel('Selected voucher');
    await expect(detail).toContainText(voucherNo);
    await expect(detail).toContainText('E2E Sales');

    // Esc: back to the (fresh) draft, focus back in the grid.
    await page.keyboard.press('Escape');
    await expect(cell(0, 'ledger')).toBeFocused();

    // Alt+C: a ledger that does not exist is created mid-flight, keyboard only.
    await page.keyboard.type('e2e customer');
    await expect(page.getByText('No ledger matches — Alt+C creates it')).toBeVisible();
    await page.keyboard.press('Alt+C');
    await expect(page.getByText('Enter next field · Enter again create · Esc cancel')).toBeVisible();
    await page.keyboard.press('Enter'); // name (seeded) → group picker
    await page.keyboard.press('Enter'); // accept the highlighted group
    await page.keyboard.press('Enter'); // create
    await expect(notice).toContainText('created under');
    await expect(cell(0, 'ledger')).toHaveValue('e2e customer');
    await expect(cell(0, 'debit')).toBeFocused();
  });
});
