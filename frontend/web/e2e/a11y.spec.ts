/**
 * The accessibility audit of the web screen (ADR-032): axe-core over the
 * principal states of the voucher-entry app, in the pipeline, against the
 * live engine and PostgreSQL — the same e2e stack as the keyboard flow.
 *
 * The bar: ZERO serious-or-critical violations (impact: 'serious',
 * 'critical') on WCAG 2.x A/AA rules. Moderate/minor findings are not
 * silently ignored — the spec prints them into the run log so the triage
 * backlog ADR-032 promises stays honest.
 */

import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

test.describe('the accessibility audit (ADR-032)', () => {
  test('the voucher-entry screen, day book and dialogs carry no serious violations', async ({
    page,
  }) => {
    test.setTimeout(90_000);
    await page.goto('/');

    const audit = async (state: string) => {
      const results = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
        .analyze();
      const serious = results.violations.filter((v) =>
        ['serious', 'critical'].includes(v.impact ?? ''),
      );
      const lesser = results.violations.filter(
        (v) => !['serious', 'critical'].includes(v.impact ?? ''),
      );
      // the honest log: what triage owes, printed whether or not it fails
      for (const v of lesser) {
        console.log(
          `[a11y triage] ${state}: ${v.impact} — ${v.help} (${v.nodes.length} nodes) ${v.helpUrl}`,
        );
      }
      for (const v of serious) {
        for (const n of v.nodes) {
          console.log(`[a11y BLOCKER] ${state}: ${v.id} — ${JSON.stringify(n.target)}`);
        }
      }
      expect(
        serious.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`),
        `${state} must carry no serious/critical violations`,
      ).toEqual([]);
    };

    // 1 — the entry grid, freshly loaded (masters fetched, focus in line 1)
    await expect(page.getByText('Loading chart of accounts…')).toBeHidden();
    await expect(page.locator('[data-cell="0:ledger"]')).toBeFocused();
    await audit('the entry grid');

    // 2 — the day book, browsed (Alt+B; Esc is back). The fresh e2e book
    // carries no vouchers yet — the day book's empty state is a principal
    // surface too, and it is what the audit sees.
    await page.keyboard.press('Alt+B');
    await expect(page.getByRole('button', { name: /Voucher entry/ })).toBeVisible();
    await audit('the day book');
    await page.keyboard.press('Escape');

    // 3 — the command palette open (Ctrl+K)
    await expect(page.locator('[data-cell="0:ledger"]')).toBeFocused();
    await page.keyboard.press('Control+KeyK');
    await expect(page.getByRole('dialog', { name: 'Command palette' })).toBeVisible();
    await audit('the command palette');
    await page.keyboard.press('Escape');
  });
});
