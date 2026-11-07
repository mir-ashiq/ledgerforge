"""The ``ledgerforge`` command line — plugin publishing and trust operations.

``ledgerforge plugins …`` is the operator/author side of ADR-016:

- authors: ``keygen`` (publisher identity) and ``sign`` (a detached
  Ed25519 signature over one release's facts + artifact bytes);
- the index: ``index`` (list the community index) and ``install``
  (fetch, verify, and land a community plugin in a drop-in directory);
- operators: ``list`` (what would load, and why) and ``ack`` (pin a
  reviewed local plugin's exact bytes into the trust store).
"""

from __future__ import annotations

import argparse
import json
import sys
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

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except TrustError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
