# racejack

A small, container-only educational project about **check-then-act races** — reading a condition at
one moment and acting on it at a later moment, when the fact you checked was already stale by the
time you acted on it.

It models a fictional limited-release storefront, *Kestrel Supply*, whose business rules rest on two
invariants that check-then-act code breaks in two different ways:

| Invariant | The question it answers | Fixture |
|---|---|---|
| `units_sold ≤ units_available` | "how many are left?" | drop `DROP-2026-03` — *Tidewater Field Jacket*, 12 units |
| each credit code redeems exactly once | "has this already happened?" | code `KESTREL-WELCOME-2500`, worth 2500 fictional credit cents |

Everything here is invented. There are no real stores, buyers, products, promotional codes, wallet
balances, or credentials, and nothing in this project contacts any system other than its own
containers.

> **Status — in progress.** The storefront, both correct implementations, the concurrent load
> harness, the full four-shape vulnerable ladder, both negative controls, and the regression matrix
> are here. Still to come: the comparison CLI and the educational walkthrough.

## Run it

You need Docker. You do not need PostgreSQL, a Python environment, or any host tuning.

```sh
bash scripts/demo.sh
```

That brings up two application replicas over one PostgreSQL instance on a container network with no
egress, seeds fresh fictional fixtures, runs the sequential demonstration, prints a reconciled
ledger, and tears everything down again. It takes well under five minutes once images are built.

The concurrent load harness — sixty buyers arriving at the twelve-unit drop at the same moment,
forty redemptions of one single-use code at the same moment, across every guard and every replica
count:

```sh
docker compose up --detach --wait app-a app-b
docker compose run --rm harness
```

The full gate — the sequential demonstration, the audit-event check, the harness, Ruff, mypy, and
the test suite, all through the same Compose boundary that CI uses:

```sh
bash scripts/verify.sh
```

### Run parameters

| Variable | Values | What it changes |
|---|---|---|
| `RACEJACK_REPLICAS` | `1`, `2` (default `2`) | how many of the running replicas the runner addresses |
| `RACEJACK_COUNTER_GUARD` | `conditional_write` (default), `pessimistic_lock` | the counter strategy a replica uses when a request does not ask for one |
| `RACEJACK_ORDER_CONCURRENCY` | 1–96 (default `60`) | concurrent buyers per burst |
| `RACEJACK_REDEMPTION_CONCURRENCY` | 1–96 (default `40`) | concurrent redemptions per burst |
| `RACEJACK_ROUNDS` | 1–20 (default `3`) | how many times each harness scenario repeats |

The replica count is a **demonstration parameter, not deployment detail**. The number of processes
that share the state is the difference between a lock that works and a lock that only appeared to,
so it is a first-class dial rather than a scaling knob. A request may also select the counter
strategy per call with the `X-Racejack-Guard` header; both strategies are correct and produce
identical client-visible outcomes.

```sh
RACEJACK_REPLICAS=1 bash scripts/demo.sh
```

## What the store does

| Endpoint | Purpose |
|---|---|
| `POST /drops/{drop_id}/orders` | place one order for one unit |
| `POST /credit/redemptions` | redeem a credit code into a wallet |
| `GET /drops/{drop_id}` | the store's own view of a drop |
| `GET /credit/wallets/{wallet_id}` | the store's own view of a wallet |

The two `GET` endpoints exist so that every claim the demonstration makes can be read back through
the product's own boundary — units available against orders confirmed, code face value against
redemptions and total credited — rather than by inspecting the database.

Requests carry a conspicuously fake demo bearer token (`racejack-demo-token-0001`, …). Missing,
malformed, unknown, and expired tokens all receive the same `401`, and tokens never appear in logs
or run artifacts. Authentication is not the subject here, and it deliberately does nothing that
could serialize or deduplicate concurrent requests.

## The three guards

Each one closes the window between the check and the act, and each closes it differently.

**A — atomic conditional write** (`src/racejack/secure/guards.py`). The check *is* the write:

```sql
UPDATE drops
   SET units_sold = units_sold + 1
 WHERE drop_id = %(drop_id)s
   AND units_sold < units_available
RETURNING units_sold
```

