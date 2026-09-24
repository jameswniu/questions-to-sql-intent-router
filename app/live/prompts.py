from functools import cache

from app.semantic.layer import LAYER_PATH


@cache
def semantic_text() -> str:
    """The semantic layer as its file reads. It never changes while the process runs, so the prefix it ends caches."""
    return f"The semantic layer, which defines every measure, dimension and value:\n\n{LAYER_PATH.read_text()}"


def main_system(instructions: str) -> tuple[str, ...]:
    """A main-model system prompt: the instructions, then the semantic layer, which ends the cached prefix."""
    return instructions, semantic_text()
