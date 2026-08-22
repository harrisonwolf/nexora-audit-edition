# Security policy

## Supported version

Security fixes are considered for the current `0.1.x` line.

## Reporting

Please use GitHub's private security-advisory reporting for this repository.
Do not place credentials, personal data, third-party proprietary data, or a
working exploit against another system in a public issue.

A useful report includes:

- the affected commit and file;
- the violated claim or boundary;
- the smallest synthetic reproduction;
- expected and observed behavior;
- whether the issue can escape a declared filesystem root, expose a partial
  publication, corrupt recovery, or silently upgrade an unsupported result.

## Claim falsification

Safe, synthetic challenges to a statement in `CLAIMS.json` may be submitted
through the repository's claim-falsification issue form. A useful challenge
identifies the claim ID and tested commit, states the relevant environment and
declared limitation, and includes the smallest reproduction showing expected
and observed behavior.

A report becomes a confirmed finding only after the behavior is reproduced and
shown to violate the claim inside its stated boundary. A false positive,
documented limitation, or untested hypothesis remains labeled as such.

With the reporter's consent, a confirmed finding will credit the reporter by
name or chosen handle in the next applicable correction record or changelog.
Anonymous credit is also available. This project does not currently offer a
monetary bug bounty.

Use private security-advisory reporting rather than the public form when a
reproduction contains a security-sensitive technique. Never submit credentials,
personal data, proprietary data, or an exploit against another system.

## Scope

This policy covers the code in this repository. It does not operate a hosted
service, accept production data, or make the edition a security-certified
system. The documented limitations remain in force.