The outcome is decided from the affected row count — one row means the unit was claimed, zero rows
means it was not. No read of the counter feeds that decision, so there is no interval for a
concurrent request to occupy.

**B — transactional pessimistic guard.** Inside one transaction, take a row-level lock on the drop
(`SELECT … FOR UPDATE`) before reading, deciding, and writing. The read-decide-write sequence still
exists, but every other writer of the same row now blocks on the lock, so it cannot interleave.

The trade-off is worth stating plainly: the conditional write holds no lock and needs one round
trip, but requires the invariant to be expressible as a predicate; the pessimistic guard serializes
writers on the row and admits arbitrary decision logic in between.

**C — uniqueness-enforced single-use redemption.** The redemption is inserted against a `UNIQUE`
constraint and the wallet is credited in the *same* transaction, so a second redemption fails as a
constraint violation and takes its own credit down with it. Nothing reads whether the code was
already redeemed.

### The database-enforced backstop

The schema carries `drops_units_sold_within_availability` (a `CHECK`) and `redemptions_single_use`
(a `UNIQUE`). They express the same two invariants the application guards enforce and they hold even
when application code is wrong — but they are a **backstop, not the primary control**. A correct
application never reaches them; an application that relies on them has turned a race into an error
rather than into correct behaviour.

## Containment

- The container network is `internal`, so nothing in the demonstration can reach anything outside
  it, and **no service publishes a port** — the database in particular is unreachable from the host.
- The database's data directory lives on tmpfs, so no run inherits another run's state and nothing
  persists on the host. Fixtures are recreated from scratch on every run.
- Every container runs non-root with all Linux capabilities dropped, `no-new-privileges`, and a
  read-only root filesystem, with a small writable `tmpfs` where one is genuinely needed.
- The only state this project ever changes is its own fictional drop, wallet, and redemption rows.

## What a sequential run does not prove

`scripts/demo.sh` exercises the store one request at a time, and a sequential run cannot construct
the interleaving that breaks a check-then-act sequence. Correct behaviour under sequential load is
therefore **not** evidence of correct behaviour under concurrent load — which is precisely why this
class of defect survives code review and a green test suite.

## The concurrent load harness

The harness is how the project stops taking the sequential run's word for it. Every request in a
burst gets its own task, each task's first action is to wait on a barrier, and they all leave the
starting line together when the last one arrives. The number of tasks *is* the bound: the load is
explicit configuration, aimed only at the demonstration's own services, on a network with no egress.

Every run ends in a reconciliation that puts the numbers that must agree next to each other:

```
units available     12   orders confirmed     12   overrun      0   shortfall      0
code face value   2500   redemptions           1   credited   2500   wallet      2500
VERDICT: INVARIANT HELD   (secure application, natural reproduction mode)
```

Against the secure application the assertion is **exact**: not "no more than twelve orders" but
*exactly* twelve — not fewer — and *exactly* one credit, with zero violations in every round, at one
replica and at two, and canonical state byte-for-byte identical to a correct sequential run of the
same request count. That exactness does double duty. It shows the fix preserves legitimate work
instead of protecting the invariant by refusing valid requests, and it is the strongest available
evidence that the harness is genuinely concurrent — a client that quietly serialized its own
requests could never spread a burst across two replicas.

The harness writes a transcript to `artifacts/harness-transcript.txt` carrying every per-request
record behind those claims: the buyer, the replica addressed, the replica that **served** it, the
status, the outcome, and the request id that matches the store's own audit event. No token, secret,
or personal datum reaches it.

### Zero violations means two different things

The **natural** reproduction mode runs with no instrumentation whatsoever in any code path and
reports the overrun rate it observed. What that rate means depends entirely on what was being
driven:

- against the **secure** application, zero violations is an exact assertion and a pass — a
  secure-side violation would be a genuine failure, never a flake to retry away;
- against a **vulnerable** application, a run that happens to observe nothing is **`inconclusive`**
  — never a pass, and never evidence that the code is correct.

Concluding otherwise from a quiet run is exactly the reasoning error this project exists to correct,
so the verdict classifier distinguishes the two cases rather than leaving it to a reader.

### What the harness is not

