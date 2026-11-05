"""The race-engineer skill, for clients that cannot load a skill.

The skill under .claude/skills is the one copy of the routine. Prompts are
cut from its sections when they are asked for, so editing the skill changes
them too. It lives in the repository, not the package: an install without
the checkout beside it has no guide, and serves no prompts or guides.
"""

import re
from pathlib import Path

SKILL_DIR = (Path(__file__).resolve().parents[1]
             / ".claude" / "skills" / "race-engineer")


def guides() -> dict[str, Path]:
    """Guide name -> file: the skill itself, then each of its references."""
    skill = SKILL_DIR / "SKILL.md"
    if not skill.is_file():
        return {}
    found = {"race-engineer": skill}
    for p in sorted((SKILL_DIR / "references").glob("*.md")):
        found[p.stem] = p
    return found


def sections(*headings: str) -> str:
    """The skill's `## ` sections with these headings, in the order given.

    A heading the skill no longer has raises, naming it: a prompt that
    silently lost half its routine would read as complete.
    """
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    found = {}
    for part in re.split(r"^(?=## )", text, flags=re.M)[1:]:
        found[part.splitlines()[0][3:].strip()] = part.strip()
    missing = [h for h in headings if h not in found]
    if missing:
        raise LookupError(f"SKILL.md has no section {missing}")
    return "\n\n".join(found[h] for h in headings)
