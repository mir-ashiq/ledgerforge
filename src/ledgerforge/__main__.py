"""The ``ledgerforge`` command line — plugin publishing and trust operations.

``ledgerforge plugins …`` is the operator/author side of ADR-016:

- authors: ``keygen`` (publisher identity) and ``sign`` (a detached
  Ed25519 signature over one release's facts + artifact bytes);
- the index: ``index`` (list the community index), ``install``
  (fetch, verify, and land a community plugin in a drop-in directory),
  ``verify-index`` (audit every entry against its artifact) and
  ``rotate-curator`` (re-sign the index onto a fresh curator key);
- operators: ``list`` (what would load, and why) and ``ack`` (pin a
  reviewed local plugin's exact bytes into the trust store).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import PLUGIN_API_VERSION, __version__
from .plugins import _api_compatible
from .plugins.trust import (
    SCHEME,
    TrustError,
    TrustStore,
    fingerprint,
    generate_publisher_keypair,
    load_private_key,
    public_key_b64,
    sha256_hex,
    sign_artifact,
    static_manifest_facts,
    static_manifest_facts_bytes,
    verify_signature,
)

#: Where the curated index lives in this repository (``--file`` overrides).
DEFAULT_INDEX = (
    Path(__file__).resolve().parent.parent.parent / "community" / "plugins" / "index.json"
)
DEFAULT_DROPIN_DIR = Path("plugins")


# ------------------------------------------------------------- the index


def _load_index(ref: str) -> tuple[dict[str, Any], str]:
    """Load the community index from a local path or an http(s) URL.

    Returns ``(index, base)`` where *base* resolves relative artifact
    references (the index directory, or the URL prefix).
    """
    if ref.startswith(("http://", "https://")):
        import httpx

        response = httpx.get(ref, follow_redirects=True, timeout=30)
        response.raise_for_status()
        return response.json(), ref.rsplit("/", 1)[0] + "/"
    path = Path(ref)
    return json.loads(path.read_text()), str(path.resolve().parent)


def _fetch_artifact(base: str, ref: str) -> bytes:
    if ref.startswith(("http://", "https://")):
        import httpx

        response = httpx.get(ref, follow_redirects=True, timeout=60)
        response.raise_for_status()
        return response.content
    return (Path(base) / ref).read_bytes()


def _find_entry(index: dict[str, Any], name: str) -> dict[str, Any]:
    for entry in index.get("plugins", []):
        if entry.get("name") == name:
            return entry
    raise SystemExit(f"error: no plugin named {name!r} in the index")


def _entry_signature_doc(entry: dict[str, Any]) -> dict[str, Any]:
    """The index entry as a signature document: what the curator vouches for."""
    try:
        return {
            "scheme": SCHEME,
            "name": entry["name"],
            "version": entry["version"],
            "api_compat": entry["api_compat"],
            "sha256": entry["sha256"],
            "public_key": entry["publisher_key"],
            "signature": entry["signature"],
        }
    except KeyError as exc:
        raise SystemExit(f"error: index entry is malformed, missing {exc}") from exc


# --------------------------------------------------------------- commands


def cmd_keygen(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    private_pem, public_b64 = generate_publisher_keypair()
    private_path = out / "publisher_ed25519.pem"
    public_path = out / "publisher_ed25519.pub"
    private_path.write_text(private_pem)
    private_path.chmod(0o600)
    public_path.write_text(public_b64 + "\n")
    print(f"publisher private key : {private_path}  (never leave your machine, never commit)")
    print(f"publisher public key  : {public_path}")
    print(f"fingerprint           : {fingerprint(public_b64)}")
    print("\nPublish the public key with your index entry; sign releases with:")
    print(f"  ledgerforge plugins sign <release.py> --key {private_path}")
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    artifact_path = Path(args.file)
    if not artifact_path.is_file():
        raise SystemExit(f"error: {artifact_path} is not a file")
    facts = static_manifest_facts(artifact_path)
    if facts is None:
        raise SystemExit(
            "error: PLUGIN = PluginManifest(name=…, version=…, api_compat=…) must be "
            "statically readable (plain string keywords) — a signature pins facts"
        )
    sig_doc = sign_artifact(
        Path(args.key).read_text(),
        name=facts["name"],
        version=facts["version"],
        api_compat=facts["api_compat"],
        artifact=artifact_path.read_bytes(),
    )
    sig_path = artifact_path.with_name(artifact_path.name + ".sig")
    sig_path.write_text(json.dumps(sig_doc, indent=2, sort_keys=True) + "\n")
    print(
        f"signed {artifact_path.name} as {facts['name']} {facts['version']} "
        f"(API {facts['api_compat']})"
    )
    print(f"signature : {sig_path}")
    print(f"sha256    : {sig_doc['sha256']}")
    print(f"publisher : {fingerprint(sig_doc['public_key'])}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    artifact_path = Path(args.file)
    sig_path = Path(args.sig) if args.sig else artifact_path.with_name(artifact_path.name + ".sig")
    try:
        sig_doc = json.loads(sig_path.read_text())
        facts = verify_signature(sig_doc, artifact_path.read_bytes())
    except (OSError, json.JSONDecodeError, TrustError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    if args.key and Path(args.key).read_text().strip() != facts.public_key:
        print("INVALID: signed by a different key than the one given", file=sys.stderr)
        return 1
    print(f"valid: {facts.name} {facts.version} (API {facts.api_compat})")
    print(f"  sha256    : {facts.sha256}")
    print(f"  publisher : {fingerprint(facts.public_key)}")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    index, _base = _load_index(args.file)
    print(
        "community plugin index "
        f"(format {index.get('index_version')}, updated {index.get('updated', '?')})"
    )
    print()
    for entry in index.get("plugins", []):
        print(f"{entry['name']} {entry['version']}  (API {entry['api_compat']})")
        print(f"  {entry.get('summary', '')}")
        print(
            f"  author {entry.get('author', '?')} · publisher {fingerprint(entry['publisher_key'])}"
        )
    return 0


def cmd_verify_index(args: argparse.Namespace) -> int:
    """Audit the whole index in one pass: every entry's artifact must match
    the sha256 the curator vouches for, the signature must verify, the
    facts must match the artifact's static manifest, and the plugin API
    floor must hold. The pre-flight check before and after a key rotation,
    and a general integrity report for operators mirroring an index."""
    index, base = _load_index(args.file)
    entries = index.get("plugins", [])
    failures = 0
    for entry in entries:
        where = f"{entry.get('name', '?')} {entry.get('version', '?')}"
        try:
            artifact = _fetch_artifact(base, entry["artifact"])
            if sha256_hex(artifact) != entry["sha256"]:
                raise TrustError("artifact bytes do not match the index's sha256")
            facts = verify_signature(_entry_signature_doc(entry), artifact)
            static = static_manifest_facts_bytes(artifact)
            if static != {
                "name": facts.name,
                "version": facts.version,
                "api_compat": facts.api_compat,
            }:
                raise TrustError(
                    f"artifact declares {static}, the index entry pins "
                    f"{facts.name} {facts.version} (API {facts.api_compat})"
                )
            if not _api_compatible(facts.api_compat):
                raise TrustError(
                    f"targets plugin API {facts.api_compat}; this build "
                    f"supports {PLUGIN_API_VERSION}"
                )
        except Exception as exc:  # noqa: BLE001 — any failure is one report line
            failures += 1
            print(f"INVALID  {where} — {exc}")
        else:
            print(f"ok       {where}  publisher {fingerprint(facts.public_key)}")
    total = len(entries)
    verdict = "" if failures else " — index is consistent"
    print(f"\n{total - failures}/{total} entries verified{verdict}")
    return 1 if failures else 0


def cmd_rotate_curator(args: argparse.Namespace) -> int:
    """Re-sign every index entry onto a fresh curator key (ADR-016's
    rotation runbook, tooled). The old signatures are verified first — a
    rotation is a re-attestation, never an opportunity to smuggle in
    bytes or facts the old key did not vouch for. Entry digests never
    change: rotation re-signs the same claims, it does not republish."""
    index, base = _load_index(args.file)
    new_private_pem = Path(args.new_key).read_text()
    try:
        new_public = public_key_b64(load_private_key(new_private_pem).public_key())
    except (TrustError, ValueError, TypeError) as exc:
        raise SystemExit(f"error: --new-key is not a usable Ed25519 private key: {exc}") from exc

    old_public: str | None = None
    if args.old_key:
        try:
            old_public = public_key_b64(
                load_private_key(Path(args.old_key).read_text()).public_key()
            )
        except (TrustError, ValueError, TypeError) as exc:
            raise SystemExit(
                f"error: --old-key is not a usable Ed25519 private key: {exc}"
            ) from exc

    problems: list[str] = []
    for entry in index.get("plugins", []):
        name = entry.get("name", "?")
        try:
            artifact = _fetch_artifact(base, entry["artifact"])
            facts = verify_signature(_entry_signature_doc(entry), artifact)
        except Exception as exc:  # noqa: BLE001 — report, never half-rotate
            problems.append(f"{name}: the existing signature does not verify — {exc}")
            continue
        if old_public is not None and entry.get("publisher_key") != old_public:
            problems.append(
                f"{name}: signed by a different key than --old-key "
                "(mixed-key index?) — refusing to rotate it blind"
            )
            continue
        fresh = sign_artifact(
            new_private_pem,
            name=facts.name,
            version=facts.version,
            api_compat=facts.api_compat,
            artifact=artifact,  # same bytes: the digest in the claim cannot move
        )
        entry["publisher_key"] = fresh["public_key"]
        entry["signature"] = fresh["signature"]

    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        print("index NOT rewritten — fix or drop the failing entries, then re-run", file=sys.stderr)
        return 1

    index["updated"] = datetime.now(UTC).date().isoformat()
    if args.file.startswith(("http://", "https://")) and not args.out:
        raise SystemExit(
            "error: rotating a remote index requires --out (write locally, review, then publish)"
        )
    out_path = Path(args.out) if args.out else Path(args.file)
    out_path.write_text(json.dumps(index, indent=2) + "\n")

    count = len(index.get("plugins", []))
    print(f"rotated {count} entries onto curator key {fingerprint(new_public)}")
    print(f"wrote {out_path}")
    print("\nnext steps (the runbook, community/plugins/README.md):")
    print("  1. `ledgerforge plugins verify-index --file <index>` — prove the result")
    print("  2. commit the re-signed index and open the PR; fresh installs now")
    print("     approve the new key automatically")
    print("  3. announce the old fingerprint as retired — operators holding it")
    print("     keep their existing installs; the next release of a plugin asks")
    print("     for one re-approval (`ledgerforge plugins install <name>`)")
    print("  4. destroy the old private key once the PR is everywhere")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    index, base = _load_index(args.file)
    entry = _find_entry(index, args.name)
    artifact_ref = entry["artifact"]
    artifact = _fetch_artifact(base, artifact_ref)
    if sha256_hex(artifact) != entry["sha256"]:
        raise SystemExit(
            f"error: {artifact_ref} does not match the index's sha256 — not installing"
        )

    facts = verify_signature(_entry_signature_doc(entry), artifact)  # raises TrustError

    static = static_manifest_facts_bytes(artifact)
    if static != {"name": facts.name, "version": facts.version, "api_compat": facts.api_compat}:
        raise SystemExit(
            f"error: artifact declares {static} but the index entry is "
            f"{facts.name} {facts.version} (API {facts.api_compat}) — not installing"
        )
    if not _api_compatible(facts.api_compat):
        raise SystemExit(
            f"error: {facts.name} targets plugin API {facts.api_compat}; this build "
            f"supports {PLUGIN_API_VERSION} — install would refuse it at boot anyway"
        )

    dropin_dir = Path(args.path)
    dropin_dir.mkdir(parents=True, exist_ok=True)
    module_stem = Path(artifact_ref).stem
    (dropin_dir / f"{module_stem}.py").write_bytes(artifact)
    (dropin_dir / f"{module_stem}.py.sig").write_text(
        json.dumps(_entry_signature_doc(entry), indent=2, sort_keys=True) + "\n"
    )
    store = TrustStore.load(dropin_dir)
    fp = store.approve_publisher(facts.public_key)
    store.save()
    print(
        f"installed {facts.name} {facts.version} (API {facts.api_compat}) "
        f"→ {dropin_dir / module_stem}.py"
    )
    print(f"publisher approved : {fp}  ({dropin_dir / '.trust.json'})")
    print("loads at next boot with LF_PLUGIN_PATH including this directory:")
    print(f"  LF_PLUGIN_PATH={dropin_dir.resolve()}")
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    dropin_dir = Path(args.path)
    name = args.module[:-3] if args.module.endswith(".py") else args.module
    plugin_file = dropin_dir / f"{name}.py"
    if not plugin_file.is_file():
        raise SystemExit(f"error: {plugin_file} is not a file")
    store = TrustStore.load(dropin_dir)
    store.acknowledge(plugin_file.stem, sha256_hex(plugin_file.read_bytes()))
    store.save()
    pin = store.acknowledged[plugin_file.stem]["sha256"][:16]
    print(f"acknowledged {plugin_file.name} — pinned sha256 {pin}…")
    print("it will load at next boot as trust=acknowledged; any byte change voids the pin.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    dropin_dir = Path(args.path)
    print(f"drop-in directory: {dropin_dir.resolve()}")
    if not dropin_dir.is_dir():
        print("  (does not exist)")
        return 0
    store = TrustStore.load(dropin_dir)
    py_files = sorted(p for p in dropin_dir.glob("*.py") if not p.stem.startswith("_"))
    if not py_files:
        print("  (empty)")
    for py_file in py_files:
        digest = sha256_hex(py_file.read_bytes())
        sig_path = py_file.with_name(py_file.name + ".sig")
        notes = []
        if store.acknowledgement_matches(py_file.stem, digest):
            print(f"{py_file.name:32} acknowledged (operator-pinned bytes)")
            continue
        if not sig_path.exists():
            print(f"{py_file.name:32} REFUSED — no signature, no acknowledgement")
            continue
        try:
            sig_doc = json.loads(sig_path.read_text())
            facts = verify_signature(sig_doc, py_file.read_bytes())
        except (json.JSONDecodeError, TrustError) as exc:
            print(f"{py_file.name:32} REFUSED — {exc}")
            continue
        static = static_manifest_facts(py_file)
        if static != {"name": facts.name, "version": facts.version, "api_compat": facts.api_compat}:
            print(f"{py_file.name:32} REFUSED — signed facts do not match the module's manifest")
            continue
        verdict = (
            "signed"
            if store.is_approved(facts.public_key)
            else ("REFUSED — publisher not approved")
        )
        if not store.is_approved(facts.public_key):
            notes.append("approve with: ledgerforge plugins install …, or edit .trust.json")
        print(
            f"{py_file.name:32} {verdict} — {facts.name} {facts.version} (API {facts.api_compat})"
        )
        print(f"{'':34} publisher {fingerprint(facts.public_key)}")
        for note in notes:
            print(f"{'':34} {note}")
    return 0


# ------------------------------------------------------------------ main


# --------------------------------------------------------- scale-out (ADR-018)


def _ops_session():
    """A database session for scale-out operations (LF_DATABASE_URL)."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from .settings import get_settings

    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    return Session(engine), engine, text