It measures **correctness under concurrency only**. It makes no throughput, latency, or performance
claim of any kind, none appears in its output, and a test asserts that none ever does. It is not a
load-testing tool and must never become one.

## The vulnerable application

> **Intentionally broken educational material. Never deploy this.**

Starting it takes **two** deliberate actions, and neither alone is enough:

```sh
ALLOW_VULNERABLE_DEMO=true docker compose --profile vulnerable up --detach --wait vuln-a vuln-b
docker compose run --rm harness python -m racejack.harness --variant vulnerable --mode deterministic
```

The opt-in profile selects the service; the environment variable acknowledges what it is. The
default Compose path never starts it, and the application refuses to construct itself without the
acknowledgement — a profile can be enabled by copying a command line, so on its own it does not
count as one.

Its routes, credentials, read views, refusal responses, and success payloads are *literally the same
code* as the secure application's (`src/racejack/api.py`). The entire difference is
`vulnerable/shapes.py` versus `secure/guards.py`:

```python
#  secure — the check IS the write, one statement, decided on affected row count
UPDATE drops SET units_sold = units_sold + 1
 WHERE drop_id = %(drop_id)s AND units_sold < units_available

#  vulnerable — a read, a decision, and then a separate write
SELECT units_available, units_sold FROM drops WHERE drop_id = %(drop_id)s   # time of check
...                                                                         # ← the window
UPDATE drops SET units_sold = units_sold + 1 WHERE drop_id = %(drop_id)s    # time of use
```

Neither shape is exotic and neither looks wrong. That is why this class of defect survives code
review, and why a green test suite does not catch it.

### What it does under load

Sixty concurrent buyers against a twelve-unit drop, and forty concurrent redemptions of one
single-use code:

```
 units available     12   units sold     60   units remaining    -48
 orders confirmed    60   overrun          48   shortfall        0
 code face value   2500   redemptions      40   credited  100000   wallet   100000
 VERDICT: INVARIANT VIOLATED   (vulnerable application, deterministic reproduction mode)
```

The store confirmed sixty orders for twelve units and reports **minus forty-eight** remaining. One
credit code worth 2500 cents credited a wallet with 100 000.

### The interleaving timeline

The deterministic mode prints the requests that raced — the reads that observed the same value, and
the writes that followed, with the serving replica on each line. That pair *is* the vulnerability:

```
    60 requests read DROP-2026-03 as 0 before any of them wrote
      step     1  vuln-a   CHECK  order-00018    observed=0
      step     2  vuln-a   CHECK  order-00034    observed=0
      ...
      step   132  vuln-b    ACT   order-00018    observed=7   decided on units_sold=0
```

### About the instrumentation, plainly

The deterministic mode uses an explicitly labelled **instrumented synchronization point** that holds
the time-of-check to time-of-use window open, so the interleaving is identical on every machine and
every run. It lives only in vulnerable code paths, in tables that say what they are
(`toctou_instrumentation_gate`, `toctou_timeline`), and a test asserts the secure application never
so much as imports it.

The window is a **genuine property of the code**. The instrumentation does not create it; it only
holds it open long enough to observe. The evidence is the natural mode, which attaches nothing at
all — no rendezvous, no recorded step, no wait — and still reproduces both overruns:

```
observed overrun rate (natural mode): 3/12 rounds, 98/600 requests
rounds reported inconclusive: 9/12
```

Note what the other nine rounds are called. A natural run that observes no violation is
**`inconclusive`** — never a pass, and never evidence the code is correct. Treating a quiet run as
proof is exactly the reasoning error this project exists to correct.

### The other two shapes: both look like fixes

**Half-fix 1 — the process-scoped lock.** The same check-then-act, wrapped in an ordinary
`asyncio.Lock`, used completely correctly:

```
 counter · shape: process_lock · replicas addressed: 1
  units available     12   units sold     12   units remaining      0
  orders confirmed    12   overrun         0   shortfall       0

 counter · shape: process_lock · replicas addressed: 2
  units available     12   units sold     13   units remaining     -1
  orders confirmed    13   overrun         1   shortfall       0
```

Same code. Same fixture state. Same burst. The only variable that changed is **how many processes
share the state** — and behind one replica the lock is genuinely holding the invariant, which is
precisely what makes it dangerous. A lock protects an invariant only if its scope contains **every
writer of that invariant**, and this invariant lives in the database.

