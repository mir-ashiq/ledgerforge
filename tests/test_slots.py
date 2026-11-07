"""Sandboxed UI slots (ADR-019, plugin API 0.7): manifest declaration, the
host's slot registry API, the sandboxed document serving, and the loader's
validation refusals."""

from __future__ import annotations

import pytest

from ledgerforge.plugins import PluginManager, PluginManifest, SlotSpec


def _pack(name: str, slots=(), api: str = "0.7"):
    """A minimal module exposing a manifest with the given slots."""

    class M:
        PLUGIN = PluginManifest(name=name, version="1.0.0", api_compat=api, slots=tuple(slots))

        @staticmethod
        def register(registry):
            return None

    return M


# ------------------------------------------------------------ the registry
def test_slots_endpoint_lists_declared_slots(client, fresh_db):
    r = client.get("/api/v1/slots")
    assert r.status_code == 200, r.text
    slots = r.json()["slots"]
    keys = {s["key"] for s in slots}
    assert "balance-glance" in keys  # the example-slot teaching pack
    glance = next(s for s in slots if s["key"] == "balance-glance")
    assert glance["plugin"] == "example-slot"
    assert glance["source"] == "entry-point"
    assert glance["trust"] == "trusted"
    assert glance["url"] is None
    assert "sandbox" in glance["title"].lower()


def test_slot_document_served_sandboxed(client, fresh_db):
    r = client.get("/api/v1/slots/balance-glance")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    # the browser treats the document as sandboxed even if an embedder
    # forgets the iframe attribute (defense in depth, ADR-019)
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    body = r.text
    assert "lf-slot:register-commands" in body
    assert "lf-slot:request" in body  # the host relay, not direct fetches


def test_unknown_slot_404(client, fresh_db):
    r = client.get("/api/v1/slots/does-not-exist")
    assert r.status_code == 404
    assert "balance-glance" in r.json()["detail"]  # points at what exists


# ------------------------------------------------------ loader validation
def test_invalid_slot_specs_refuse_the_load():
    mgr = PluginManager()
    cases = [
        SlotSpec(key="Bad Key", title="T", html="<p>x</p>"),  # not a slug
        SlotSpec(key="ok", title="", html="<p>x</p>"),  # no title
        SlotSpec(key="ok", title="T"),  # neither html nor url
        SlotSpec(key="ok", title="T", html="<p>x</p>", url="https://x.example"),  # both
        SlotSpec(key="ok", title="T", url="ftp://x.example"),  # not http(s)
        SlotSpec(key="ok", title="T", html="x" * 131_073),  # over the size cap
    ]
    for spec in cases:
        with pytest.raises(RuntimeError, match="invalid slot"):
            mgr.load_module(_pack("bad-slot-pack", [spec]))
    assert mgr.slot_specs() == []


def test_duplicate_slot_keys_refuse_the_second_pack():
    mgr = PluginManager()
    mgr.load_module(_pack("first-pack", [SlotSpec(key="glance", title="A", html="<p>a</p>")]))
    with pytest.raises(RuntimeError, match="already owns"):
        mgr.load_module(_pack("second-pack", [SlotSpec(key="glance", title="B", html="<p>b</p>")]))
    assert [s.key for _r, s in mgr.slot_specs()] == ["glance"]


def test_url_slot_lists_with_url_and_is_not_served_inline():
    mgr = PluginManager()
    mgr.load_module(
        _pack("url-pack", [SlotSpec(key="ext", title="External", url="https://dash.example/x")])
    )
    ((record, spec),) = mgr.slot_specs()
    assert spec.url == "https://dash.example/x"
    assert record.manifest.name == "url-pack"


def test_slot_manifest_compatible_with_trust_gate_facts():
    """ADR-016's static facts read name/version/api_compat without executing
    the module — a slots-declaring manifest keeps that property (the slot
    HTML itself is covered by the artifact-bytes signature)."""
    from ledgerforge.plugins.trust import static_manifest_facts_bytes

    source = (
        b"from ledgerforge.plugins import PluginManifest, SlotSpec\n"
        b"PLUGIN = PluginManifest(\n"
        b'    name="s", version="1.0.0", api_compat="0.7",\n'
        b"    slots=(SlotSpec(key='k', title='T', html='<p>hi</p>'),),\n"
        b")\n"
    )
    facts = static_manifest_facts_bytes(source)
    assert facts == {"name": "s", "version": "1.0.0", "api_compat": "0.7"}


def test_example_slot_pack_loads_standalone():
    """The teaching module loads in a standalone manager (plugin-guide
    pattern) and contributes exactly one well-formed slot."""
    from ledgerforge.contrib import example_slot

    mgr = PluginManager()
    mgr.load_module(example_slot)
    ((record, spec),) = mgr.slot_specs()
    assert record.manifest.name == "example-slot"
    assert spec.key == "balance-glance"
    spec.validate()
