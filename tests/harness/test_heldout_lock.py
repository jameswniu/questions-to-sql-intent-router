import hashlib
from pathlib import Path

import pytest

from evals.splits import SPLITS, LockMismatch, Split, load, verify_lock

NAMES = ("routing", "quantitative", "qualitative", "why", "ocr", "permissions", "paraphrase")


def tree(root: Path, content: bytes, listed: bytes | None = None) -> None:
    heldout = root / "evals" / "heldout"
    heldout.mkdir(parents=True)
    (heldout / "routing.jsonl").write_bytes(content)
    digest = hashlib.sha256(content if listed is None else listed).hexdigest()
    (root / "evals" / "heldout.lock").write_text(f"{digest}  evals/heldout/routing.jsonl\n")


def test_the_committed_lock_matches_the_heldout_files() -> None:
    verify_lock()


def test_a_heldout_file_that_changed_refuses_the_run(tmp_path: Path) -> None:
    tree(tmp_path, b'{"id": "a"}\n', listed=b'{"id": "b"}\n')
    with pytest.raises(LockMismatch, match="does not match its checksum"):
        verify_lock(tmp_path)


def test_a_heldout_file_the_lock_does_not_name_refuses_the_run(tmp_path: Path) -> None:
    tree(tmp_path, b'{"id": "a"}\n')
    (tmp_path / "evals" / "heldout" / "why.jsonl").write_text("{}\n")
    with pytest.raises(LockMismatch, match="why.jsonl is not in the lock"):
        verify_lock(tmp_path)


@pytest.mark.parametrize("name", NAMES)
def test_every_case_file_holds_only_its_own_split(name: str) -> None:
    for split in SPLITS:
        assert all(case["split"] == split for case in load(split, name))


def test_no_dev_question_or_paraphrase_repeats_a_heldout_question() -> None:
    def asked(split: Split) -> set[str]:
        return {" ".join(case["q"].lower().split()) for name in NAMES for case in load(split, name)}

    assert not asked("dev") & asked("heldout")
