/**
 * The inventory screen (ADR-027, Phase 6): stock masters, the depth
 * positions, and the reports. `Alt+N` opens it; `I`/`S`/`B` switch the
 * tabs (Items / Stock / BOMs); on the Stock tab `1`..`5` walk the views
 * (positions, expiry ageing, re-order shortfall, godown-wise,
 * batch-wise); `N` on Items focuses the new-item form; `Esc` is back to
 * entry. Every number is the engine's string — the client never sums
 * (Law 3); the tables render exactly the folds the PDFs print.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';
import { api } from '../api/client';
import type {
  StockBom,
  StockExpiryReport,
  StockGodownReport,
  StockItemMaster,
  StockPositionReport,
  StockShortfallReport,
} from '../api/types';
import type { Notice } from './Banner';

type LoadState = 'loading' | 'ready' | 'error';
type Tab = 'items' | 'stock' | 'boms';
type StockView = 'position' | 'expiry' | 'shortfall' | 'godown' | 'batch';

interface InventoryScreenProps {
  onExit: () => void;
  /** Live interaction flags, read by keymap `when` predicates. */
  ctxRef: React.MutableRefObject<Record<string, unknown>>;
}

const TAB_LABEL: Record<Tab, string> = { items: 'Items', stock: 'Stock', boms: 'BOMs' };
const VIEW_LABEL: Record<StockView, string> = {
  position: 'Positions',
  expiry: 'Expiry ageing',
  shortfall: 'Re-order shortfall',
  godown: 'Godown-wise',
  batch: 'Batch-wise',
};
const VIEWS: StockView[] = ['position', 'expiry', 'shortfall', 'godown', 'batch'];

/** A numeric string without its trailing zeros (NUMERIC scale is storage). */
function plain(value: string | null | undefined): string {
  if (value == null || value === '') return '—';
  const parsed = Number(value);
  return Number.isFinite(parsed) ? String(parsed) : value;
}

/** 'Main location: 14; Annex: 6' from the fold's split. */
function splitOf(split: Record<string, string>): string {
  const entries = Object.entries(split);
  return entries.length
    ? entries.map(([g, q]) => `${g}: ${plain(q)}`).join('; ')
    : '—';
}

