# Nexora Audit Edition

Nexora Audit Edition is a small, synthetic, executable extraction of reliability
mechanisms developed during the Nexora no-feature hardening programme. It exists
so that reviewers can inspect the code, challenge the claims, and run the tests
without receiving the private product, its data, its interface, or its history.

This is not a deployable real-estate application, a security certification, or a
claim that finite tests prove correctness. The public claim boundary is
machine-readable in [CLAIMS.json](CLAIMS.json).

## What is included

| Mechanism | Question it addresses | Executable evidence |
| --- | --- | --- |
| Manifest-bound artifact integrity | Did the consumer receive the same bytes whose identity, version, size, and digest were checked? | `tests/test_manifest.py` |
| Atomic publication | Can a validated directory become discoverable under one final name without exposing a partial tree? | `tests/test_publication.py` |
| Journaled multi-target transition | Can a bounded three-target filesystem transition recover conservatively after each tested interruption point? | `tests/test_transition.py` |
| Durable SQLite carry-over | Can selected durable rows survive a volatile database rebuild without accepting malformed state or partial restoration? | `tests/test_sqlite_state.py` |
| Qualified composition | Does a positive-weighted input without material evidence suppress the composite instead of being silently ignored? | `tests/test_ranking.py` |

The modules use only the Python standard library at runtime.

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
[audit questions](docs/audit-questions.md). The source lineage and the changes
made during extraction are recorded in [SOURCE_MAP.md](SOURCE_MAP.md).
Known boundaries are explicit in [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

A coding agent can help search the state space, but its report should be treated
as an argument to reproduce, not as a certification. Ask it for exact paths,
line-specific counterexamples, and tests that fail before a proposed fix.

## Scope

The edition contains synthetic identities, payloads, and SQLite rows only. It
does not contain third-party datasets, client information, credentials,
production configuration, the product interface, or the private repository's
history. The source map identifies the private baseline by commit, tag, and Git
blob IDs; those identifiers are provenance attestations, while this repository's
code and tests stand on their own as the public evidence.

## License

Copyright © 2026 Harrison Wolf.

Licensed under the GNU Affero General Public License, version 3 only
([AGPL-3.0-only](LICENSE)). See [NOTICE.md](NOTICE.md).
