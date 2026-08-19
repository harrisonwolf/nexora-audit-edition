# Audit questions

Use these questions for human review, static analysis, fault injection, or a
coding-agent audit. Require exact paths and a reproducible counterexample for
every finding.

## Manifest integrity

1. Can the declared payload escape the bundle lexically or through a symlink?
2. Can a non-regular file be accepted?
3. Are the consumed bytes exactly the bytes whose size and digest were checked?
4. Can malformed schema, identity, version, size, or digest values be coerced
   into acceptance?
5. What time-of-check/time-of-use races remain outside the stated model?

## Atomic publication

1. Can any discovery path observe a partially written tree?
2. Is the manifest written only after every other staged payload?
3. Can a collision replace or modify the incumbent?
4. Can two publishers of the same identity both report success?
5. Are both directories changed by the final rename synchronized?
6. What does the caller learn if visibility succeeds but durability reporting
   fails?

## Journaled transition

1. For every durable phase, what exact live/staged/backup states are allowed?
2. Can a forged in-memory plan or a forged marker cause deletion or mutation
   before the whole record and exact prepared slot state are validated?
3. Can any path, scope name, symlink, or alias escape the marker root?
4. Are concurrent transition, recovery, and orphan-sweep mutations excluded
   across processes and across different scope names under one root?
   Are independently configured roots guaranteed not to overlap?
5. Does every precommit interruption recover old state, and every postcommit
   interruption preserve verified new state?
6. Can recovery itself be interrupted and safely resumed?
7. Does marker publication always leave either the previous or next complete
   record?

8. Are orphan names matched literally, and are all deletion candidates
   validated before the first removal?
## SQLite carry-over

1. Which rows are durable, and which fields are intentionally retained?
2. Does malformed input roll back every section?
3. Are legacy omissions explicit rather than silently invented?
4. Are identity collisions rejected?
5. Are connections closed and transactions rolled back on exceptional paths?

## Qualified composition

1. Can a positively weighted unsupported dimension disappear from the result?
2. Do evidence keys, weights, results, and coverage actually align?
3. Can NaN, infinity, booleans, negative weights, or huge finite values corrupt
   the calculation?
4. Is the zero-weight policy explicit?
5. Which policy choices would require empirical validation before real use?

## Reporting standard

Classify each result as reproduced defect, untested hypothesis, documented
limitation, or false positive. Passing a review is not a certification; a good
review leaves behind a smaller failing test or a clearer boundary.
