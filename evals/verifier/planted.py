import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from app.answer.format import NumberRef
from app.answer.types import Claim, Draft, Evidence
from app.sources.documents import Hit

PLANTED = Path(__file__).with_name("planted.jsonl")


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    user: str
    mutation: str  # "clean", or the kind of error planted
    planted: tuple[int, ...]  # indexes of the claims carrying the planted error
    draft: Draft
    evidence: Evidence

    @property
    def expect(self) -> Literal["pass", "fail"]:
        return "fail" if self.planted else "pass"


# Result cells keep their exact type through the file, so a derivation recomputes to the digit it did live.
def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    raise TypeError(f"can't store {type(value).__name__} in a planted case")


def _decode(obj: dict[str, Any]) -> Any:
    if obj.keys() == {"$decimal"}:
        return Decimal(obj["$decimal"])
    if obj.keys() == {"$datetime"}:
        return datetime.fromisoformat(obj["$datetime"])
    if obj.keys() == {"$date"}:
        return date.fromisoformat(obj["$date"])
    return obj


def to_line(case: Case) -> str:
    record = {
        "id": case.id,
        "question": case.question,
        "user": case.user,
        "mutation": case.mutation,
        "expect": case.expect,
        "planted": list(case.planted),
        "draft": asdict(case.draft),
        "evidence": asdict(case.evidence),
    }
    return json.dumps(record, default=_encode, ensure_ascii=False)


def from_line(line: str) -> Case:
    record = json.loads(line, object_hook=_decode)
    draft, evidence = record["draft"], record["evidence"]
    claims = tuple(
        Claim(claim["text"], tuple(NumberRef(**ref) for ref in claim["numbers"]), tuple(claim["citations"]))
        for claim in draft["claims"]
    )
    return Case(
        record["id"],
        record["question"],
        record["user"],
        record["mutation"],
        tuple(record["planted"]),
        Draft(claims, tuple(draft["caveats"])),
        Evidence(
            tuple(evidence["rows"]),
            tuple(Hit(**hit) for hit in evidence["hits"]),
            tuple(evidence["sandbox"]),
            tuple(evidence["scan_fields"]),
        ),
    )


def dump(cases: Iterable[Case], path: Path = PLANTED) -> None:
    path.write_text("".join(f"{to_line(case)}\n" for case in cases))


def load(path: Path = PLANTED) -> tuple[Case, ...]:
    return tuple(from_line(line) for line in path.read_text().splitlines() if line.strip())
