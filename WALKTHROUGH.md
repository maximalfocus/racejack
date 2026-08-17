# racejack — a walkthrough

> **Everything here is fictional and local.** The storefront, the buyers, the products, the
> promotional code, the wallet balances and the credentials are all invented. The vulnerable
> application in this repository is **deliberately broken educational material and must never be
> deployed** or exposed to a network you do not control.

This is the long version. If you want to see it run first, `bash scripts/demo.sh` and then the
[README](README.md).

---

## 1. The mechanism

A great deal of ordinary code has this shape:

```python
if there_is_room():      # the check
    take_one()           # the act
```

It reads correctly, it reviews correctly, and it is wrong — because those are two separate steps.

The moment `there_is_room()` returns, its answer becomes **a claim about the past**. Nothing holds
it true. Between the check and the act there is an *interval*, and any other execution sharing that
state can occupy it: read the same value, reach the same conclusion, and act on it too.

That is a **check-then-act race**, also called **time-of-check to time-of-use** (TOCTOU), and its
consequences go by names like **lost update** and **double-spend**.

It needs exactly two ingredients:

1. **shared mutable state** — something more than one execution can write; and
2. **concurrent access** — more than one execution actually in flight at the same time.

Take either away and the bug is not merely unlikely; it is impossible. That is why removing the
*second* one — running the code sequentially — makes it disappear completely, and why doing so
proves nothing at all. More on that in §5.

## 2. What this project models

A fictional store, **Kestrel Supply**, that sells limited production runs. Two invariants carry the
demonstration, because check-then-act breaks both kinds and the two need *different* fixes:

| Invariant | The question it answers | Fixture | What a violation looks like |
|---|---|---|---|
| `units_sold ≤ units_available` | "how many are left?" | drop `DROP-2026-03`, 12 units | 60 confirmed orders, `units_remaining` = **−48** |
| each credit code redeems exactly once | "has this already happened?" | `KESTREL-WELCOME-2500`, worth 2500 cents | one code redeemed **40** times, wallet credited **100 000** |

The application runs as **two replicas** over **one** database. That is not a deployment detail; it
is part of the mechanism, and §4.3 is the reason why.

## 3. Terminology, and one honest caveat

The precise identifiers for this defect are:

- **CWE-367** — *Time-of-check Time-of-use (TOCTOU) Race Condition*
- **CWE-362** — *Concurrent Execution using Shared Resource with Improper Synchronization* (parent)

The closest OWASP Top-10 anchor is **A04:2021 – Insecure Design**, and that is an **anchoring
judgement about the nature of the defect, not a published mapping**. It is stated that way
deliberately, and the distinction matters:

> **CWE-367 does not appear in the published A04:2021 CWE list.** A04 is named here because this is
> a *design* failure — the design never made the check and the act atomic, so no amount of correct
> implementation of the individual steps can hold the invariant — which places it alongside A04's
> business-logic family, **CWE-840** (business logic errors) and **CWE-841** (improper enforcement of
> behavioral workflow). If you reject that anchoring, drop the A04 claim and keep the CWE
> identifiers; nothing else in this project depends on it.

### A different CWE-367 in the same series

`dnsrebindjack` is also a CWE-367 demonstration, and it is **not the same mechanism**. There, the
check and the use are separated by *DNS resolution*: a hostname is validated against an allowlist,
and by the time the request is actually made the name resolves somewhere else. Same identifier, same
"the fact you checked was already stale" shape — but the window is opened by a **resolution** step
rather than by concurrency, and neither of the fixes below applies to it.

If you are looking for the concurrency race, you are in the right repository. If you are looking for
the resolution race, that one is `dnsrebindjack`.

## 4. The ladder: four shapes

Run `docker compose run --rm compare` to see all of these side by side.

### 4.1 Unguarded counter

```python
drop = SELECT units_available, units_sold FROM drops WHERE drop_id = ...   # time of check
if drop.units_sold < drop.units_available:
    #                                                                       ← the window
    UPDATE drops SET units_sold = units_sold + 1 WHERE drop_id = ...        # time of use
    INSERT INTO orders ...
```

Sixty concurrent buyers, twelve units:

```
units available     12   units sold     60   units remaining    -48
orders confirmed    60   overrun        48   shortfall       0
```

Sixty people hold confirmed orders for a twelve-unit drop, and the store now believes it owes
forty-eight units it never had.

### 4.2 Unguarded single-use invariant

Same shape, different question:

```python
already = SELECT count(*) FROM redemptions WHERE code = ...   # time of check
if already == 0:
    #                                                          ← the window
    UPDATE wallets SET balance_cents = balance_cents + ...     # time of use
    INSERT INTO redemptions ...
```

Forty concurrent redemptions of **one** code: forty redemptions, and a wallet holding 100 000 cents
of a 2500-cent code.

### 4.3 Half-fix — the process-scoped lock

The obvious response is to add a lock:

```python
async with LOCK:                    # an ordinary asyncio.Lock, used correctly
    ... the same check-then-act ...
```

