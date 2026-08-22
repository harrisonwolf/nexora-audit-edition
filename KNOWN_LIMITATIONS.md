# Known limitations

These boundaries are part of the release, not footnotes to it.

## General

- This is a narrow audit artifact, not the Nexora product or a deployment
  template.
- Passing tests establish behavior for exercised cases. They do not prove
  correctness, security, availability, suitability, or regulatory compliance.
- No cryptographic signatures or external trust anchors are implemented.
- The edition assumes a cooperative application process. A privileged or
  malicious process that can rewrite files or process state is outside scope.
- The code is synthetic-data-only. It establishes nothing about the legality,
  licensing, quality, or fitness of any real dataset.

## Filesystem and process model

- The mechanisms require a case-sensitive local POSIX filesystem and
  same-filesystem directory renames. Windows is unsupported. Network, overlay,
  FUSE, and unusual storage stacks may provide different semantics.
- Transition locking uses `fcntl.flock`. One marker parent is the exclusive
  transition ownership root for every scope beneath it. Separately configured
  roots must not overlap or nest.
- Declared transition targets must be closed, immutable for the operation, and
  self-contained before entry. In particular, an open SQLite database or a
  database depending on external WAL/SHM sidecars is outside the transition
  contract. Concurrent readers and writers are not coordinated by this module.
- Publication and transition synchronize regular-file contents and directory
  entries at their respective durability boundaries. Missing owned roots are
  created by synchronizing the complete ancestry, and a retry re-synchronizes
  ancestry left by a prior failed attempt. Some filesystems reject directory
  `fsync`; a documented set of unsupported errors is treated as unavailable
  directory durability rather than a portability failure. Power-loss durability
  is therefore not claimed on such filesystems.
- Symlink ancestors and nested symlinks are rejected, as are special files in
  declared targets. The code does not use `openat2` or anchored directory file
  descriptors, so it does not eliminate every same-user time-of-check/time-of-use
  race.
- Publication checks that the final name is absent before the direct rename.
  A non-cooperating actor can race that check. The tested one-winner guarantee
  concerns publishers using this API under the cooperative-process model.
- The publication validator is mechanically bound to the return-time path set,
  entry types, permission modes, hardlink status, and regular-file bytes.
  Timestamps, ownership, ACLs, and extended attributes are not bound. A callback
  that retains a writer or a non-cooperating process that mutates the tree after
  validation is outside the cooperative-process model.
- Publication can become visible and then raise
  `PublicationDurabilityError` if a parent-directory sync fails. Callers must
  treat that as an indeterminate durability outcome and inspect the final name;
  they must not retry blindly under another identity.
- Incomplete staging directories and transition residue are retained for
  explicit inspection or maintenance. They are not silently treated as valid
  releases or reclaimed final identities.
- The deterministic exception-injection matrices precisely exercise dirty
  windows around every modeled rename and marker-publication point, selected
  synchronization failures, and postcommit cleanup. Separate tests exercise
  actual process disappearance—and, for transitions, kernel release of the
  process-held lock—at selected stable boundaries immediately before and after
  publication visibility and at representative precommit and postcommit
  transition phases. These bodies of evidence are complementary. The selected
  SIGKILL cases do not emulate every kernel panic, torn write, disk-cache
  behavior, power loss, device failure, or administrator action.

## Resource model

- A manifest may occupy at most 1 MiB.
- `verify_and_read` returns the payload in memory. Its default payload ceiling
  is 64 MiB; callers may choose another positive finite limit.
- Transition identity checks hash complete target trees and therefore have time
  and I/O cost proportional to their total regular-file contents.
- No resource quota, deadline, or cancellation policy is supplied for an entire
  transition or publication.

## Data and decision model

- The SQLite schema retains only synthetic token and saved-report rows. It is
  not a general schema migration or backup system.
- Timestamp validation checks parseability and an explicit UTC offset; it does
  not impose a canonical textual representation.
- JSON validation accepts the standard grammar, including numeric magnitudes
  larger than a binary float can represent. It rejects Python's non-standard
  `NaN`, `Infinity`, and `-Infinity` tokens.
- The ranking thresholds, coverage fields, score range, and neutral zero-weight
  result are illustrative policy choices. They are not empirical validation of
  a real scoring model.
- “Material evidence” means the explicit structural predicates in
  `ranking.py`; it is not a general theory of evidentiary sufficiency.
- The transition extraction models two fixed target shapes. Adding targets or
  changing their order changes the state matrix and requires new interruption
  and recovery evidence.

## Provenance

The private source identifiers in [SOURCE_MAP.md](SOURCE_MAP.md) cannot be
independently resolved from this public repository. They document lineage; they
do not add warrant to the public code. Reviewers should base public conclusions
on what is present here.
