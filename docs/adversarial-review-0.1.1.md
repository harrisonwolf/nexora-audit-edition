# Adversarial review record: 0.1.1

Date: 2026-08-19

## Purpose

After the initial 0.1.0 publication, the public extraction was treated as an
object to attack rather than a finished proof. External model reports and
independent agent review were handled as untrusted hypotheses. A new 0.1.1
finding entered this record only after a concrete reproduction or a demonstrated
mismatch between the 0.1.0 code and its published contract.

This is a correction record, not a certification.

## Public release snapshots

The exact initial 0.1.0 state is public at
[`99935c2`](https://github.com/harrisonwolf/nexora-audit-edition/commit/99935c2).
The corrective release is tagged
[`v0.1.1`](https://github.com/harrisonwolf/nexora-audit-edition/releases/tag/v0.1.1),
and the complete release-level delta is the
[0.1.0-to-v0.1.1 comparison](https://github.com/harrisonwolf/nexora-audit-edition/compare/99935c2...v0.1.1).
The public history preserves these release snapshots, not every intermediate
fail-before and repair commit; this method record does not substitute for those
omitted intermediate states.

## Method

For each accepted new finding:

1. preserve the smallest counterexample;
2. add a regression that fails against the affected implementation;
3. implement the narrowest root-cause repair;
4. rerun the focused mechanism suite;
5. reconcile `CLAIMS.json`, architecture, limitations, audit questions, and
   provenance;
6. require the complete repository and installed-wheel gates before release.

Controls already present in 0.1.0 were reverified but are separated below from
new 0.1.1 changes.

## Reproduced 0.1.1 findings and disposition

| Boundary | Reproduced failure | 0.1.1 disposition |
| --- | --- | --- |
| Manifest memory and error boundary | Manifest and payload reads had no explicit API ceiling, and an injected low-level read error escaped the typed integrity exception. | Added a 1 MiB manifest cap, configurable positive payload cap (64 MiB default), exact-limit and growth guards, and typed I/O translation. |
| Final publication | The reservation design could leave an empty final-name tombstone; an intermediate repair then treated any empty directory as reclaimable without provenance. | Removed final-name reservation. Rename occurs only after an absent-name preflight; every final path already present there—including an empty directory or symlink—is preserved. Collision translation now requires observed final-name occupancy. |
| Validator snapshot | A validator could successfully inspect the staged files, then change them before returning; the changed bytes were published without another sync. | Seal paths, entry types, modes, hardlink status, and bytes across the callback; reject drift, re-synchronize the unchanged snapshot, and recheck before rename. |
| Root creation durability | A new ownership/publication root could be created without synchronizing the link from its parent; after a failed sync, retry treated the leftover directory as already durable. | Synchronize the full ancestry for every call, including existing directories left by a prior failed attempt, before staging or lock acquisition. |
| Transition topology | Canonical marker spelling, reserved marker/lock namespaces, cross-target aliases, and ancestor/descendant slots were not all rejected centrally. | Validate canonical marker paths before target hashing; reserve control names; require distinct sibling triples and pairwise ancestry-disjoint data slots. |
| Input file type | A special entry inside a target tree could reach the digest reader instead of receiving a controlled rejection. | Identity and synchronization walks now accept only regular files and directories and reject symlinks or special entries. |
| Input durability | Journal metadata could become durable while staged or incumbent file contents remained only page-cache-visible. | Under the root lock, synchronize every closed regular file, nested directory, and containing ancestor through the owned root before `prepared`; then recompute every identity. Failure leaves no marker or rename. |
| Transition phase ordering | A target rename could be followed by a completed phase before the changed parent directory was synchronized. | Synchronize every affected target parent inside the rename primitive before publishing the completed phase. |
| Build metadata | A non-object `build.json` produced an uncontrolled attribute failure rather than a typed postcheck rejection. | Require an object-shaped build marker; add dedicated missing-entry, malformed-JSON, mismatched-ID, every-rename, and postcommit-cleanup cases around the existing tree-identity and publication checks. |
| Saved-report JSON | Python's JSON decoder accepted non-standard `NaN` and infinity constants at the durable-row boundary. | Reject non-standard constants, parse standard numeric literals without binary-float overflow, and preserve all-or-nothing transactional restoration. |
| Build reproducibility | The isolated build backend and wheel dependency floated above minimum or unbounded versions. | Pin exact backend versions; retain the exact `build` frontend pin in CI and reproduce the installed-wheel gate before release. |
| Release identity and public contract | Package metadata, module version, claims, architecture, limitations, and source lineage did not describe the corrective delta. | Assign one 0.1.1 identity and bind each promoted statement to exact tests and explicit limitations. |

## Controls retained and reverified from 0.1.0

The review also reran controls already present in the initial release. They are
not attributed to 0.1.1:

- strict caller-record validation, fresh prepared-state checking, and
  initial-marker non-clobbering;
- physical containment across symlinked ancestors;
- one root-global, non-blocking process lock;
- durable rollback and exact whole-state recovery legality;
- exact landed database bytes plus semantic publication identity;
- literal, contained, all-candidate orphan cleanup;
- staged publication synchronization and typed visible-but-not-durable outcome;
- strict typed, transactional SQLite restoration;
- finite huge-weight normalization in qualified composition.

Their extraction-era provenance remains in
[../SOURCE_MAP.md](../SOURCE_MAP.md).

## Executable anchors

The machine-readable selectors are in [../CLAIMS.json](../CLAIMS.json). The
highest-leverage regressions added or materially expanded for 0.1.1 include:

- exact manifest/payload limits, growth after size observation, and typed read
  failures;
- empty-directory and symlink incumbent preservation;
- final-rename interruption without a publisher-created tombstone;
- validator path/type/mode/hardlink/byte sealing plus identical-byte
  post-validator re-synchronization;
- publication and transition root-link synchronization plus retry after a
  partial ancestry-sync failure;
- nested file and directory synchronization before `prepared`, injected sync
  failure, and identity drift after synchronization;
- canonical marker, control-namespace, special-file, and ancestry-overlap
  rejection;
- parent synchronization before completed transition phases;
- every build rename, malformed build metadata, and postcommit cleanup;
- a 30-point marker-publication interruption matrix;
- standards-valid JSON enforcement with transactional rollback;
- one tested 0.1.1 identity across public metadata and claims.

## Residual boundaries

The review did not convert local mechanisms into distributed or hostile-host
guarantees. The remaining process, filesystem, resource, SQLite sidecar, race,
and policy boundaries are stated in [../KNOWN_LIMITATIONS.md](../KNOWN_LIMITATIONS.md).
The model remains cooperative-process, local-filesystem, and finite-test
bounded.

## Release criterion

Version 0.1.1 is eligible for merge and tag only after:

- `make verify` passes from the staged public tree;
- the package builds and installs in a fresh environment;
- the package, module, and claim scope all report `0.1.1`;
- the public-tree verifier passes;
- the protected GitHub pull request passes the Python 3.11, 3.12, and 3.13
  verification matrix;
- a final independent review reports no unresolved blocker or major finding.
