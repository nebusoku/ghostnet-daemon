from typing import List
def simple_overlap_chunks(text: str, size: int, overlap: int) -> List[str]:
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        chunks.append(text[start:end])
        if end == len(text): break
        start = end - overlap
    return chunks
