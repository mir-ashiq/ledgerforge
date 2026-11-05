# ADR-004: Frontend — React+TS host over a framework-agnostic keyboard command framework

- **Status:** Accepted (2026-11-06)
- **Deciders:** mir-ashiq
- **Supersedes:** —

## Context

The web UI must replicate TallyPrime's mouse-free velocity: two-key navigation contract, F-key mode switching, mid-flight master creation, zero-latency type-ahead. It must also host third-party plugin dashboards (ADR-003). Full analysis: `docs/analysis.md` §6.

## Decision

- **Host:** React 18 + TypeScript + Vite. Rationale: largest plugin-dashboard ecosystem, controlled-input reliability, community talent pool; raw renderer speed is not the bottleneck (keystroke latency is solved by architecture).
- **Interaction core:** framework-agnostic TypeScript in `frontend/keyboard-kit`:
  1. **Command Registry** — every action is `Command {id, title, run, when}`;
  2. **Keybinding Resolver** — declarative bindings, chords, platform normalization, `when` predicates;
  3. **Focus Scope Stack** — `Esc` pops scopes (Tally's universal back), roving focus per scope;
  4. **Single capture-phase pipeline** — one `keydown` listener; IME-composition-aware; no widget key races.
- **Default keymap** mirrors TallyPrime (`Ctrl+A` accept, `Alt+C` create-ledger-here, `Alt+D` delete line, `F2` date, `F4–F9` voucher types, `Esc` back) and ships as data, user-remappable.
- **Amounts on the wire are strings**; the client does display formatting only — no float arithmetic (Law 3).

## Consequences

- **(+)** Keyboard-first doubles as the accessibility model (commands have titles; focus is explicit).
- **(+))** Plugin commands integrate into the palette/keymap for free.
- **(−)** The React bet; mitigated by keeping the interaction core framework-agnostic so an alternative host remains possible.

## Alternatives rejected

- **Svelte/Solid host:** faster renderers, but ecosystem and plugin-authoring pool are the binding constraints for a community project.
- **Per-widget key handling:** key-handling races and unmaintainable shortcuts — the classic web app failure mode this framework exists to eliminate.