Addressed at **one** replica:

```
units sold     12   units remaining      0   overrun         0
```

Fixed. Perfectly, convincingly fixed. Ship it.

Addressed at **two** replicas — same code, same fixtures, same burst, nothing else changed:

```
units sold     13   units remaining     -1   overrun         1
```

The second process has its **own** lock, and neither knows about the other. The invariant lives in
the database; the lock lives in a process.

> **A lock protects an invariant only if its scope contains every writer of that invariant.**

This is the most dangerous rung on the ladder, because it is the one that passes review, passes
staging, and fails the first time someone scales to two workers — or adds a second machine, or
enables a second worker process in the application server's config.

### 4.4 Half-fix — the single transaction

The second obvious response is to add a transaction:

```python
async with conn.transaction():      # BEGIN ... COMMIT, at the default isolation level
    ... the same read-check-write ...
```

```
units sold     60   units remaining    -48   overrun        48
isolation in effect  read committed
```

Completely unchanged. A transaction buys you **atomicity** — all of it happens or none of it does —
and **durability**. The failure here is neither of those. It is a **lost update**, and preventing a
lost update is an **isolation** property.

At `READ COMMITTED`, which is PostgreSQL's default and the default in most deployments, a statement
sees whatever was committed at the moment that statement began. Two transactions can each read
`units_sold = 0`, each decide there is room, and each go on to write. `BEGIN` and `COMMIT` around
unchanged logic change nothing about that.

`SERIALIZABLE` with retry-on-conflict **is** a correct transactional answer — see §9. This variant
deliberately does not use it, because "I wrapped it in a transaction" is the belief being examined.

## 5. The two controls: what a race is *not*

### 5.1 Sequential execution passes

The **identical vulnerable code**, driven by the identical client, one request at a time:

```
units available     12   units sold     12   units remaining      0
orders confirmed    12   overrun         0   shortfall       0
requests issued  orders    60 (refused 48)
```

Exactly twelve orders. The code redeemed exactly once. The ledger reconciles perfectly. Every
functional assertion passes.

**This is why the defect ships.** A sequential test cannot construct the interleaving that breaks
check-then-act, so a green sequential suite is not evidence of correctness for this class — it is
evidence that the suite never asked the question.

Note that the harness still labels this run **`inconclusive`**, not "secure". Vulnerable code that
happened not to lose a race has not demonstrated anything about its correctness, and calling that a
pass is the precise error this project exists to correct.

### 5.2 Throttling narrows the window without closing it

The other tempting response is to slow things down — a rate limit, a queue, a smaller connection
pool, a client that sends fewer requests at once:

| requests in flight at once | redemptions | wallet | overrun |
|---|---|---|---|
| 40 | 40 | 100 000 | **39** |
| 8 | 8 | 20 000 | **7** |
| 2 | 2 | 5 000 | **1** |

Exactly `N − 1`, every time. The damage falls with the window and **never reaches zero**.

> Throttling reduces the **probability** of a race. It never reduces its **possibility**. A defect
> that appears only under load is still a defect at any load.

The series above runs on the single-use invariant deliberately, because there the arithmetic is
exact and monotone. On the counter the same model produces a sawtooth: a window width that happens
to divide the remaining stock lands the batches precisely on the boundary and shows no overrun. That
is an arithmetic coincidence of the model, not a property of throttling, and a control should not be
built on one.

## 6. The fixes

Two independently correct strategies, both of which close the window rather than narrowing it.

### 6.1 Atomic conditional write — the check *is* the write

```sql
UPDATE drops
   SET units_sold = units_sold + 1
 WHERE drop_id = %(drop_id)s
   AND units_sold < units_available
RETURNING units_sold
```

The outcome is decided from the **affected row count**: one row means the unit was claimed, zero
rows means it was not. There is no read of the counter that the decision depends on, so there is no
interval for anything to occupy. The database evaluates the predicate and performs the write as one
operation, holding the row lock until that operation ends.

### 6.2 Transactional pessimistic guard — serialize every writer

```sql
SELECT units_available, units_sold FROM drops WHERE drop_id = %(drop_id)s FOR UPDATE
```

Inside one transaction, take a row-level lock on the drop *before* reading. The read-decide-write
sequence that follows looks exactly like the vulnerable one — and it is safe, because every other
writer of that row now blocks on the lock until this transaction ends.

Note what makes this different from §4.3: the lock's scope is **the row in the shared database**, so
it contains every writer of the invariant, no matter which process or machine they run on.

### 6.3 Choosing between them

| | Atomic conditional write | Transactional pessimistic guard |
|---|---|---|
| Locks held | none of your own | a row lock, for the transaction |
| Round trips | one | several |
| Decision logic | must be expressible as a predicate | arbitrary — anything can run between lock and write |
| Reads well when | the rule is "only if *X*" | the rule needs several facts, or calls out to other logic |

Both hold the invariant with zero violations at every concurrency level this project applies, at one
replica and at two, and both produce **identical** client-visible outcomes. The harness asserts they
confirm *exactly* `units_available` orders — **not fewer** — because a "fix" that protects an
invariant by refusing valid work has not fixed anything, it has broken something else.

