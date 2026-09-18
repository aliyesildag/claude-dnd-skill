"""Split a narration block so the speaker's lines leave the narrator's voice.

A DM block is prose with dialogue inside it: the scene is described, someone
speaks in quotes, the scene continues. Read by one voice the dialogue vanishes
into the narration.

Who is speaking is declared, not guessed. An earlier version inferred it from
the surrounding prose and was wrong often enough to be worse than useless on
this table's writing: a name near a quote is usually the topic rather than the
speaker ("Yavuz kupasını dolduruyor" then a line *about* Nazmi), and a short
name can collide with an ordinary word. A wrong voice is worse than one voice.
So send.py carries --speaker and this module only decides which spans are
dialogue.

Not every pair of quotes is speech. `tanıkla "ocağımda yedin" denirse` quotes an
idiom mid-sentence; splitting it would break the sentence in half and swap
voices inside a clause. A quote counts as dialogue only when it stands on its
own, which is what the two rules below test.
"""

from __future__ import annotations

import re

# Straight quotes are what this table writes; curly ones arrive via paste.
_QUOTE = re.compile(r'["“]([^"”“]+)["”]')

# Prose that ends a sentence, so a quote after it starts fresh rather than
# sitting inside a clause. A newline counts: a quote on its own line is speech.
_SENTENCE_END = re.compile(r'(?:[.!?:…]["”]?|\n)\s*$')

# Below this, a quoted fragment is a term or an idiom rather than a line.
_MIN_LINE_CHARS = 12


def _is_dialogue(prose_before: str, quoted: str) -> bool:
    if len(quoted) < _MIN_LINE_CHARS and not quoted.endswith((".", "!", "?", "…")):
        return False
    if not prose_before.strip():
        return True
    return bool(_SENTENCE_END.search(prose_before))


def split(text: str, speaker: "str | None") -> "list[tuple[str | None, str]]":
    """Return [(speaker or None, span)] in reading order.

    `None` means the narrator. With no speaker declared the block comes back
    whole, so the caller can treat one voice and many the same way.
    """
    text = (text or "").strip()
    if not text:
        return []
    if not speaker:
        return [(None, text)]

    spans: "list[tuple[str | None, str]]" = []
    cursor = 0
    for m in _QUOTE.finditer(text):
        prose = text[cursor:m.start()]
        quoted = m.group(1).strip()
        if not _is_dialogue(prose, quoted):
            continue          # leave it in the prose; cursor does not move
        stripped = prose.strip()
        if stripped:
            spans.append((None, stripped))
        spans.append((speaker, quoted))
        cursor = m.end()
    tail = text[cursor:].strip()
    if tail:
        spans.append((None, tail))
    return spans or [(None, text)]
