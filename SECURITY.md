# Security policy

This project contains a vulnerability **on purpose**. That makes "is this a security issue?" an
unusually confusing question here, so this file exists to answer it.

## The race condition in this repository is the subject, not a bug

`racejack` demonstrates **check-then-act races** — CWE-367 (time-of-check to time-of-use) and its
parent CWE-362 — by shipping code that has them. The following are all deliberate, documented, and
under test:

- the unguarded counter shape, which oversells a twelve-unit drop to sixty concurrent buyers and
  drives the remaining count negative;
- the unguarded single-use shape, which credits one promotional code forty times;
- the process-scoped-lock shape, which holds the invariant at one replica and breaks at two;
- the single-transaction shape, which still loses updates at `READ COMMITTED`;
- the instrumented synchronization point that widens the window in deterministic mode; and
- the two backstop database constraints being dropped for vulnerable runs, so the overrun is
  *observable* instead of surfacing as a constraint violation.

Every one of these lives behind two deliberate opt-in actions — the `vulnerable` Compose profile and
`ALLOW_VULNERABLE_DEMO=true` — and is explained in [`WALKTHROUGH.md`](WALKTHROUGH.md). **Please do not
report any of them.** They are the demonstration. A report that the vulnerable application is
vulnerable is the project working as designed.

## What *is* worth reporting

An **unintended** weakness — one that is not part of the lesson. For example:

- a flaw in the secure application's guards, or in the harness, comparison, or test code;
- a container or Compose misconfiguration that widens the blast radius beyond the demo's own
  services — an unintended published port, a lost capability drop, a way out of the egress-less
  network;
- a way for the vulnerable application to start without **both** opt-in actions;
- a real credential, personal datum, or non-fictional identifier anywhere in the repository, its
  history, or a run artifact; or
- a supply-chain problem in the pinned dependencies or base images.

### How to report

Use **GitHub private vulnerability reporting** on this repository:

> **Security** tab → **Report a vulnerability**

That opens a private advisory visible only to the maintainer. Please do not open a public issue for
an unintended weakness, and please include the commit you observed it on plus the smallest
reproduction you have.

There is no security contact email; the private advisory is the reporting path.

## No supported versions, and nothing to report against

There is no supported-version table here, because there are no versions to support.

This project is **local educational material**. It is not deployed, not hosted, and not published as
a package or container image. There is no running system, no endpoint, and no user data anywhere —
so there is nothing to compromise operationally, and reports about a hosted `racejack` are
necessarily about something that is not this project.

The project makes **no** service-level, support-duration, compatibility, or production-readiness
promise. Fixes are made on a best-effort basis on the default branch. Nothing here is hardened for
any purpose beyond the demonstration, and none of it should be deployed or exposed to a network you
do not control.

## Scope note

Reports are welcome about this repository's own code, tests, tooling, and container configuration.
They are out of scope for any third-party system: this project identifies, contacts, and tests
nothing but its own containers, and all of its stores, buyers, products, promotional codes, wallet
balances, and credentials are invented.