def cmd_scale_out_status(args) -> int:
    session, engine, _text = _ops_session()
    try:
        rows = session.execute(
            _text("SELECT out_name, out_value FROM ops.scale_out_status()")
        ).all()
        for name, value in rows:
            print(f"{name:32s} {value}")
    finally:
        session.close()
        engine.dispose()
    return 0


def cmd_scale_out_refresh(args) -> int:
    session, engine, _text = _ops_session()
    try:
        rows = session.execute(
            _text("SELECT * FROM rpt.refresh_balance_cache(:b)"),
            {"b": args.book},
        ).all()
        session.commit()
        for book, ledgers, horizon, action in rows:
            print(f"book {book}: {action} (rows={ledgers}, horizon={horizon})")
    finally:
        session.close()
        engine.dispose()
    return 0


def _print_steps(rows) -> None:
    for step, detail in rows:
        print(f"  [{step}] {detail}")


def cmd_scale_out_partition(args) -> int:
    session, engine, _text = _ops_session()
    try:
        rows = session.execute(
            _text("SELECT * FROM ops.partition_entries_by_year(:h)"),
            {"h": args.headroom},
        ).all()
        session.commit()
        _print_steps(rows)
    except Exception as exc:  # noqa: BLE001 — operator-facing report
        session.rollback()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
        engine.dispose()
    return 0


