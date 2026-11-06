-- ============================================================================
-- LedgerForge — seed: TallyPrime-style chart of accounts, voucher types,
-- default ledgers and settings. Idempotent (ON CONFLICT DO NOTHING).
--
-- The 28 predefined groups mirror TallyPrime (15 primary + 13 sub-groups;
-- see docs/analysis.md §2.2). `nature` decides Balance Sheet vs P&L
-- placement; `affects_gross` drives the trading-account (gross profit) split.
-- Placement notes: the root "Primary" is liability-natured so that ledgers
-- attached directly to it (like "Profit & Loss A/c") surface on the
-- liabilities side, matching TallyPrime's default display.
-- ============================================================================

-- Root -------------------------------------------------------------------
INSERT INTO groups (name, parent_id, nature, affects_gross, is_root)
SELECT 'Primary', NULL, 'liability', FALSE, TRUE
WHERE NOT EXISTS (SELECT 1 FROM groups WHERE is_root);

-- 15 primary groups --------------------------------------------------------
INSERT INTO groups (name, parent_id, nature, affects_gross)
SELECT v.name, p.id, v.nature, v.affects_gross
FROM (VALUES
    ('Capital Account',        'equity',     FALSE),
    ('Loans (Liability)',      'liability',  FALSE),
    ('Current Liabilities',    'liability',  FALSE),
    ('Fixed Assets',           'asset',      FALSE),
    ('Investments',            'asset',      FALSE),
    ('Current Assets',         'asset',      FALSE),
    ('Branch / Divisions',     'liability',  FALSE),
    ('Misc. Expenses (Asset)', 'asset',      FALSE),
    ('Suspense A/c',           'liability',  FALSE),
    ('Sales Accounts',         'income',     TRUE),
    ('Purchase Accounts',      'expense',    TRUE),
    ('Direct Incomes',         'income',     TRUE),
    ('Indirect Incomes',       'income',     FALSE),
    ('Direct Expenses',        'expense',    TRUE),
    ('Indirect Expenses',      'expense',    FALSE)
) AS v(name, nature, affects_gross)
CROSS JOIN (SELECT id FROM groups WHERE name = 'Primary') p
ON CONFLICT (name) DO NOTHING;

-- 13 predefined sub-groups -------------------------------------------------
INSERT INTO groups (name, parent_id, nature, affects_gross)
SELECT v.name, p.id, v.nature, v.affects_gross
FROM (VALUES
    ('Reserves & Surplus',       'Capital Account',     'equity',     FALSE),
    ('Bank OD A/c',              'Loans (Liability)',   'liability',  FALSE),
    ('Secured Loans',            'Loans (Liability)',   'liability',  FALSE),
    ('Unsecured Loans',          'Loans (Liability)',   'liability',  FALSE),
    ('Duties & Taxes',           'Current Liabilities', 'liability',  FALSE),
    ('Provisions',               'Current Liabilities', 'liability',  FALSE),
    ('Sundry Creditors',         'Current Liabilities', 'liability',  FALSE),
    ('Bank Accounts',            'Current Assets',      'asset',      FALSE),
    ('Cash-in-Hand',             'Current Assets',      'asset',      FALSE),
    ('Deposits (Asset)',         'Current Assets',      'asset',      FALSE),
    ('Loans & Advances (Asset)', 'Current Assets',      'asset',      FALSE),
    ('Stock-in-Hand',            'Current Assets',      'asset',      FALSE),
    ('Sundry Debtors',           'Current Assets',      'asset',      FALSE)
) AS v(name, parent, nature, affects_gross)
JOIN groups p ON p.name = v.parent
ON CONFLICT (name) DO NOTHING;

-- Default ledgers (TallyPrime creates these two automatically) --------------
INSERT INTO ledgers (name, group_id, is_system)
SELECT 'Cash', g.id, TRUE FROM groups g WHERE g.name = 'Cash-in-Hand'
ON CONFLICT (name) DO NOTHING;

INSERT INTO ledgers (name, group_id, is_system)
SELECT 'Profit & Loss A/c', g.id, TRUE FROM groups g WHERE g.name = 'Primary'
ON CONFLICT (name) DO NOTHING;

-- FX gain/loss system ledgers (issue #4, ADR-007): realized differences on
-- settlement/receipt; unrealized on period-end revaluation of open positions.
INSERT INTO ledgers (name, group_id, is_system)
SELECT v.name, g.id, TRUE
FROM (VALUES
    ('Realized Exchange Gain',   'Indirect Incomes'),
    ('Realized Exchange Loss',   'Indirect Expenses'),
    ('Unrealized Exchange Gain', 'Indirect Incomes'),
    ('Unrealized Exchange Loss', 'Indirect Expenses')
) AS v(name, parent)
JOIN groups g ON g.name = v.parent
ON CONFLICT (name) DO NOTHING;

-- Currencies: the base currency ships as a system row; others via POST
-- /api/v1/currencies. minor_unit drives display rounding (JPY 0, BHD 3).
INSERT INTO currencies (code, name, symbol, minor_unit, is_system) VALUES
    ('INR', 'Indian Rupee', '₹', 2, TRUE)
ON CONFLICT (code) DO NOTHING;

-- Voucher types (TallyPrime's set) ------------------------------------------
INSERT INTO voucher_types (code, name, number_prefix, restart_yearly, is_system) VALUES
    ('OPEN', 'Opening Journal', 'OPN', TRUE, TRUE),
    ('FYC',  'Financial Year Close', 'FYC', TRUE, TRUE),
    ('CN',   'Contra',          'CN',  TRUE, FALSE),
    ('PYT',  'Payment',         'PYT', TRUE, FALSE),
    ('RCT',  'Receipt',         'RCT', TRUE, FALSE),
    ('JV',   'Journal',         'JV',  TRUE, FALSE),
    ('SAL',  'Sales',           'SAL', TRUE, FALSE),
    ('PUR',  'Purchase',        'PUR', TRUE, FALSE),
    ('CRN',  'Credit Note',     'CRN', TRUE, FALSE),
    ('DBN',  'Debit Note',      'DBN', TRUE, FALSE)
ON CONFLICT (code) DO NOTHING;

-- Settings -------------------------------------------------------------------
INSERT INTO settings (key, value) VALUES
    ('company_name',    'My Company'),
    ('base_currency',   'INR'),
    ('fy_start_month',  '4')
ON CONFLICT (key) DO NOTHING;
