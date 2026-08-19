# Known limitations

These boundaries are part of the release, not footnotes to it.

## General

- This is a narrow audit artifact, not the Nexora product and not a deployment
  template.
- Passing tests establish behavior for the exercised cases. They do not prove
  correctness, security, availability, or regulatory compliance.
- No cryptographic signatures or external trust anchors are implemented.
- The edition assumes a cooperative application process. A privileged or
  malicious process that can rewrite files between checks is outside scope.
- The code is synthetic-data-only. It says nothing about the legality, quality,
  licensing, or fitness of any real dataset.

## Filesystem and process model

- Transition locking uses `fcntl.flock`; Windows is unsupported.
- The lock is shared by every scope under one marker parent. That parent must
  be treated as the exclusive transition ownership root; separately configured
  roots must not overlap or nest.
- Rename and directory-sync claims assume a local POSIX filesystem. Network,
  overlay, FUSE, and unusual storage stacks can have different guarantees.
- Some platforms reject directory `fsync`; the helper treats a documented set
  of “unsupported” errors as unavailable durability rather than portability
  failure.
- The publication API can make a final directory visible and then report
  `PublicationDurabilityError` if a parent sync fails. Callers must treat that
  as an indeterminate durability outcome, not retry blindly under a new ID.
- Symlink ancestors are rejected at declared roots, but the code does not use
  `openat2` or directory file descriptors to eliminate every same-user
  time-of-check/time-of-use race.
- The transition tests inject exceptions around each rename and selected marker
  operations. They do not emulate every kernel panic, torn write, disk-cache
  behavior, power loss, or device failure.

## Data and decision model

- The SQLite schema retains only synthetic token and saved-report rows. It is
  not a general schema migration or backup system.
- Timestamp validation checks parseability and an explicit UTC offset; it does
  not impose a canonical textual representation.
- The ranking thresholds, coverage fields, score range, and neutral zero-weight
  result are illustrative policy choices. They are not empirical validation of
  a real scoring model.
- “Material evidence” here means the explicit structural predicates in
  `ranking.py`; it is not a general theory of evidentiary sufficiency.
- The transition extraction models a bounded, three-target generation. Adding
  target kinds changes the state matrix and requires new proof obligations and
  interruption tests.

## Provenance

The private source identifiers in `SOURCE_MAP.md` cannot be independently
resolved from this public repository. They document lineage; they do not add
warrant to the public code. Reviewers should base public conclusions on what is
present here.
