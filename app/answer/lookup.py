from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from app import db
from app.answer.format import format_value, long_date
from app.answer.quant import verify_sql
from app.config import settings
from app.identity import Principal
from app.semantic.describe import state_names

VIEW = "sem.v_claim_detail"
COLUMNS = (
    "claim_id",
    "policy_id",
    "region",
    "state",
    "peril",
    "loss_date",
    "reported_date",
    "closed_date",
    "status",
    "channel",
    "adjuster_name",
    "edition",
    "deductible",
    "reserve",
    "denial_reason",
    "first_name",
    "last_name",
    "paid_total",
)
SQL = f"SELECT {', '.join(COLUMNS)} FROM {VIEW} WHERE claim_id = %s LIMIT 1"
ANALYST_TEXT = "Analysts see aggregates only, so I can't open individual claims."


@dataclass(frozen=True)
class LookupResult:
    kind: Literal["found", "not_found", "not_allowed"]
    text: str
    claim_id: int
    data_as_of: date
    fields: dict[str, Any] = field(default_factory=dict)
    sql: str | None = None


def _describe(claim: dict[str, Any]) -> str:
    state = state_names().get(claim["state"], claim["state"])
    money = {key: format_value(claim[key], "currency") for key in ("paid_total", "reserve", "deductible")}
    lines = [
        f"Claim {claim['claim_id']} is {claim['status']}: {claim['peril']} loss in {state}"
        f" on {long_date(claim['loss_date'])}, reported {long_date(claim['reported_date'])} by {claim['channel']}.",
        f"Policyholder {claim['first_name']} {claim['last_name']}, form {claim['edition']},"
        f" adjuster {claim['adjuster_name']}.",
        f"Paid {money['paid_total']}, reserve {money['reserve']}, deductible {money['deductible']}.",
    ]
    if claim["closed_date"] is not None:
        lines.append(f"Closed {long_date(claim['closed_date'])}.")
    if claim["denial_reason"]:
        lines.append(f"Denied: {claim['denial_reason']}.")
    return " ".join(lines)


async def answer_lookup(principal: Principal, claim_id: int) -> LookupResult:
    as_of = settings().as_of
    if principal.kind == "analyst":
        return LookupResult("not_allowed", ANALYST_TEXT, claim_id, as_of)
    sql = verify_sql(SQL, relations=frozenset({VIEW}))
    found = await db.run(principal, sql, (claim_id,))
    # Row security hides claims outside the user's regions, so a claim they can't see and one that
    # doesn't exist come back the same way, and must read the same way.
    if not found.rows:
        return LookupResult("not_found", f"I can't find claim {claim_id}.", claim_id, as_of, sql=sql)
    claim = dict(zip(found.columns, found.rows[0], strict=True))
    return LookupResult("found", _describe(claim), claim_id, as_of, fields=claim, sql=sql)
