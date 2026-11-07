"""Plugin trust: ed25519 signatures over release facts (ADR-016).

A drop-in plugin is code that landed on disk outside the operator's
pip-install channel, so it loads only with proof:

- a **detached signature** (``<module>.py.sig``) by an approved publisher,
  covering the artifact bytes *and* the manifest facts (name, version,
  ``api_compat``) — a signature cannot be replayed onto another release or
  onto a renamed artifact; or
- an **operator acknowledgement** pinning the exact bytes (sha256) of a
  plugin the operator has reviewed.

The signature is verified **before the module is ever imported**: the
manifest facts are extracted statically (AST), so unsigned or tampered code
never executes at boot. What the model does *not* claim: in-process Python
cannot be sandboxed. A publisher whose key the operator approved is trusted
the way an apt repository is — the gate is provenance and refusal, not a
sandbox. Untrusted work belongs to the after-commit event tier (§5.3).

Key format: Ed25519. Private keys are PEM (PKCS8); public keys travel as
base64 raw 32-byte keys in the signature document and the trust store.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

logger = logging.getLogger("ledgerforge.plugins.trust")

#: The signature scheme identifier embedded in every signature document.
SCHEME = "ed25519"

#: The trust-store file that lives in each drop-in directory.
TRUST_FILE = ".trust.json"


class TrustError(Exception):
    """A trust decision failed — refuse to load, loudly."""


# ---------------------------------------------------------------- keys


def generate_publisher_keypair() -> tuple[str, str]:
    """A fresh publisher keypair: ``(private_pem, public_key_b64)``.

    The private half never leaves the author's machine; the public half is
    the identity the community index records and the trust store approves.
    """
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return pem, public_key_b64(private.public_key())


def public_key_b64(public_key: Ed25519PublicKey) -> str:
    return base64.b64encode(public_key.public_bytes_raw()).decode()


def load_private_key(pem: str) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TrustError("not an Ed25519 private key")
    return key


def load_public_key(key_b64: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(key_b64.strip(), validate=True)
    except Exception as exc:  # noqa: BLE001 — any decode failure is a trust failure
        raise TrustError(f"public key is not valid base64: {exc}") from exc
    if len(raw) != 32:
        raise TrustError(f"Ed25519 public key must be 32 bytes, got {len(raw)}")
    return Ed25519PublicKey.from_public_bytes(raw)


def fingerprint(key_b64: str) -> str:
    """A short, printable identity for a public key (first 16 hex of sha256)."""
    digest = hashlib.sha256(key_b64.strip().encode()).hexdigest()
    return ":".join(digest[i : i + 4] for i in range(0, 16, 4)).upper()


# ------------------------------------------------------------ payloads


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_payload(name: str, version: str, api_compat: str, digest: str) -> bytes:
    """The exact bytes a signature covers: the manifest facts + artifact digest.

    Sorted keys, no whitespace — one canonical form, so a signature made by
    the CLI verifies at boot regardless of JSON formatting choices.
    """
    return json.dumps(
        {"api_compat": api_compat, "name": name, "sha256": digest, "version": version},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def sign_artifact(
    private_pem: str, *, name: str, version: str, api_compat: str, artifact: bytes
) -> dict[str, Any]:
    """Sign one release: the signature document written as ``<file>.py.sig``."""
    private = load_private_key(private_pem)
    digest = sha256_hex(artifact)
    signature = private.sign(canonical_payload(name, version, api_compat, digest))
    return {
        "scheme": SCHEME,
        "name": name,
        "version": version,
        "api_compat": api_compat,
        "sha256": digest,
        "public_key": public_key_b64(private.public_key()),
        "signature": base64.b64encode(signature).decode(),
    }


@dataclass(frozen=True)
class VerifiedFacts:
    """What a valid signature proves: these facts, about these bytes, by this key."""

    name: str
    version: str
    api_compat: str
    sha256: str
    public_key: str


def verify_signature(sig_doc: dict[str, Any], artifact: bytes) -> VerifiedFacts:
    """Verify a signature document against artifact bytes.

    Raises :class:`TrustError` on any mismatch — scheme, digest, or the
    signature itself. This checks the *mathematics* only; whether the
    publisher is *approved* is the trust store's question (and the caller's).
    """
    try:
        scheme = sig_doc["scheme"]
        name = sig_doc["name"]
        version = sig_doc["version"]
        api_compat = sig_doc["api_compat"]
        digest = sig_doc["sha256"]
        key_b64 = sig_doc["public_key"]
        signature_b64 = sig_doc["signature"]
    except (KeyError, TypeError) as exc:
        raise TrustError(f"signature document is malformed: missing {exc}") from exc
    if scheme != SCHEME:
        raise TrustError(f"unsupported signature scheme {scheme!r}")
    if digest != sha256_hex(artifact):
        raise TrustError(
            "artifact bytes do not match the signed sha256 — the file changed after signing"
        )
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise TrustError(f"signature is not valid base64: {exc}") from exc
    try:
        load_public_key(key_b64).verify(
            signature, canonical_payload(name, version, api_compat, digest)
        )
    except TrustError:
        raise
    except Exception as exc:  # noqa: BLE001 — InvalidSignature and friends
        raise TrustError(f"signature does not verify: {exc}") from exc
    return VerifiedFacts(
        name=name, version=version, api_compat=api_compat, sha256=digest, public_key=key_b64
    )


# --------------------------------------------------- static manifest facts


def static_manifest_facts_bytes(source: bytes) -> dict[str, str] | None:
    """Extract ``PLUGIN = PluginManifest(name=…, version=…, api_compat=…)``
    from plugin source **without executing it**.

    Returns ``None`` when the manifest is not statically readable (computed,
    imported, conditional) — such a plugin can still load through the
    acknowledgement path, but never on a signature alone: the gate runs
    before the first line of plugin code executes.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == "PLUGIN"):
            continue
        if not isinstance(node.value, ast.Call):
            return None
        facts: dict[str, str] = {}
        for kw in node.value.keywords:
            if (
                kw.arg in ("name", "version", "api_compat")
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                facts[kw.arg] = kw.value.value
        return facts if len(facts) == 3 else None
    return None


def static_manifest_facts(path: Path) -> dict[str, str] | None:
    """:func:`static_manifest_facts_bytes` for a file on disk."""
    return static_manifest_facts_bytes(path.read_bytes())


# ------------------------------------------------------------- trust store


@dataclass
class TrustStore:
    """The per-directory trust anchors: approved publishers + acknowledged pins.

    Lives at ``<drop-in-dir>/.trust.json``. Publishers are recorded by
    fingerprint (key → identity); acknowledgements pin a module's exact
    bytes. Everything is plain JSON — operators can read it, diff it, and
    put it under review like any other configuration.
    """

    path: Path
    publishers: dict[str, str] = field(default_factory=dict)
    acknowledged: dict[str, dict[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.path.name == TRUST_FILE:
            raise TrustError(f"trust store path must be a {TRUST_FILE} file, got {self.path}")

    # ------------------------------------------------------------ loading
    @classmethod
    def load(cls, directory: Path) -> TrustStore:
        path = directory / TRUST_FILE
        if not path.exists():
            return cls(path=path)
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise TrustError(f"{path} is not valid JSON: {exc}") from exc
        return cls(
            path=path,
            publishers={str(k): str(v) for k, v in data.get("publishers", {}).items()},
            acknowledged={
                str(k): {
                    "sha256": str(v.get("sha256", "")),
                    "acknowledged_at": str(v.get("acknowledged_at", "")),
                }
                for k, v in data.get("acknowledged", {}).items()
            },
        )

    def save(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "publishers": self.publishers,
                    "acknowledged": self.acknowledged,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    # --------------------------------------------------------- publishers
    def approve_publisher(self, key_b64: str) -> str:
        """Record a publisher key as approved; returns its fingerprint."""
        fp = fingerprint(key_b64)
        self.publishers[fp] = key_b64.strip()
        return fp

    def is_approved(self, key_b64: str) -> bool:
        return key_b64.strip() in self.publishers.values()

    # ---------------------------------------------------- acknowledgements
    def acknowledge(self, module: str, digest: str) -> None:
        self.acknowledged[module] = {
            "sha256": digest,
            "acknowledged_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    def acknowledgement_matches(self, module: str, digest: str) -> bool:
        record = self.acknowledged.get(module)
        return record is not None and record["sha256"] == digest