### 6.4 The single-use invariant: let uniqueness decide

```sql
UPDATE wallets SET balance_cents = balance_cents + %(amount)s WHERE wallet_id = ...
INSERT INTO redemptions (code, ...) VALUES (...)   -- UNIQUE (code)
```

Both statements in **one transaction**. Nothing reads whether the code was already redeemed;
the second redemption fails as a constraint violation, and the credit applied a moment earlier is
rolled back with it. A credit without its redemption record — or the reverse — is impossible by
construction, and the regression matrix asserts it on every path.

## 7. The database-enforced backstop

The schema carries two constraints:

- `drops_units_sold_within_availability` — `CHECK (units_sold >= 0 AND units_sold <= units_available)`
- `redemptions_single_use` — `UNIQUE (code)`

They express the same two invariants the application guards enforce, and they hold **even when
application code is wrong**. They are a **backstop, not the primary control**. The distinction is
worth being precise about:

- a correct application never reaches them;
- an application that *relies* on them has turned a race into an **error** — a failed statement, a
  500, an alert at 3am — rather than into correct behaviour.

That is still enormously better than overselling, which is exactly why you want them. It is also why
a demonstration of the *application-level* damage has to take them off first: with the `CHECK` in
place, the unguarded write does not oversell the drop, it fails. A vulnerable run in this project
drops both constraints **by name**, says so in its output, and never touches the secure schema.
`python -m racejack.seed --secure` puts everything back.

## 8. About the instrumentation

The **deterministic** reproduction mode uses an explicitly labelled *instrumented synchronization
point* inside the vulnerable code paths — a rendezvous in the shared database that holds the
time-of-check to time-of-use window open, so the interleaving is identical on every machine and
every run. It lives in tables that say what they are (`toctou_instrumentation_gate`,
`toctou_timeline`), and a test asserts the secure application never so much as imports it.

**The window is a genuine property of the code.** The instrumentation does not create it; it holds
it open long enough to observe, so that a demonstration is a demonstration rather than a coin flip.

The evidence for that claim is the **natural** reproduction mode, which attaches nothing at all — no
rendezvous, no recorded step, no wait — and reproduces both overruns anyway, on developer machines
and on CI runners alike. A test asserts the absence is real by checking the timeline table stays
empty after a natural burst; this is not instrumentation that happens to be switched off.

Natural-mode runs report an **observed rate**, and a run that observes nothing is reported
`inconclusive`. Only the deterministic mode carries a required assertion.

## 9. What this project deliberately does not build

Three correct answers are **named and not implemented**, because two correct strategies already
carry the contrast and building more would add code without adding a lesson:

- **Optimistic version-column compare-and-set with bounded retry.** Add a `version` column; read it
  with the row; write with `WHERE version = %(seen)s` and bump it; if zero rows were affected,
  somebody else got there first — re-read and retry, up to a bound. Good when contention is low and
  you would rather not hold locks; needs a retry policy and an answer for when the bound is hit.
- **`SERIALIZABLE` isolation with retry-on-conflict.** Ask the database for the guarantee directly.
  PostgreSQL's serializable snapshot isolation will abort one of the conflicting transactions with a
  serialization failure, and your application retries it. Correct and general; costs you a retry
  path on every transaction that can conflict.
- **Distributed locks** (Redis, ZooKeeper, etcd, a lease service). The answer when the invariant
  spans systems that share **no database** — which is the only situation where §4.3's lock cannot
  simply be replaced by one of §6's guards. Introduces its own hard problems: lease expiry, clock
  assumptions, and what happens when a lock holder pauses.

Also out of scope, and named for completeness: **filesystem TOCTOU** (the canonical CWE-367 shape of
checking a path and then opening it while an attacker swaps a symlink) is a different mechanism and
needs a privileged/unprivileged split to demonstrate honestly. **DNS-resolution TOCTOU** is
`dnsrebindjack`, per §3.

## 10. What to take away

1. A condition read at one moment and acted on at a later moment is a claim about the past.
2. The interval between them is a window, and shared mutable state plus concurrency is all it takes
   to occupy it.
3. A **lock** helps only if its scope contains every writer of the invariant.
4. A **transaction** gives atomicity and durability; a lost update is an isolation problem.
5. **Throttling** changes the probability, never the possibility.
6. A **sequential test passing** is not evidence — it is the absence of the question.
7. The fix is to make the check and the act **one operation**: let the predicate ride along with the
   write, or serialize every writer on the row before deciding.
8. Put the invariant in the schema too, and understand that when it fires you have had an outage,
   not a success.

---

## Safety, once more

The vulnerable application in this repository is **intentionally broken local educational material**.
It runs only behind an opt-in Compose profile *and* an explicit `ALLOW_VULNERABLE_DEMO=true`
acknowledgement, on a container network with no egress, publishing no ports, against a database whose
storage is discarded when the run ends. Keep it that way. **Do not deploy it.**
