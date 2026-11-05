"""Text from outside the project, made safe to hand to the model.

Lobby driver names, mod tracks' section names and downloaded setup files
are written by strangers and end up in tool replies, some of them in
strings the skill says to quote to the driver.
"""

import re
import unicodedata

# Backticks and angle brackets are how text in a reply pretends to be code
# or markup around it; nothing in a name needs them.
_MARKUP = str.maketrans("`<>", "'()")


def untrusted(raw, limit: int) -> str:
    """`raw` on one line of printable text, at most `limit` characters.

    Control and format characters (zero-width spaces, direction overrides)
    are dropped, line breaks become spaces. Non-strings become "" rather
    than their repr.
    """
    if not isinstance(raw, str):
        return ""
    kept = []
    for ch in raw.translate(_MARKUP):
        cat = unicodedata.category(ch)
        if cat[0] == "Z" or ch in "\t\n\r\v\f":
            kept.append(" ")
        elif cat[0] != "C":
            kept.append(ch)
    return re.sub(r"\s+", " ", "".join(kept)).strip()[:limit].rstrip()
