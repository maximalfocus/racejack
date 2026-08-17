# Contributing

Contributions are welcome. This is a teaching project, so the bar for a change is "does it make the
mechanism clearer, or the evidence stronger" rather than "does it add a feature".

## The one thing you need

Docker. No PostgreSQL, no Python environment, no host tuning — everything runs inside containers.

## The one command that has to pass

```sh
bash scripts/verify.sh
```

This is the complete verification boundary: images, both applications, the sequential demonstration
at two replicas and at one, the audit gate, the containment checks, the concurrent load harness, the
vulnerable ladder in both reproduction modes, the full comparison, then Ruff, mypy, and the test
suite. GitHub Actions runs exactly this script, so a green local run and a green CI run mean the same
thing. Please make sure it passes before opening a pull request.

## Hard constraints

These are not style preferences. A change that breaks one of them cannot be merged.

**Everything is fictional.** No real store, product, buyer, promotional code, wallet balance,
endpoint, organization, credential, or personal datum — not in code, not in tests, not in
documentation, not in a transcript. Fixtures must be conspicuously invented.

**The vulnerable code stays opt-in.** It must remain unreachable from the default Compose path and
must refuse to start without *both* the `vulnerable` profile and `ALLOW_VULNERABLE_DEMO=true`. It
must keep labelling itself as intentionally broken.

**The secure application stays clean.** No delay, no instrumentation import, and no unguarded
read-then-write in `src/racejack/secure/`. `tests/test_scope.py` enforces this, because a secure path
that drifted would make every comparison in the project meaningless.

**No performance claims.** The harness generates concurrency to expose a race; it is not a
load-testing tool. No throughput, latency, or "faster/slower" claim about the software belongs
anywhere in the output or the documentation. There is a test for this.

**Nothing gets deployed.** No hosting, no published package or image, no cloud configuration, no
egress from the demo network.

**Don't claim the project lacks something it ships.** Stale sentences of that shape have shipped here
before, so `tests/test_acceptance_repairs.py` now watches for them.

## Practical notes

- Assertions should read the structured result — the `Comparison`, `Report`, or ledger object —
  rather than scraping printed output. Output is for humans; structures are for tests.
- A secure-side violation is a genuine failure, never a flake to retry away.
- A natural-mode run that observes nothing is `inconclusive`, never a pass. Only the deterministic
  mode carries a required vulnerable-side assertion.
- Ruff and mypy run in strict mode. Match the surrounding prose-heavy docstring style; the
  explanations are part of the deliverable.

## Reporting a security problem

An unintended weakness goes through the private path in [`SECURITY.md`](SECURITY.md), not a public
issue. The demonstrated race condition is the subject of the project and is not a bug — that file
draws the line.

## License

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE) that
covers this project.
