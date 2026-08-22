# Evidence release record: 0.1.2

Date: 2026-08-22

## Purpose

Version 0.1.2 extends the public evidence and review surface of the Audit
Edition. It does not alter the five runtime mechanism implementations from
v0.1.1; the only importable-source change is the module version marker. The
v0.1.1 corrective record remains a point-in-time account of that release.

This is an evidence release, not a certification.

## Release delta

- link the exact public 0.1.0 snapshot and its complete release-level
  comparison with v0.1.1;
- state the limits of the public release history and disclose the maintainer
  and coding-agent implementation roles;
- add a structured claim-falsification issue form, reproduction standard, and
  public or anonymous credit policy for confirmed findings;
- test four real processes contending for one publication identity after
  validation, requiring one winner, typed collisions for every loser, and
  final bytes matching the winner;
- kill real child processes immediately before and after the final publication
  rename, then verify final-name availability or incumbent preservation;
- kill real transition processes at one representative precommit durable phase
  and at the commit phase, then require conservative, idempotent recovery and
  kernel release of the process-held lock;
- reconcile the machine-readable claims, limitations, audit questions, and
  public-tree allowlist with that evidence.

## Complementary interruption evidence

The existing deterministic exception-injection matrices exercise dirty windows
around every modeled target rename and marker-publication point, selected
synchronization failures, and postcommit cleanup. The SIGKILL tests exercise
actual process disappearance at selected stable boundaries and, for the
transition, release of a lock held by the dead process.

These methods answer different questions and are intentionally complementary.
No randomized-delay test is promoted as evidence. Neither method emulates
kernel panic, torn writes, lost device caches, power loss, distributed
filesystems, or a hostile process.

The publication SIGKILL cases establish final-name visibility, retry, and
collision behavior. They are not power-loss durability tests.

## Deferred work

The following remain possible evidence extensions rather than release
requirements:

- deterministic real process death after a target rename but before its
  completed-phase marker;
- an opt-in, per-test replay harness for the 0.1.0-to-0.1.1 correction;
- optional property-based tests outside the standard-library-only default
  verification path.

The private Nexora product does not import this AGPL-3.0-only package. Private
reverse-ports remain separate changes with their own history and review.

## Release gate

Version 0.1.2 is eligible for merge and tag only after:

- `make verify` passes from the staged public tree;
- an isolated source distribution and wheel build, wheel installation, and
  imported version check pass;
- the package, module, claim scope, and identity test all report `0.1.2`;
- the protected GitHub pull request passes the Python 3.11, 3.12, and 3.13
  verification matrix;
- final review reports no unresolved blocker or major finding.
