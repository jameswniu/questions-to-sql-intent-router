from app.answer.passages import split
from app.llm.request import Passage
from app.sources.documents import Hit


def passage(hit: Hit) -> Passage:
    """A retrieved chunk as the model reads it: its header, then one block per sentence, so a citation names the
    sentences it rests on. This wrapper is the only way document text enters a request."""
    blocks = [hit.header] if hit.header and hit.header.strip() else []
    blocks += [text for text in split(hit.body) if text.strip()]
    return Passage(hit.chunk_id, hit.title or hit.doc_id, tuple(blocks) or (hit.body,))
