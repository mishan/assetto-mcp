"""A session's turn numbering, from the laps it is built on.

Here rather than in server.py so that everything reading the database
numbers corners the same way -- the MCP tools, and the line map a script
draws without starting a server. The server keeps its cache on top of
this; nothing here holds state.
"""

from . import analysis, db

# How many laps a session's turn numbering is built from. Every one costs a
# full sample load, so this is a budget as much as a sample size: eight is
# well clear of the two laps analysis.corner_map wants before it will number
# a corner, and it bounds what the first lap_summary call on a race pays.
CORNER_MAP_LAPS = 8

# How many laps to read per query while looking for those eight. Usability
# is decided in Python (db.lap_usability), so the search pages rather than
# filtering in SQL -- a second copy of that rule in a WHERE clause is one
# that drifts. One page covers any ordinary session.
CORNER_MAP_PAGE = 32


def newest_usable_lap_ids(conn, session_id: int, want: int) -> list[int]:
    """The newest `want` usable laps of a session, reading no more than needed.

    This runs on every lap_summary call, cached or not, because the lap ids
    are the cache key. Reading the whole session to keep eight made a long
    race pay for every lap in it each time a single lap was summarised.
    """
    ids, offset = [], 0
    while len(ids) < want:
        page = db.list_laps(conn, session_id, limit=CORNER_MAP_PAGE,
                            offset=offset)
        ids += [l["id"] for l in page if db.lap_usability(l)[0]]
        if len(page) < CORNER_MAP_PAGE:
            break
        offset += len(page)
    return ids[:want]


def basis_lap_ids(conn, session_id: int,
                  lap_id: int | None = None) -> list[int]:
    """Which laps a session's numbering should be built from.

    The newest usable ones. When there are none -- every lap of the session
    is an out-lap, an in-lap or a lap that ended in the barrier -- the
    requested lap is all the evidence there is, and numbering from it alone
    beats answering with no numbers, as long as the payload says how thin
    that basis is, which the basis_lap_ids field corner_map_from returns
    does.
    """
    ids = newest_usable_lap_ids(conn, session_id, CORNER_MAP_LAPS)
    if not ids and lap_id is not None:
        ids = [lap_id]
    return ids


def corner_map_from(conn, lap_ids: list[int]) -> dict:
    """Turn numbers for a set of laps, plus the bar they were found against.

    The lateral-g reference comes back with the map because the two cannot
    be separated: corners found against one bar cannot be numbered by a map
    built against another, so whatever labels these laps also has to say
    which threshold produced them.
    """
    sets, used = [], []
    for lap_id in lap_ids:
        samples = db.get_samples(conn, lap_id)
        if samples:
            sets.append(samples)
            used.append(lap_id)
    detail = analysis.lat_g_reference_detail(sets)
    out = analysis.corner_map(
        [analysis.detect_corners(s, detail["reference"]) for s in sets])
    out["reference"] = detail
    out["basis_lap_ids"] = used
    return out
