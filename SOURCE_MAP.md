# Source map

## Baseline

This edition was extracted from the private Nexora no-feature hardening
implementation at:

- commit: `f4159991ddeaa3697e17bc820b055aed64a5ac79`
- annotated tag: `nexora-no-feature-hardening-2026-08-18`

The tag identifies the implementation baseline. Later private closeout records
are not part of the extracted code.

Because the source repository remains private, these identifiers are a
maintainer provenance attestation rather than independently fetchable public
evidence. The public edition must be evaluated from its own source, tests,
claims, and history.

## File lineage

| Public module | Private source file(s) and Git blob(s) | Extraction |
| --- | --- | --- |
| `src/nexora_audit/manifest.py` | `src/manifest_integrity.py` — `b5e40ddd31bbace96def83be558f59dc7d567996` | Function-level extraction; product schema reduced to one synthetic artifact. |
| `src/nexora_audit/publication.py` | `src/publish.py` — `d83e0a666e52cfd5103bef22311468df0bfc4cd5` | Publication core generalized to byte mappings and an injected validator. |
| `src/nexora_audit/transition.py` | `src/runtime_transition.py` — `e0588b4b455a68945f3c5e9d13e3ab7c10887811`; `src/bootstrap.py` — `41d592e218dbb976b5e0e5599afcd7409c21fde2` | Journal, state matrix, postchecks, and recovery extracted; product commands and paths removed. |
| `src/nexora_audit/sqlite_state.py` | `src/bootstrap.py` — `41d592e218dbb976b5e0e5599afcd7409c21fde2`; `src/storage.py` — `418edcac2dd8a47bb2635a31e717c3d209f33e8a`; `src/web.py` — `bd3b6e2569ee3831601b6909e26ed674588aecac` | Two durable table shapes retained in a minimal synthetic runtime database. |
| `src/nexora_audit/ranking.py` | `src/ranking.py` — `2ea7bb3daf02489c4db7b05e200897b23962df10` | Evidence qualification and weighted composition extracted without domain data. |

The public tests are purpose-built for this edition rather than copied as a
product test suite.

## Hardening added during extraction

A separate adversarial pass against the public extraction found cases that were
not safe to leave implicit. Before publication, the edition added:

- physical containment checks for symlinked transition ancestors;
- structural marker validation and a durable rollback phase before recovery;
- strict validation of caller-supplied plans and their still-current prepared
  slot state before the first journal write;
- one root-global, non-blocking POSIX lock around public transition mutations;
- rejection of a symlinked publication staging root;
- explicit synchronization of both parents affected by final publication;
- normalization that keeps a weighted mean finite for huge finite weights;
- exact byte-identity verification for the landed database in addition to its
  semantic publication check;
- literal, contained, all-candidate preflight before orphan-residue deletion;
- strict typed SQLite row validation and all-or-nothing restoration tests.

These changes mean the edition is not a byte-for-byte mirror of the private
baseline. That distinction is intentional and inspectable.
