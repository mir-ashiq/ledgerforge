/**
 * Payslip-screen logic (ADR-013): the posted voucher list is the payroll
 * run index — a run is a voucher whose `meta.payroll.mode === 'run'`.
 * Amounts stay strings end-to-end (Law 3); this module never computes.
 */

import type { PayrollComputedEmployee, PayrollRunDoc, VoucherOut } from '../api/types';

/** The voucher's payroll run document, or null when it is not a run. */
export function payrollRunOf(voucher: VoucherOut): PayrollRunDoc | null {
  const meta = voucher.meta;
  if (!meta || typeof meta !== 'object') return null;
  const payroll = (meta as Record<string, unknown>).payroll;
  if (!payroll || typeof payroll !== 'object') return null;
  const doc = payroll as Record<string, unknown>;
  if (doc.mode !== 'run') return null; // payouts print as voucher slips
  if (!Array.isArray(doc.employees)) return null;
  return doc as unknown as PayrollRunDoc;
}

/** The run's work month — the computed block is canonical (ADR-012). */
export function runPeriod(doc: PayrollRunDoc): string {
  return doc.computed?.period ?? doc.period;
}

/** The employee names covered by a run, in document order. */
export function runEmployees(doc: PayrollRunDoc): string[] {
  return doc.employees.map((e) => e.employee).filter((name) => name.trim() !== '');
}

/** The run's computed figures for one employee (gross/withheld/net, YTD). */
export function runComputedEmployee(
  doc: PayrollRunDoc,
  employee: string,
): PayrollComputedEmployee | null {
  return doc.computed?.employees.find((e) => e.employee === employee) ?? null;
}

/** Unique employee names across runs, first-seen order. */
export function employeesOfRuns(runs: readonly VoucherOut[]): string[] {
  const seen = new Set<string>();
  for (const run of runs) {
    const doc = payrollRunOf(run);
    if (!doc) continue;
    for (const name of runEmployees(doc)) seen.add(name);
  }
  return [...seen];
}

/** The periods one employee has a run for, newest first. */
export function periodsOfEmployee(runs: readonly VoucherOut[], employee: string): string[] {
  const periods = new Set<string>();
  for (const run of runs) {
    const doc = payrollRunOf(run);
    if (!doc) continue;
    if (!runEmployees(doc).includes(employee)) continue;
    periods.add(runPeriod(doc));
  }
  return [...periods].sort((a, b) => (a < b ? 1 : a > b ? -1 : 0));
}

/** The voucher of `employee`'s run in `period`, or null. */
export function findRun(
  runs: readonly VoucherOut[],
  employee: string,
  period: string,
): VoucherOut | null {
  for (const run of runs) {
    const doc = payrollRunOf(run);
    if (!doc) continue;
    if (runPeriod(doc) === period && runEmployees(doc).includes(employee)) return run;
  }
  return null;
}
