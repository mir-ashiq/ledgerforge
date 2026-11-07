/**
 * The plugins screen (ADR-016): the operator's window onto the trust
 * model. `Alt+X` opens it (X for extensions, the VS Code mnemonic); it
 * lists every loaded pack with its channel (first-party/pip vs drop-in)
 * and its trust basis (trusted / signed / acknowledged), and — just as
 * important — everything the boot gate refused and why. A refused plugin
 * someone noticed is the model working; one nobody noticed is not.
 * Read-only: installing and signing are CLI acts (`ledgerforge plugins …`).
 */

import { useEffect, useState } from 'react';
import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';
import { api } from '../api/client';
import type { PluginOut, PluginRefusalOut } from '../api/types';

interface PluginsScreenProps {
  onExit: () => void;
  /** Live interaction flags, read by keymap `when` predicates. */
  ctxRef: React.MutableRefObject<Record<string, unknown>>;
}

const SOURCE_LABEL: Record<PluginOut['source'], string> = {
  'entry-point': 'built-in / pip',
  'drop-in': 'drop-in',
};

const TRUST_LABEL: Record<PluginOut['trust'], string> = {
  trusted: 'trusted',
  signed: 'signed',
  acknowledged: 'acknowledged',
};

const TRUST_HINT: Record<PluginOut['trust'], string> = {
  trusted: 'loaded through the operator\u2019s own channel (in-repo pack or pip install)',
  signed: 'detached signature by a publisher approved in the drop-in trust store',
  acknowledged: 'exact bytes pinned by the operator after review',
};

type LoadState = 'loading' | 'error' | 'ready';

export function PluginsScreen({ onExit, ctxRef }: PluginsScreenProps) {
  const [plugins, setPlugins] = useState<readonly PluginOut[]>([]);
  const [refused, setRefused] = useState<readonly PluginRefusalOut[]>([]);
  const [loadState, setLoadState] = useState<LoadState>('loading');
  const [reload, setReload] = useState(0);

  // this screen is the top focus scope: Esc pops it (the universal back)
  useFocusScope('pluginsScreen', () => {
    onExit();
    return true;
  });

  ctxRef.current = { pluginsOpen: true };
  useEffect(
    () => () => {
      ctxRef.current = {};
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  useEffect(() => {
    let cancelled = false;
    setLoadState('loading');
    api
      .plugins()
      .then((out) => {
        if (cancelled) return;
        setPlugins(out.plugins);
        setRefused(out.refused);
        setLoadState('ready');
      })
      .catch(() => {
        if (!cancelled) setLoadState('error');
      });
    return () => {
      cancelled = true;
    };
  }, [reload]);

  useCommand(
    {
      id: 'plugins.reload',
      title: 'Plugins: refresh the inventory',
      run: () => setReload((n) => n + 1),
    },
    'F5',
    (ctx) => ctx.scopeId === 'pluginsScreen',
  );

  return (
    <div className="screen">
      <header className="bar">
        <span className="brand">LedgerForge</span>
        <span className="mode">Plugins — loaded packs &amp; trust</span>
        <span className="spacer" />
        <button type="button" className="date-chip" onClick={onExit}>
          Voucher entry <kbd>Esc</kbd>
        </button>
      </header>

      <main className="plugins-body">
        {loadState === 'loading' && (
          <p className="payslip-note" role="status">
            Loading the plugin inventory…
          </p>
        )}
        {loadState === 'error' && (
          <p className="payslip-note bad" role="alert">
            Cannot reach the engine — is it running? (make dev)
          </p>
        )}
        {loadState === 'ready' && (
          <>
            <section className="payslip-card plugins-card" aria-label="Loaded plugins">
              <div className="payslip-card-head">
                <strong>Loaded — {plugins.length} pack{plugins.length === 1 ? '' : 's'}</strong>
                <button
                  type="button"
                  className="daybook-reload"
                  aria-label="Refresh the inventory"
                  onClick={() => setReload((n) => n + 1)}
                >
                  Refresh
                </button>
              </div>
              <table className="daybook-entries plugins-table" aria-label="Loaded plugins">
                <thead>
                  <tr>
                    <th scope="col">Pack</th>
                    <th scope="col">Version</th>
                    <th scope="col">API</th>
                    <th scope="col">Channel</th>
                    <th scope="col">Trust</th>
                  </tr>
                </thead>
                <tbody>
                  {plugins.map((p) => (
                    <tr key={`${p.source}/${p.name}`} title={TRUST_HINT[p.trust]}>
                      <td className="mono">{p.name}</td>
                      <td className="mono">{p.version}</td>
                      <td className="mono">{p.api_compat}</td>
                      <td>{SOURCE_LABEL[p.source]}</td>
                      <td>
                        <span className={`plugins-trust ${p.trust}`}>{TRUST_LABEL[p.trust]}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="payslip-note">
                <strong>trusted</strong> = the operator&rsquo;s channel (built-in pack or pip
                install) · <strong>signed</strong> = approved publisher&rsquo;s detached signature
                (verified before the plugin&rsquo;s first line of code runs) ·{' '}
                <strong>acknowledged</strong> = operator-pinned bytes. The engine re-validates the
                three financial laws after every plugin hook — a plugin can enrich or veto a
                posting; it can never corrupt one.
              </p>
            </section>

            {refused.length > 0 && (
              <section className="payslip-card plugins-card" aria-label="Refused plugins">
                <div className="payslip-card-head">
                  <strong>Refused at boot — {refused.length}</strong>
                  <span>the trust gate (ADR-016) at work</span>
                </div>
                <table className="daybook-entries plugins-table" aria-label="Refused plugins">
                  <thead>
                    <tr>
                      <th scope="col">Module</th>
                      <th scope="col">Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {refused.map((r) => (
                      <tr key={r.module}>
                        <td className="mono">{r.module}</td>
                        <td className="plugins-reason">{r.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="payslip-note">
                  Unsigned or tampered drop-ins never load — and never execute. Fix with{' '}
                  <code>ledgerforge plugins install &lt;name&gt;</code> (community index) or{' '}
                  <code>ledgerforge plugins ack &lt;module&gt;</code> (a pack you reviewed
                  yourself), then restart.
                </p>
              </section>
            )}
          </>
        )}
      </main>

      <footer className="daybook-hud" aria-hidden="true">
        <span>
          <kbd>Esc</kbd> back
        </span>
        <span>installing &amp; signing are CLI acts — `ledgerforge plugins --help`</span>
      </footer>
    </div>
  );
}