export function InventoryScreen({ onExit, ctxRef }: InventoryScreenProps) {
  const [tab, setTab] = useState<Tab>('items');
  const [view, setView] = useState<StockView>('position');
  const [items, setItems] = useState<StockItemMaster[] | null>(null);
  const [boms, setBoms] = useState<StockBom[] | null>(null);
  const [positions, setPositions] = useState<StockPositionReport | null>(null);
  const [expiry, setExpiry] = useState<StockExpiryReport | null>(null);
  const [shortfall, setShortfall] = useState<StockShortfallReport | null>(null);
  const [godown, setGodown] = useState<StockGodownReport | null>(null);
  const [batch, setBatch] = useState<StockPositionReport | null>(null);
  const [loadState, setLoadState] = useState<LoadState>('loading');
  const [notice, setNotice] = useState<Notice | null>(null);
  const [formFocus, setFormFocus] = useState(false);
  const nameRef = useRef<HTMLInputElement | null>(null);
  const [newItem, setNewItem] = useState({ name: '', unit: '', reorder_level: '', reorder_quantity: '' });

  // this screen is the top focus scope: Esc pops it (the universal back)
  useFocusScope('inventoryScreen', () => {
    onExit();
    return true;
  });

  ctxRef.current = { inventoryOpen: true, formFocus };
  useEffect(() => () => {
    ctxRef.current = {};
  }, [ctxRef]);

  const loadItems = () => {
    setLoadState('loading');
    api
      .inventoryItems()
      .then((rows) => {
        setItems(rows);
        setLoadState('ready');
      })
      .catch((e: unknown) => {
        setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'Could not load the items' });
        setLoadState('error');
      });
  };

  const loadBoms = () => {
    api
      .inventoryBoms()
      .then(setBoms)
      .catch((e: unknown) => {
        setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'Could not load the BOMs' });
      });
  };

  const loadView = (which: StockView) => {
    setLoadState('loading');
    const done = () => setLoadState('ready');
    const failed = (e: unknown) => {
      setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'Could not load the report' });
      setLoadState('error');
    };
    if (which === 'position') {
      api.stockPack<StockPositionReport>('stock-position').then(setPositions).then(done).catch(failed);
    } else if (which === 'expiry') {
      api.stockPack<StockExpiryReport>('stock-expiry').then(setExpiry).then(done).catch(failed);
    } else if (which === 'shortfall') {
      api.stockPack<StockShortfallReport>('stock-shortfall').then(setShortfall).then(done).catch(failed);
    } else if (which === 'godown') {
      api.stockPack<StockGodownReport>('stock-godown').then(setGodown).then(done).catch(failed);
    } else {
      api.stockPack<StockPositionReport>('stock-batch').then(setBatch).then(done).catch(failed);
    }
  };

  useEffect(loadItems, []);
  useEffect(() => {
    loadBoms();
  }, []);
  useEffect(() => {
    if (tab === 'stock') loadView(view);
  }, [tab, view]);

  const inScreen = () => ctxRef.current.inventoryOpen === true && !formFocus;

  useCommand(
    { id: 'inventory.tabItems', title: 'Inventory: items tab', run: () => setTab('items') },
    'I',
    inScreen as () => boolean,
  );
  useCommand(
    { id: 'inventory.tabStock', title: 'Inventory: stock tab', run: () => setTab('stock') },
    'S',
    inScreen as () => boolean,
  );
  useCommand(
    { id: 'inventory.tabBoms', title: 'Inventory: BOMs tab', run: () => setTab('boms') },
    'B',
    inScreen as () => boolean,
  );
  useCommand(
    {
      id: 'inventory.newItem',
      title: 'Inventory: new stock item',
      run: () => {
        setTab('items');
        nameRef.current?.focus();
      },
    },
    'N',
    inScreen as () => boolean,
  );
  VIEWS.forEach((which, i) => {
    useCommand(
      {
        id: `inventory.view${i + 1}`,
        title: `Inventory: ${VIEW_LABEL[which]}`,
        run: () => {
          setTab('stock');
          setView(which);
        },
      },
      String(i + 1),
      inScreen as () => boolean,
    );
  });

  const createItem = (event: React.FormEvent) => {
    event.preventDefault();
    if (!newItem.name.trim()) {
      setNotice({ kind: 'err', text: 'A stock item needs a name' });
      return;
    }
    const payload: Partial<StockItemMaster> = { name: newItem.name.trim() };
    if (newItem.unit.trim()) payload.unit = newItem.unit.trim();
    if (newItem.reorder_level.trim()) payload.reorder_level = newItem.reorder_level.trim();
    if (newItem.reorder_quantity.trim()) payload.reorder_quantity = newItem.reorder_quantity.trim();
    api
      .inventoryCreateItem(payload)
      .then(() => {
        setNewItem({ name: '', unit: '', reorder_level: '', reorder_quantity: '' });
        setNotice({ kind: 'ok', text: `Stock item ${payload.name} created` });
        loadItems();
        loadBoms();
      })
      .catch((e: unknown) => {
        setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'Could not create the item' });
      });
  };

  const body = useMemo(() => {
    if (tab === 'items') {
      return (
        <table className="grid" data-testid="inventory-items">
          <thead>
            <tr>
              <th>Item</th>
              <th>Unit</th>
              <th>Batches</th>
              <th>Serials</th>
              <th className="num">Re-order level</th>
              <th className="num">Re-order qty</th>
              <th className="num">Prices</th>
            </tr>
          </thead>
          <tbody>
            {(items ?? []).map((it) => (
              <tr key={it.id}>
                <td>{it.name}</td>
                <td>{it.unit ?? '—'}</td>
                <td>{it.track_batches ? '✓' : '—'}</td>
                <td>{it.track_serials ? '✓' : '—'}</td>
                <td className="num">{plain(it.reorder_level)}</td>
                <td className="num">{plain(it.reorder_quantity)}</td>
                <td className="num">{it.price_count}</td>
              </tr>
            ))}
            {!items?.length && (
              <tr>
                <td colSpan={7} className="empty">
                  No stock items yet — depth is opted into, never demanded
                </td>
              </tr>
            )}
          </tbody>
        </table>
      );
    }
    if (tab === 'boms') {
      return (
        <table className="grid" data-testid="inventory-boms">
          <thead>
            <tr>
              <th>BOM</th>
              <th>Output</th>
              <th className="num">Per run</th>
              <th>Components</th>
            </tr>
          </thead>
          <tbody>
            {(boms ?? []).map((bom) => (
              <tr key={bom.id}>
                <td>{bom.name}</td>
                <td>{bom.output}</td>
                <td className="num">{plain(bom.output_qty)}</td>
                <td>
                  {bom.components.map((c) => `${c.component} × ${plain(c.qty_per_output)}`).join('; ')}
                </td>
              </tr>
            ))}
            {!boms?.length && (
              <tr>
                <td colSpan={4} className="empty">
                  No BOMs yet — the manufacturing journal validates against them by name
                </td>
              </tr>
            )}
          </tbody>
        </table>
      );
    }
    if (view === 'position' && positions) {
      return (
        <table className="grid" data-testid="inventory-positions">
          <thead>
            <tr>
              <th>Item</th>
              <th className="num">Qty</th>
              <th className="num">Value</th>
              <th className="num">Avg cost</th>
              <th>Godowns</th>
              <th>Batches</th>
              <th className="num">Serials</th>
            </tr>
          </thead>
          <tbody>
            {positions.items.map((row) => (
              <tr key={row.item}>
                <td>{row.item}</td>
                <td className="num">{plain(row.qty)}</td>
                <td className="num">{row.value}</td>
                <td className="num">{row.avg_cost ?? '—'}</td>
                <td>{splitOf(row.godowns)}</td>
                <td>{splitOf(Object.fromEntries(row.batches.map((b) => [b.batch, b.qty])))}</td>
                <td className="num">{row.serial_count || '—'}</td>
              </tr>
            ))}
            {!positions.items.length && (
              <tr>
                <td colSpan={7} className="empty">
                  No stock documents posted yet
                </td>
              </tr>
            )}
          </tbody>
          <tfoot>
            <tr>
              <td>Total</td>
              <td />
              <td className="num">{positions.total_value}</td>
              <td colSpan={4} />
            </tr>
          </tfoot>
        </table>
      );
    }
    if (view === 'expiry' && expiry) {
      return (
        <table className="grid" data-testid="inventory-expiry">
          <thead>
            <tr>
              <th>Item</th>
              <th>Batch</th>
              <th>Expiry</th>
              <th className="num">Days</th>
              <th>Bucket</th>
              <th className="num">Qty</th>
              <th className="num">Value</th>
            </tr>
          </thead>
          <tbody>
            {expiry.rows.map((row) => (
              <tr key={`${row.item}-${row.batch}`}>
                <td>{row.item}</td>
                <td>{row.batch}</td>
                <td>{row.expiry}</td>
                <td className="num">{row.days_left}</td>
                <td>{row.bucket}</td>
                <td className="num">{plain(row.qty)}</td>
                <td className="num">{row.value}</td>
              </tr>
            ))}
            {!expiry.rows.length && (
              <tr>
                <td colSpan={7} className="empty">
                  No dated batches — nothing expires
                </td>
              </tr>
            )}
          </tbody>
        </table>
      );
    }
    if (view === 'shortfall' && shortfall) {
      return (
        <table className="grid" data-testid="inventory-shortfall">
          <thead>
            <tr>
              <th>Item</th>
              <th>Unit</th>
              <th className="num">On hand</th>
              <th className="num">Level</th>
              <th className="num">Below by</th>
              <th className="num">Order qty</th>
            </tr>
          </thead>
          <tbody>
            {shortfall.rows.map((row) => (
              <tr key={row.item}>
                <td>{row.item}</td>
                <td>{row.unit ?? '—'}</td>
                <td className="num">{plain(row.on_hand)}</td>
                <td className="num">{plain(row.reorder_level)}</td>
                <td className="num">{plain(row.below_by)}</td>
                <td className="num">{plain(row.suggested_order)}</td>
              </tr>
            ))}
            {!shortfall.rows.length && (
              <tr>
                <td colSpan={6} className="empty">
                  Nothing below its re-order level
                </td>
              </tr>
            )}
          </tbody>
        </table>
      );
    }
    if (view === 'godown' && godown) {
      return (
        <table className="grid" data-testid="inventory-godown">
          <thead>
            <tr>
              <th>Godown</th>
              <th>Item</th>
              <th className="num">Qty</th>
              <th className="num">Value</th>
              <th className="num">Avg cost</th>
            </tr>
          </thead>
          <tbody>
            {godown.rows.map((row) => (
              <tr key={`${row.godown}-${row.item}`}>
                <td>{row.godown}</td>
                <td>{row.item}</td>
                <td className="num">{plain(row.qty)}</td>
                <td className="num">{row.value}</td>
                <td className="num">{row.avg_cost ?? '—'}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            {Object.entries(godown.godown_totals).map(([g, total]) => (
              <tr key={g}>
                <td>{g}</td>
                <td>Total</td>
                <td />
                <td className="num">{total}</td>
                <td />
              </tr>
            ))}
          </tfoot>
        </table>
      );
    }
    if (view === 'batch' && batch) {
      return (
        <table className="grid" data-testid="inventory-batch">
          <thead>
            <tr>
              <th>Item</th>
              <th>Batch</th>
              <th className="num">Qty</th>
              <th className="num">Value</th>
              <th>Godowns</th>
            </tr>
          </thead>
          <tbody>
            {batch.items.flatMap((item) =>
              item.batches.map((b) => (
                <tr key={`${item.item}-${b.batch}`}>
                  <td>{item.item}</td>
                  <td>{b.batch}</td>
                  <td className="num">{plain(b.qty)}</td>
                  <td className="num">{item.value}</td>
                  <td>{splitOf(b.godowns)}</td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      );
    }
    return <p className="empty">{loadState === 'loading' ? 'Loading…' : 'Nothing to show'}</p>;
  }, [tab, view, items, boms, positions, expiry, shortfall, godown, batch, loadState]);

  return (
    <div className="screen">
      <header className="bar">
        <span className="brand">LedgerForge</span>
        <span className="mode">Inventory</span>
        <span className="tabs" role="tablist">
          {(['items', 'stock', 'boms'] as Tab[]).map((key) => (
            <button
              key={key}
              role="tab"
              aria-selected={tab === key}
              className={tab === key ? 'tab on' : 'tab'}
              onClick={() => setTab(key)}
            >
              {TAB_LABEL[key]}
            </button>
          ))}
        </span>
        {tab === 'stock' && (
          <span className="tabs" role="tablist" data-testid="stock-views">
            {VIEWS.map((which) => (
              <button
                key={which}
                role="tab"
                aria-selected={view === which}
                className={view === which ? 'tab on' : 'tab'}
                onClick={() => setView(which)}
              >
                {VIEW_LABEL[which]}
              </button>
            ))}
          </span>
        )}
        <button className="tab" onClick={onExit}>
          Back (Esc)
        </button>
      </header>

      {notice && (
        <div className={`banner ${notice.kind}`} role="status">
          {notice.text}
        </div>
      )}

      {tab === 'items' && (
        <form className="panel" onSubmit={createItem} data-testid="new-item-form">
          <input
            ref={nameRef}
            placeholder="New stock item (name)"
            value={newItem.name}
            onFocus={() => setFormFocus(true)}
            onBlur={() => setFormFocus(false)}
            onChange={(e) => setNewItem({ ...newItem, name: e.target.value })}
          />
          <input
            placeholder="Unit (of measure)"
            value={newItem.unit}
            onFocus={() => setFormFocus(true)}
            onBlur={() => setFormFocus(false)}
            onChange={(e) => setNewItem({ ...newItem, unit: e.target.value })}
          />
          <input
            placeholder="Re-order level"
            value={newItem.reorder_level}
            onFocus={() => setFormFocus(true)}
            onBlur={() => setFormFocus(false)}
            onChange={(e) => setNewItem({ ...newItem, reorder_level: e.target.value })}
          />
          <input
            placeholder="Re-order quantity"
            value={newItem.reorder_quantity}
            onFocus={() => setFormFocus(true)}
            onBlur={() => setFormFocus(false)}
            onChange={(e) => setNewItem({ ...newItem, reorder_quantity: e.target.value })}
          />
          <button type="submit">Create</button>
        </form>
      )}

      <div className="sheet">{body}</div>
    </div>
  );
}
