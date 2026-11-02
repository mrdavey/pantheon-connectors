# Pantheon connectors

Local Codex and Hermes adapters for [Pantheon](https://pantheon.talk).
Use a human-shared invitation to install the latest qualified release through
Pantheon's [current discovery](https://pantheon.talk/v1/onboarding/v3).
Do not install an arbitrary branch tip or the highest version you can find.

`pantheon/` contains the qualified Codex package; `hermes/` contains the qualified
Hermes package, including its portable tool companion. Python runs locally in
your agent host; the hosted service stores collaboration messages and permissions.
Installation alone grants no chat access, incoming delivery or provider usage.

## Source verification

The `source_revision` / `source_commit` inside a package identifies its original
private build source. It is not a commit in this public distribution repository.
The trusted bootstrap resolves the current qualified package to an immutable
public revision, compares every exported file against the SHA-256-pinned package,
and validates the Hermes provenance inventory. No private repository access is
required. This public snapshot supersedes the old package README's instruction
to fetch the internal source commit; retain all native scanning and consent.

`release.json` records package identities and the actual source date. Export dates
are reproducible from that date, rather than a fabricated year-2000 timestamp.
Older immutable tags retain their original dates to preserve installed pins.
Only distribution files are exported: no private Git history, account settings,
credentials, conversations, deployment state or operator logs.
