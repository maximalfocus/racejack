"""The sequential demonstration.

One request at a time, no concurrency anywhere. That restriction is deliberate and is the honest
boundary of this stage of the project: a sequential run can establish that the store *behaves*
correctly, and it can establish nothing whatsoever about whether it behaves correctly under
concurrent load. Proving the second needs a concurrent load harness, which this run does not have.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

from .. import fixtures
from ..config import CounterGuard, RunnerConfig
from ..seed import seed
from .client import RequestRecord, StorefrontClient

RULE: Final = "=" * 78
THIN: Final = "-" * 78

CREATED_STATUS: Final = 201
UNAUTHORIZED_STATUS: Final = 401
REFUSED_STATUS: Final = 409


@dataclass(frozen=True, slots=True)
class Check:
    description: str
    passed: bool
    detail: str = ""


@dataclass(slots=True)
class Report:
    checks: list[Check] = field(default_factory=list)
    refusals: int = 0
    refusal_responses: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record(self, description: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(description, passed, detail))

    def remember_refusal(self, label: str, record: RequestRecord) -> None:
        self.refusals += 1
        self.refusal_responses[label] = {"status": record.status_code, "body": body_of(record)}

    def passed_since(self, mark: int) -> bool:
        return all(check.passed for check in self.checks[mark:])

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


def body_of(record: RequestRecord) -> dict[str, Any]:
    return record.body or {}


def _verdict(report: Report, mark: int) -> str:
    return "INVARIANT HELD" if report.passed_since(mark) else "INVARIANT VIOLATED"


def _print_header(config: RunnerConfig, client: StorefrontClient) -> None:
    labels = client.replica_labels
    print(RULE)
    print(f" racejack — {fixtures.STORE_NAME} · secure application · sequential demonstration")
    print(RULE)
    print(f" replicas addressed : {len(labels)} ({', '.join(labels)})")
    print(" reproduction mode  : sequential — one request at a time, no concurrency")
    print(
        f" drop               : {fixtures.DROP_ID} "
        f'"{fixtures.DROP_PRODUCT_NAME}", {fixtures.DROP_UNITS_AVAILABLE} units available'
    )
    print(
        f" credit code        : {fixtures.CREDIT_CODE} "
        f"worth {fixtures.CREDIT_CODE_AMOUNT_CENTS} fictional credit cents"
    )
    print(f" wallet             : {fixtures.WALLET_ID}")
    print()
    print(" Everything below is fictional demonstration material. Every claim is read back through")
    print(" the store's own HTTP boundary; nothing is established by inspecting the database.")
    print()


async def _counter_scenario(
    client: StorefrontClient, config: RunnerConfig, guard: CounterGuard, report: Report
) -> None:
    await seed(config.database_url, create=False)
    mark = len(report.checks)
    print(THIN)
    print(f" Scenario · counter invariant · guard: {guard.value}")
    print(THIN)

    confirmed: list[RequestRecord] = []
    for index in range(1, fixtures.DROP_UNITS_AVAILABLE + 1):
        record = await client.place_order(fixtures.DROP_ID, buyer_index=index, guard=guard.value)
        confirmed.append(record)
        print(
            f"  order {index:>2}  {record.buyer_id}  served by {record.served_by}  "
            f"{record.status_code} {body_of(record).get('status', '')}"
        )

    over_index = fixtures.DROP_UNITS_AVAILABLE + 1
    over = await client.place_order(fixtures.DROP_ID, buyer_index=over_index, guard=guard.value)
    print(
        f"  order {over_index:>2}  {over.buyer_id}  served by {over.served_by}  "
        f"{over.status_code} refused  -> {json.dumps(body_of(over))}"
    )

    drop = body_of(await client.read_drop(fixtures.DROP_ID))
    overrun = int(drop.get("orders_confirmed", 0)) - fixtures.DROP_UNITS_AVAILABLE
    print()
    print(
        f"  store view   units_available={drop.get('units_available')}  "
        f"units_sold={drop.get('units_sold')}  units_remaining={drop.get('units_remaining')}  "
        f"orders_confirmed={drop.get('orders_confirmed')}"
    )
    print(
        f"  ledger       {fixtures.DROP_UNITS_AVAILABLE} units available · "
        f"{drop.get('orders_confirmed')} orders confirmed · overrun {overrun}"
    )

    report.record(
        f"[{guard.value}] the first {fixtures.DROP_UNITS_AVAILABLE} orders are confirmed",
        all(r.status_code == CREATED_STATUS for r in confirmed),
        f"statuses={sorted({r.status_code for r in confirmed})}",
    )
    report.record(
        f"[{guard.value}] order {over_index} is refused",
        over.status_code == REFUSED_STATUS,
        f"status={over.status_code}",
    )
    report.record(
        f"[{guard.value}] the store reports the drop sold out exactly, with no overrun",
        drop.get("units_sold") == fixtures.DROP_UNITS_AVAILABLE
        and drop.get("units_remaining") == 0
        and drop.get("orders_confirmed") == fixtures.DROP_UNITS_AVAILABLE,
        f"view={json.dumps(drop)}",
    )
    report.remember_refusal("sold out", over)
    print(f"  verdict      {_verdict(report, mark)}")
    print()


async def _redemption_scenario(
    client: StorefrontClient, config: RunnerConfig, report: Report
) -> None:
    await seed(config.database_url, create=False)
    mark = len(report.checks)
    print(THIN)
    print(" Scenario · single-use invariant · guard: unique constraint inside one transaction")
    print(THIN)

    first = await client.redeem(
        code=fixtures.CREDIT_CODE, wallet_id=fixtures.WALLET_ID, buyer_index=1
    )
    print(
        f"  redemption 1  {first.buyer_id}  served by {first.served_by}  "
        f"{first.status_code} {body_of(first).get('status', '')}  "
        f"amount={body_of(first).get('amount_cents')}  "
        f"wallet_balance={body_of(first).get('wallet_balance_cents')}"
    )
    second = await client.redeem(
        code=fixtures.CREDIT_CODE, wallet_id=fixtures.WALLET_ID, buyer_index=2
    )
    print(
        f"  redemption 2  {second.buyer_id}  served by {second.served_by}  "
        f"{second.status_code} refused  -> {json.dumps(body_of(second))}"
    )

    wallet = body_of(await client.read_wallet(fixtures.WALLET_ID))
    print()
    print(
        f"  store view   balance_cents={wallet.get('balance_cents')}  "
        f"redemption_count={wallet.get('redemption_count')}  "
        f"total_credited_cents={wallet.get('total_credited_cents')}"
    )
    print(
        f"  ledger       code face value {fixtures.CREDIT_CODE_AMOUNT_CENTS} · "
        f"{wallet.get('redemption_count')} redemption(s) · "
        f"{wallet.get('total_credited_cents')} cents credited"
    )

    report.record(
        "[single-use] the first redemption credits the wallet exactly once",
        first.status_code == CREATED_STATUS
        and body_of(first).get("amount_cents") == fixtures.CREDIT_CODE_AMOUNT_CENTS
        and body_of(first).get("wallet_balance_cents") == fixtures.CREDIT_CODE_AMOUNT_CENTS,
        f"body={json.dumps(body_of(first))}",
    )
    report.record(
        "[single-use] the second redemption of the same code is refused",
        second.status_code == REFUSED_STATUS,
        f"status={second.status_code}",
    )
    report.record(
        "[single-use] the wallet holds exactly the face value, from exactly one redemption",
        wallet.get("balance_cents") == fixtures.CREDIT_CODE_AMOUNT_CENTS
        and wallet.get("redemption_count") == 1
        and wallet.get("total_credited_cents") == fixtures.CREDIT_CODE_AMOUNT_CENTS,
        f"view={json.dumps(wallet)}",
    )
    report.remember_refusal("already redeemed", second)
    print(f"  verdict      {_verdict(report, mark)}")
    print()


async def _credential_scenario(
    client: StorefrontClient, config: RunnerConfig, report: Report
) -> None:
    await seed(config.database_url, create=False)
    print(THIN)
    print(" Scenario · demo credentials · every failure mode answers identically")
    print(THIN)

    probes: dict[str, str | None] = {
        "missing": None,
        "malformed (no scheme)": "racejack-demo-token-0001",
        "malformed (wrong scheme)": "Basic racejack-demo-token-0001",
        "unknown": "Bearer racejack-demo-token-not-issued",
        "expired": "Bearer racejack-demo-token-expired",
    }
    observed: list[tuple[int, str]] = []
    for label, header in probes.items():
        record = await client.probe_credential(fixtures.DROP_ID, authorization=header)
        rendered = json.dumps(body_of(record), sort_keys=True)
        observed.append((record.status_code, rendered))
        print(f"  {label:<26} {record.status_code}  -> {rendered}")

    drop = body_of(await client.read_drop(fixtures.DROP_ID))
    print()
    print(
        f"  store view   units_sold={drop.get('units_sold')}  "
        f"orders_confirmed={drop.get('orders_confirmed')} (unauthenticated attempts sold nothing)"
    )
    print()

    report.record(
        "[credentials] missing, malformed, unknown, and expired tokens all return 401",
        all(status == UNAUTHORIZED_STATUS for status, _ in observed),
        f"statuses={[status for status, _ in observed]}",
    )
    report.record(
        "[credentials] all four failure modes return a byte-identical body",
        len({rendered for _, rendered in observed}) == 1,
        f"distinct bodies={len({rendered for _, rendered in observed})}",
    )
    report.record(
        "[credentials] no unauthenticated attempt changed the store's state",
        drop.get("units_sold") == 0 and drop.get("orders_confirmed") == 0,
        f"view={json.dumps(drop)}",
    )


def _check_no_oracle(report: Report) -> None:
    responses = list(report.refusal_responses.values())
    identical = len(responses) > 1 and all(response == responses[0] for response in responses)
    report.record(
        "[no oracle] every refusal returns the same generic response, whatever caused it",
        identical,
        f"observed={json.dumps(report.refusal_responses, sort_keys=True)}",
    )
    print(RULE)
    print(" Refusal responses")
    print(RULE)
    for label, response in report.refusal_responses.items():
        print(f"  {label:<20} {response['status']}  -> {json.dumps(response['body'])}")
    print()


def _print_summary(report: Report) -> None:
    print(RULE)
    print(" Reconciliation")
    print(RULE)
    for check in report.checks:
        print(f"  [{'PASS' if check.passed else 'FAIL'}] {check.description}")
        if not check.passed:
            print(f"         {check.detail}")
    print()
    print(RULE)
    if report.passed:
        print(" VERDICT: both invariants held, sequentially, through the store's own boundary.")
        print()
        print(" What this run does NOT establish: a sequential run cannot construct the request")
        print(" interleaving that breaks a check-then-act sequence. Correct behaviour here is not")
        print(" evidence of correct behaviour under concurrent load — proving that needs a")
        print(" concurrent load harness, which this stage of the project does not yet have.")
    else:
        print(" VERDICT: FAILED — a secure-side expectation was not met. That is a genuine")
        print(" failure, never a flake to be retried away.")
    print(RULE)
    print(
        "racejack-demo-summary: "
        + json.dumps(
            {"passed": report.passed, "refusals": report.refusals, "checks": len(report.checks)},
            sort_keys=True,
        )
    )


async def run(config: RunnerConfig) -> int:
    report = Report()
    async with StorefrontClient(
        config.replica_urls, timeout=config.request_timeout_seconds
    ) as client:
        await client.wait_until_ready()
        _print_header(config, client)
        for guard in (CounterGuard.CONDITIONAL_WRITE, CounterGuard.PESSIMISTIC_LOCK):
            await _counter_scenario(client, config, guard, report)
        await _redemption_scenario(client, config, report)
        await _credential_scenario(client, config, report)
    _check_no_oracle(report)
    _print_summary(report)
    return 0 if report.passed else 1