**Half-fix 2 — the single transaction.** The same read-check-write inside one transaction, at the
isolation level the run reads back from the database itself:

```
 counter · shape: single_transaction · replicas addressed: 2
  units available     12   units sold     60   units remaining    -48
  orders confirmed    60   overrun        48   shortfall       0
  isolation in effect  read committed
```

Unchanged. A transaction buys **atomicity** — all of it happens or none of it does — and durability.
The lost update it would have to prevent is neither; it is an **isolation** property. At the
`READ COMMITTED` default a statement sees whatever was committed when that statement began, so two
transactions can each read the same value and each go on to write. `SERIALIZABLE` with
retry-on-conflict *is* a correct transactional answer; this variant deliberately does not use it.

## The two controls: what a race is *not*

**Sequential execution passes.** The identical vulnerable code, one request at a time:

```
 counter · shape: unguarded · replicas addressed: 2 · sequential
  units available     12   units sold     12   units remaining      0
  orders confirmed    12   overrun         0   shortfall       0
  requests issued  orders    60 (refused 48)
```

Perfect. Every functional assertion satisfied, the ledger reconciling exactly. **This is why the
defect ships** — past code review, past a green test suite — and it is why the run is still labelled
`inconclusive` rather than passing. A sequential test cannot construct the interleaving that breaks
check-then-act, so it can never be evidence of correctness for this class.

**Throttling narrows the window without closing it.** A client that keeps only *N* requests in
flight at once, against the single-use code:

| in flight at once | redemptions | wallet | overrun |
|---|---|---|---|
| 40 | 40 | 100 000 | **39** |
| 8 | 8 | 20 000 | **7** |
| 2 | 2 | 5 000 | **1** |

The damage falls exactly as the window narrows, and never reaches zero. A rate limit, an added
delay, a queue, or a slower caller reduces the **probability** of a race and never its
**possibility**. A defect that appears only under load is still a defect at any load.

(The series runs on the single-use invariant deliberately: there the arithmetic is exact — the first
*N* requests all read "not redeemed" and all redeem, so the overrun is *N−1* for every *N*. On the
counter the same model produces a sawtooth, because a window that happens to divide the remaining
stock lands the batches exactly on the boundary. That is a coincidence of the model, not anything
true about throttling, and a control should not be built on one.)

### Why the backstop comes off

A vulnerable run drops `drops_units_sold_within_availability` and `redemptions_single_use` by name,
and says so in its output. With them in place the unguarded write does not oversell the drop — it
fails as a constraint violation, and the store returns an *error* instead of a negative remaining
count. That is exactly what a backstop is for, and it is why a real system wants one. It is also why
showing the application-level damage means taking it off first and being loud about it. The secure
schema is untouched, and `python -m racejack.seed --secure` puts everything back.

## Layout

```
src/racejack/
  config.py      run parameters (replica count, counter guard)
  schema.py      the shared store's schema, including the backstop constraints
  fixtures.py    deterministic, wholly fictional fixture data
  seed.py        create the schema and rebuild fixtures from scratch
  auth.py        demo-only bearer tokens
  audit.py       the generic rejection audit event
  store.py       the store's own read-side views
  auditcheck.py  the gate that verifies those audit events in a captured log stream
  httpclient.py  the shared HTTP boundary; knows how to speak, not when
  api.py         the HTTP boundary both variants share, so only the sequencing differs
  instrumentation.py  the labelled synchronization point and the interleaving timeline
  secure/        the secure application: guards.py holds the three strategies
  vulnerable/    the opt-in vulnerable application: shapes.py holds all four shapes
  demo/          the sequential demonstration runner
  harness/       the concurrent load harness: burst, ledger, engine, transcript
tests/           the regression suite, run inside the same network
artifacts/       harness transcripts from the last run (disposable, gitignored)
```

## Safety

This is local educational material. It is not a product, it is not hardened for anything but the
demonstration, and it must not be deployed or exposed to a network you do not control. The
vulnerable application in particular is **deliberately broken** and exists only to be observed
failing on a container network with no egress.
