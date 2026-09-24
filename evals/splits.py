import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from app.config import ROOT

Split = Literal["dev", "heldout"]
Case = dict[str, Any]
SPLITS: tuple[Split, ...] = ("dev", "heldout")
FOLDERS: dict[Split, str] = {"dev": "cases", "heldout": "heldout"}


class LockMismatch(Exception):
    pass


def verify_lock(root: Path = ROOT) -> None:
    """The held-out files must be byte for byte the ones evals/heldout.lock names, and it must name all of them."""
    evals = root / "evals"
    listed: dict[Path, str] = {}
    for line in (evals / "heldout.lock").read_text().splitlines():
        digest, _, name = line.partition("  ")
        listed[(root / name).resolve()] = digest
    problems = []
    for path, digest in listed.items():
        if not path.exists():
            problems.append(f"{path.name} is missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            problems.append(f"{path.name} does not match its checksum")
    unlisted = sorted({path.resolve() for path in (evals / "heldout").glob("*.jsonl")} - set(listed))
    problems += [f"{path.name} is not in the lock" for path in unlisted]
    if problems:
        raise LockMismatch("; ".join(problems))


def load(split: Split, name: str, root: Path = ROOT) -> list[Case]:
    path = root / "evals" / FOLDERS[split] / f"{name}.jsonl"
    if not path.exists():
        return []
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    wrong = sorted({case["split"] for case in cases} - {split})
    if wrong:
        raise ValueError(f"{path.name} holds {wrong} cases, but it is read as {split}")
    return cases
