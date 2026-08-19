# Architecture

The edition isolates five mechanisms that can be reasoned about separately.
Their composition is deliberately not presented as a full application.

## 1. Manifest-bound read

`verify_and_read` validates a manifest's schema, identity, producer version,
contained payload path, byte count, and SHA-256 digest. It opens the manifest
and payload as non-symlink regular files, reads each once under an explicit byte
ceiling, hashes the returned payload bytes, and returns those same bytes. The
consumer therefore does not verify one read and consume a second.

The manifest ceiling is 1 MiB. The payload ceiling defaults to 64 MiB and is a
positive finite caller parameter. The read loop enforces the ceiling itself, so
growth after the initial file-size observation is rejected.

## 2. Atomic publication

`publish_atomically` synchronizes the full ancestry of any newly created
publication-root directories, writes ordinary files to a private, uniquely
named staging directory, and writes `manifest.json` last. Every regular file
and staged directory is synchronized before validation.

The implementation seals the staged path set, entry types, permission modes,
hardlink status, and regular-file bytes across the validator callback. A changed,
added, removed, linked, or special entry is rejected. Because a validator can
rewrite identical bytes, the unchanged snapshot is synchronized again and
rechecked before publication.

The final path must be absent. A directory, file, or symlink already occupying
that identity—including an empty directory—is a collision and is preserved.
Once validation succeeds, one same-filesystem directory rename exposes the
staged tree. The staging parent and publication root are then synchronized. The
implementation never precreates an empty final-name reservation, so an
interruption before the rename cannot leave a publisher-created tombstone.

Discoverability is narrower than existence: `discover_publications` admits
only non-symlink final directories with a regular manifest and ignores the
staging namespace. Stale staging residue is evidence for explicit maintenance;
it is not silently promoted or deleted.

## 3. Journaled transition

The transition module manages two bounded generation shapes: a three-target
runtime (`db`, `thumbs`, `photos`) and a one-target web build.

### Topology and ownership

One canonical absolute marker path defines an owned root. The marker, its
temporary marker names, and the root-global lock are a reserved control
namespace and cannot be data slots. Every live/stage/backup triple consists of
distinct siblings. All slots across the record are pairwise
ancestry-disjoint, physically contained beneath the root, and free of symlinked
ancestors. Targets and their descendants must be regular files or directories.

Every public transition, recovery, marker-write, and residue-sweep mutation
takes one non-blocking `fcntl.flock` shared by every scope under that root.
Any visible transition marker blocks a new transition.

Missing owned-root directories are created with full-ancestry synchronization
before the lock is opened. If an ancestry sync fails after creating a
directory, a retry re-synchronizes the complete chain rather than assuming that
an existing directory is already durable.

### Durable entry

Before the first journal write, the caller-supplied record is copied through
JSON, parsed by the same strict validator used for recovery, required to begin
at `prepared`, and checked against the exact live/stage/backup identities.

The incumbent and staged targets must already be closed and self-contained.
While holding the root lock, the implementation synchronizes every regular file,
every nested directory, and the containing directory chain through the owned
root. It then recomputes all recorded identities. A synchronization failure or
identity change leaves no marker and performs no rename.

### Phases, postchecks, and recovery

Every target move follows this order:

1. publish and synchronize the intent marker;
2. rename one target;
3. synchronize each changed parent directory;
4. publish the completed phase.

The complete new generation is the sole commit point. Runtime targets must
match their planned byte identities; the database must also reopen with exactly
one expected publication row. A build must match its planned tree identity,
contain every required entry, and carry one object-shaped `build.json` naming
the expected publication.

Before the commit point, recovery prefers the complete old generation. After
it, recovery preserves the verified new generation and removes residue.
Recovery first proves that the entire observed slot state belongs to an allowed
precommit, rollback, or postcommit state. Unknown states fail closed and retain
the marker and bytes for manual inspection.

## 4. Durable SQLite carry-over

The sample database separates a volatile publication identity from two durable
row sets. Rebuild reads selected rows without altering the old database,
creates a new database, validates complete typed rows, and restores all sections
within one transaction. Malformed fields, duplicate identities, non-object
report payloads, and Python's non-standard JSON constants are rejected before
commit. Any failure rolls back the whole restoration.

## 5. Qualified composition

`qualified_weighted_mean` separates the possibility of a numeric score from
the warrant to include it. A positively weighted dimension without a score or
material-evidence flag suppresses the composite and names the unsupported
dimension. Domain-shaped evidence helpers additionally require coverage and
key alignment before marking sample school or safety evidence material.

Huge finite weights are scaled by their maximum before summation, so finite
inputs do not overflow the composite arithmetic.

## Trust boundary

These mechanisms constrain claims inside declared local models. They do not
supply a product-level threat model, distributed transaction, signature
authority, empirical domain validation, compliance determination, or human
authorization decision. The marker parent is one ownership boundary; separately
configured roots must not overlap or nest. See [KNOWN_LIMITATIONS.md](../KNOWN_LIMITATIONS.md).
