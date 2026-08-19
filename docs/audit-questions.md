# Audit questions

Use these questions for human review, static analysis, fault injection, or a
coding-agent audit. Require exact paths and a reproducible counterexample for
every finding.

## Manifest integrity

1. Can the declared payload escape the bundle lexically or through a symlink?
2. Can a symlink, directory, device, pipe, or other non-regular file be
   accepted as manifest or payload?
3. Are the consumed bytes exactly the bytes whose size and digest were checked?
4. Do both the initial file-size check and the read loop enforce the declared
   memory ceiling? What happens at the exact limit or if the file grows?
5. Can malformed schema, identity, version, size, or digest values be coerced
   into acceptance?
6. What time-of-check/time-of-use races remain outside the stated model?

## Atomic publication

1. Can any discovery path observe a partially written tree?
2. Is the manifest written only after every other staged payload?
3. Are newly created root links and every staged file and directory synchronized
   before validation? Does retry re-synchronize a partially created root?
4. Does validation bind the path set, entry type, permission mode, hardlink
   status, and regular-file bytes that will be published?
5. If a validator rewrites identical bytes, are those writes synchronized again
   before visibility?
6. Can a pre-existing file, nonempty directory, empty directory, or symlink at
   the final identity be replaced or modified?
7. Can interruption before the final rename leave a publisher-created final
   tombstone?
8. Can two cooperative publishers of the same identity both report success?
9. Are both directories changed by the final rename synchronized?
10. Are unrelated rename failures distinguished from collisions?
11. What does the caller learn if visibility succeeds but durability reporting
    fails?
12. Which metadata and non-cooperating path races remain outside the model?

## Journaled transition

1. Are newly created ownership-root links synchronized through the full
   ancestry, including after a failed partial creation attempt?
2. For every durable phase, what exact live/stage/backup states are allowed?
3. Can a forged in-memory plan or marker cause deletion or mutation before the
   whole record and exact prepared slot state are validated?
4. Must the marker path be canonical and absolute?
5. Can any path, scope name, symlink, special file, alias, control name, or
   ancestor/descendant relationship escape or corrupt the ownership model?
6. Are incumbent and staged regular-file contents and containing directories
   synchronized before `prepared`? Are identities recomputed afterward?
7. Can a synchronization failure or post-sync mutation leave a marker or move
   a byte?
8. After each target rename, is the changed parent synchronized before the
   corresponding completed phase is published?
9. Are concurrent transition, recovery, marker-write, and orphan-sweep
   mutations excluded across processes and scope names under one root? Are
   independently configured roots guaranteed not to overlap?
10. Does every tested precommit interruption recover old state, and every tested
    postcommit interruption preserve verified new state?
11. Can recovery itself be interrupted and resumed?
12. Does marker publication always leave either the previous or next complete
    record?
13. Do runtime database checks establish both byte identity and semantic
    publication identity?
14. Do build checks require the exact planned tree, every required entry,
    object-shaped metadata, and the expected publication identity?
15. Are orphan names matched literally, and are all deletion candidates
    validated before the first removal?

## SQLite carry-over

1. Which rows and fields are durable?
2. Does malformed input in any section roll back every section?
3. Are legacy omissions explicit rather than silently invented?
4. Are identity collisions rejected?
5. Are complete row shape, scalar types, timestamps, quotas, and JSON object
   shape validated before insertion?
6. Are non-standard `NaN` and infinity tokens rejected without narrowing the
   standard JSON number grammar?
7. Are connections closed and transactions rolled back on exceptional paths?
8. What closed-database and WAL/SHM preconditions are outside this module?

## Qualified composition

1. Can a positively weighted unsupported dimension disappear from the result?
2. Do evidence keys, weights, results, and coverage actually align?
3. Can NaN, infinity, booleans, negative weights, or huge finite values corrupt
   the calculation?
4. Is the zero-weight policy explicit?
5. Which policy choices require empirical validation before real use?

## Reporting standard

Classify each result as a reproduced defect, untested hypothesis, documented
limitation, or false positive. Passing a review is not a certification. A
useful review leaves behind a smaller failing test, a sharper boundary, or both.