def cmd_scale_out_unpartition(args) -> int:
    session, engine, _text = _ops_session()
    try:
        rows = session.execute(_text("SELECT * FROM ops.unpartition_entries()")).all()
        session.commit()
        _print_steps(rows)
    except Exception as exc:  # noqa: BLE001 — operator-facing report
        session.rollback()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
        engine.dispose()
    return 0


def cmd_scale_out_ensure(args) -> int:
    session, engine, _text = _ops_session()
    try:
        rows = session.execute(
            _text("SELECT * FROM ops.ensure_entry_partitions(:f, :t)"),
            {"f": args.from_year, "t": args.to_year},
        ).all()
        session.commit()
        for year, created in rows:
            print(f"  {year}: {'created' if created else 'already present'}")
    except Exception as exc:  # noqa: BLE001 — operator-facing report
        session.rollback()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
        engine.dispose()
    return 0


# ------------------------------------------------------------- desktop (ADR-021)


def cmd_desktop(args: argparse.Namespace) -> int:
    """Delegate to the desktop edition's own CLI."""
    from .desktop import main as desktop_main

    argv: list[str] = []
    if args.data_dir:
        argv += ["--data-dir", str(args.data_dir)]
    if args.port:
        argv += ["--port", str(args.port)]
    if args.no_browser:
        argv.append("--no-browser")
    if args.smoke:
        argv.append("--smoke")
    return desktop_main(argv)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ledgerforge",
        description="LedgerForge operations — plugins, publishing and trust (ADR-016).",
    )
    parser.add_argument("--version", action="version", version=f"ledgerforge {__version__}")
    sub = parser.add_subparsers(dest="group", required=True)

    plugins = sub.add_parser("plugins", help="publish, install and trust plugins")
    psub = plugins.add_subparsers(dest="command", required=True)

    p = psub.add_parser("keygen", help="generate a publisher keypair")
    p.add_argument("--out", default=".", help="directory for the keypair (default: cwd)")
    p.set_defaults(fn=cmd_keygen)

    p = psub.add_parser("sign", help="sign one release of a plugin (author-side)")
    p.add_argument("file", help="the plugin .py file")
    p.add_argument("--key", required=True, help="publisher private key (PEM)")
    p.set_defaults(fn=cmd_sign)

    p = psub.add_parser("verify", help="verify a detached signature offline")
    p.add_argument("file", help="the plugin .py file")
    p.add_argument("--sig", help="signature file (default: <file>.sig)")
    p.add_argument("--key", help="expected publisher public key (b64 file)")
    p.set_defaults(fn=cmd_verify)

    p = psub.add_parser("index", help="list the community plugin index")
    p.add_argument("--file", default=str(DEFAULT_INDEX), help="index path or URL")
    p.set_defaults(fn=cmd_index)

    p = psub.add_parser(
        "verify-index",
        help="audit every entry in an index against its artifact (curator pre-flight)",
    )
    p.add_argument("--file", default=str(DEFAULT_INDEX), help="index path or URL")
    p.set_defaults(fn=cmd_verify_index)

    p = psub.add_parser(
        "rotate-curator",
        help="re-sign the index onto a fresh curator key (verifies every old "
        "signature first; runbook in community/plugins/README.md)",
    )
    p.add_argument("--new-key", required=True, help="the NEW curator private key (PEM)")
    p.add_argument(
        "--old-key",
        help="the OLD curator private key (PEM); when given, every entry must "
        "carry its public half — a mixed-key index is refused",
    )
    p.add_argument("--file", default=str(DEFAULT_INDEX), help="index path or URL")
    p.add_argument("--out", help="output path (default: rewrite --file; required for URLs)")
    p.set_defaults(fn=cmd_rotate_curator)

    p = psub.add_parser("install", help="install a plugin from the community index")
    p.add_argument("name", help="plugin name in the index")
    p.add_argument("--file", default=str(DEFAULT_INDEX), help="index path or URL")
    p.add_argument("--path", default=str(DEFAULT_DROPIN_DIR), help="drop-in directory")
    p.set_defaults(fn=cmd_install)

    p = psub.add_parser("ack", help="acknowledge a reviewed local plugin (pin its bytes)")
    p.add_argument("module", help="module name or .py file in the drop-in directory")
    p.add_argument("--path", default=str(DEFAULT_DROPIN_DIR), help="drop-in directory")
    p.set_defaults(fn=cmd_ack)

    p = psub.add_parser("list", help="show drop-ins and their trust status")
    p.add_argument("--path", default=str(DEFAULT_DROPIN_DIR), help="drop-in directory")
    p.set_defaults(fn=cmd_list)

    scale = sub.add_parser(
        "scale-out", help="report scale-out operations (ADR-018): status, cache, partitions"
    )
    ssub = scale.add_subparsers(dest="command", required=True)

    p = ssub.add_parser("status", help="entries shape, partitions, cache state")
    p.set_defaults(fn=cmd_scale_out_status)

    p = ssub.add_parser(
        "refresh-cache", help="incrementally rebuild the balance cache (idempotent)"
    )
    p.add_argument("--book", type=int, default=None, help="one book id (default: every book)")
    p.set_defaults(fn=cmd_scale_out_refresh)

    p = ssub.add_parser(
        "partition",
        help="convert voucher_entries to yearly range partitions (maintenance window; "
        "runs as the table owner — the connection in LF_DATABASE_URL must own it)",
    )
    p.add_argument(
        "--headroom",
        type=int,
        default=2,
        help="pre-create empty partitions this many years past the current one (default: 2)",
    )
    p.set_defaults(fn=cmd_scale_out_partition)

    p = ssub.add_parser("unpartition", help="revert to the plain entries table (tested mirror)")
    p.set_defaults(fn=cmd_scale_out_unpartition)

    p = ssub.add_parser("ensure", help="create missing yearly partitions for a year range")
    p.add_argument("from_year", type=int)
    p.add_argument("to_year", type=int)
    p.set_defaults(fn=cmd_scale_out_ensure)

    desktop = sub.add_parser(
        "desktop",
        help="the zero-config desktop edition: embedded PostgreSQL + web UI on "
        "127.0.0.1 (ADR-021; needs the 'desktop' extra)",
    )
    desktop.add_argument("--data-dir", type=Path, default=None, help="data directory")
    desktop.add_argument("--port", type=int, default=None, help="loopback port")
    desktop.add_argument("--no-browser", action="store_true", help="do not open a browser")
    desktop.add_argument("--smoke", action="store_true", help="boot headless, self-check, exit 0/1")
    desktop.set_defaults(fn=cmd_desktop)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except TrustError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
