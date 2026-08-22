# Forward correction record: 0.1.3

Date: 2026-08-22

## Purpose

The v0.1.2 release record said that the release stated the limits of its public
history. The README linked the 0.1.0 snapshot and aggregate v0.1.1 comparison
but did not state that separate fail-before and repair commits were absent.
That caveat had been appended after v0.1.1 and was correctly removed when the
v0.1.1 record was restored to its immutable tagged bytes, but it was not moved
forward.

Version 0.1.3 corrects that documentation drift without altering the v0.1.1 or
v0.1.2 tags or their records. It also consolidates the private/public
relationship in the source map: the public AGPL package is not a dependency of
private Nexora, and separately reviewed reverse-ports do not establish
automatic or publicly verifiable conformance between the trees.

This is a forward documentation correction, not a mechanism change or a
certification.

## Reproduction and correction

The corrective sequence is preserved as public commits:

1. [66ee417](https://github.com/harrisonwolf/nexora-audit-edition/commit/66ee41796a24e3d907c2c70acb58d6d9bcdc0716)
   added the v0.1.3 identity expectation and a machine-checked disclosure
   boundary. The focused test and all three draft-PR CI jobs failed against the
   v0.1.2 tree as intended.
2. [f673fa9](https://github.com/harrisonwolf/nexora-audit-edition/commit/f673fa9955b8c23c1a7e88394829a06ebd0f46ae)
   added the missing README and source-map disclosures and bound the package,
   module, and claim scope to v0.1.3. The focused tests then passed.
3. This reconciliation commit adds the immutable correction record, release
   link, current issue-form identity, and public-tree requirement.

The red CI run is retained at
[GitHub Actions run 32583552268](https://github.com/harrisonwolf/nexora-audit-edition/actions/runs/32583552268).

## Corrected boundaries

The README now states that public history preserves release snapshots and their
aggregate deltas but not separate fail-before and repair commits for the v0.1.1
correction. The method ordering is therefore documented rather than
independently observable from commit order.

The source map now states that the public package is not a dependency of
private Nexora. Reverse-ports have separate review and history; no automatic or
publicly verifiable conformance between the trees is claimed. This does not
weaken the public artifact's warrant, which rests on its own source, tests,
claims, limitations, and history.

## Scope

- No runtime mechanism implementation or dependency changed.
- The only importable-source change is the module version marker.
- The five machine-readable claim statements, evidence selectors, and
  limitations are unchanged; only their release scope advances to 0.1.3.
- The v0.1.1 adversarial record and v0.1.2 evidence record remain unchanged.
- No retrospective v0.1.0 tag is created. Commit
  [99935c2](https://github.com/harrisonwolf/nexora-audit-edition/commit/99935c2)
  remains the directly linked initial snapshot.

## Corrective release history

Future corrective pull requests should preserve separate fail-before, repair,
and reconciliation commits and enter main through a merge commit. The release
tag belongs on the green merge commit. Squash merging remains appropriate for
non-corrective work when no red-to-green sequence carries evidentiary value.

An intermediate red commit is evidence, not a release. Only the fully reviewed,
green merge commit is eligible for a version tag.

## Release gate

Version 0.1.3 is eligible for merge and tag only after:

- make verify passes from the complete branch;
- an isolated source distribution and wheel build, wheel installation, and
  imported version check pass;
- the package, module, claim scope, issue form, and identity test all report
  0.1.3;
- the protected pull request passes the Python 3.11, 3.12, and 3.13 matrix;
- final review reports no unresolved blocker or major finding;
- the pull request is merged without squashing, and the resulting green merge
  commit is tagged exactly v0.1.3.
