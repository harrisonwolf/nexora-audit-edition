# Architecture

The edition isolates five mechanisms that can be reasoned about separately.
Their composition is deliberately not presented as a full application.

## 1. Manifest-bound read

`verify_and_read` validates a manifest's schema, identity, producer version,
contained payload path, byte count, and SHA-256 digest. It opens the payload as
a non-symlink regular file, reads it once, hashes those bytes, and returns those
same bytes. The consumer therefore does not verify one read and consume a
second.

## 2. Atomic publication

`publish_atomically` writes ordinary files to a private, uniquely named
staging directory and writes `manifest.json` last. It synchronizes the staged
tree, invokes a caller-supplied validator, and exposes the tree through one
same-filesystem directory rename. A pre-existing final identity is never
replaced.

Discoverability is narrower than existence: `discover_publications` admits
only non-symlink final directories with a regular manifest and ignores the
staging namespace.

## 3. Journaled transition

The transition module manages a bounded generation change across three target
slots. Its marker records:

- exact live, staged, and backup paths;
- expected old and new identities;
- the transition token and ordered target names;
- a durable phase.

Before mutation, the caller-supplied record is parsed through the same strict
validator as a recovered marker, must begin at `prepared`, and must still
match every live, staged, and backup identity. Every public mutator takes one
non-blocking process lock shared by all scopes under the owned root; any
pending root marker blocks a new transition. Each rename is followed by a
durable phase update. Recovery validates that the complete observed slot
state belongs to an allowed precommit, rollback, or postcommit state before it
changes anything.

The commit point is successful verification of the complete new generation.
The landed database must match the planned byte identity as well as reopen
cleanly with the expected publication identity.
Before it, recovery prefers the old generation. After it, recovery preserves
the verified new generation and removes residue.

## 4. Durable SQLite carry-over

The sample database separates a volatile publication identity from two durable
row sets. Rebuild reads the selected rows without altering the old database,
creates a new database, validates complete typed rows, and restores them within
one transaction. Any malformed section or uniqueness failure rolls back the
whole restoration.

## 5. Qualified composition

`qualified_weighted_mean` separates the possibility of a numeric score from
the warrant to include it. A positively weighted dimension without a score or
material-evidence flag suppresses the composite and names the unsupported
dimension. Domain-shaped evidence helpers additionally require coverage and
key alignment before marking sample school or safety evidence material.

Huge finite weights are scaled by their maximum before summation, so finite
inputs do not overflow the composite arithmetic.

## Trust boundary

The mechanisms constrain claims inside their declared local models. They do
not supply a product-level threat model, a distributed transaction, a
signature authority, empirical domain validation, or a human authorization
decision. See `KNOWN_LIMITATIONS.md`.

The marker parent is an ownership boundary. Two roots must not overlap or nest;
the root-global lock cannot coordinate independently configured roots.
