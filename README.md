# Nexora Audit Edition

Nexora Audit Edition is a small, synthetic, executable extraction of reliability
mechanisms developed during the Nexora no-feature hardening programme. It exists
so reviewers can inspect the code, challenge bounded claims, and run the tests
without receiving the private product, its data, its interface, or its history.

This is not a deployable real-estate application, a security certification, or
proof that finite tests establish correctness. The public claim boundary is
machine-readable in [CLAIMS.json](CLAIMS.json). The 0.1.1 corrective release and
its reproduced findings are recorded in
[the adversarial review](docs/adversarial-review-0.1.1.md).
The exact initial 0.1.0 snapshot is public at
[`99935c2`](https://github.com/harrisonwolf/nexora-audit-edition/commit/99935c2),
and its complete release-level correction is visible in the
[0.1.0-to-v0.1.1 comparison](https://github.com/harrisonwolf/nexora-audit-edition/compare/99935c2...v0.1.1).

## What is included

| Mechanism | Question it addresses | Executable evidence |
| --- | --- | --- |
| Manifest-bound artifact integrity | Did the consumer receive the same bounded bytes whose identity, version, size, and digest were checked? | `tests/test_manifest.py` |
| Atomic publication | Can a validated directory become discoverable under one final name without exposing a partial tree or reclaiming an incumbent? | `tests/test_publication.py` |
| Journaled multi-target transition | Can a bounded runtime or build transition recover conservatively after each tested interruption point? | `tests/test_transition.py` |
| Durable SQLite carry-over | Can selected durable rows survive a volatile database rebuild without accepting malformed state or partial restoration? | `tests/test_sqlite_state.py` |
| Qualified composition | Does a positive-weighted input without material evidence suppress the composite instead of disappearing silently? | `tests/test_ranking.py` |

The modules use only the Python standard library at runtime.

## Important mechanics

- Manifest reads are capped at 1 MiB. Payload reads default to 64 MiB and the
  caller may choose a different positive finite ceiling.
- Publication synchronizes newly created root links, writes and synchronizes a
  complete private tree, seals its path set, entry types, permission modes, and
  unshared regular-file bytes across validation, then re-synchronizes the same
  snapshot before one direct rename into an absent final name. Every final path
  already present at preflight—including an empty directory or symlink—is a
  collision and is left untouched.
- A transition synchronizes newly created root links and accepts only canonical,
  physically contained, ancestry-disjoint targets outside its reserved control
  namespace. Under one root-global lock, it synchronizes the closed incumbent
  and staged contents, recomputes their identities, and only then writes the
  first intent marker.
- Each target rename synchronizes the changed parent before the corresponding
  completed phase can be recorded. Runtime and web-build generations receive
  type-specific postchecks before the commit point.
- Saved-report JSON rejects Python's non-standard `NaN` and infinity tokens
  without rejecting standards-valid large numeric literals.

These are local mechanisms under a declared cooperative-process model. Read
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) before relying on any of them.

## Run the audit surface

Requirements: Python 3.11 or newer on a POSIX system.

```sh
git clone https://github.com/harrisonwolf/nexora-audit-edition.git
cd nexora-audit-edition
make verify
```

`make verify` runs the test suite, compiles the Python sources, and checks the
tracked public tree for disallowed file types, executable text, local absolute
paths, and license drift.

For a clean installed-package check:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install .
python -I -c "import nexora_audit; print(nexora_audit.__version__)"
```

## How to review it

Start with [the architecture](docs/architecture.md), then use the fixed
[audit questions](docs/audit-questions.md). The source lineage and subsequent
public hardening are distinguished in [SOURCE_MAP.md](SOURCE_MAP.md).

A coding agent can help search the state space, but its report is an argument
to reproduce, not a certification. Require exact paths, a concrete failure
trace, and a test that fails before a proposed fix. False positives and
documented limitations should be labeled as such.

Reproducible challenges to a public claim are welcome through the
[claim-falsification issue form](https://github.com/harrisonwolf/nexora-audit-edition/issues/new?template=claim-falsification.yml).
Use private security reporting instead when a reproduction is sensitive.

## Authorship

Harrison Wolf directed the extraction, claim scoping, adversarial review, and
release. Coding agents produced most of the implementation under executable
tests and release gates; their reports and patches were treated as untrusted
proposals, not certifications.

## Scope

The edition contains synthetic identities, payloads, and SQLite rows only. It
does not contain third-party datasets, client information, credentials,
production configuration, the product interface, or the private repository's
history. Private source identifiers in the source map are maintainer provenance
attestations; the public code, tests, and history must carry the public warrant.

## License

Copyright © 2026 Harrison Wolf.

Licensed under the GNU Affero General Public License, version 3 only
([AGPL-3.0-only](LICENSE)). See [NOTICE.md](NOTICE.md).
