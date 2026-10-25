"""Record a game's shared memory to a file, and read it back.

A recording keeps the raw pages, not our reading of them: a bug in how a
game's data is read -- a unit, a sign, an offset -- shows up when the file
is replayed through the reader, and an old recording stays good after the
reader is fixed. It is also what lets someone without the game test
support for it.

    python -m assetto_mcp.sims.recording record lap.simrec
    python -m assetto_mcp.sims.recording info lap.simrec

The file is MAGIC, then one lzma stream: a length-prefixed JSON header, then
records of (seconds since the start, page index, length, bytes). A page is
written only when its bytes changed, so the static page appears once and a
paused game adds nothing. A file cut short reads up to its last whole
record.
"""

import argparse
import json
import lzma
import struct
import sys
import time
from pathlib import Path

MAGIC = b"SIMREC\x00\x01"
FORMAT = 1
_RECORD = struct.Struct("<dBI")
_LENGTH = struct.Struct("<I")


class Writer:
    def __init__(self, path, header: dict):
        self.pages = list(header["pages"])
        self._last: dict[str, bytes] = {}
        self._file = open(path, "wb")
        self._file.write(MAGIC)
        self._z = lzma.LZMAFile(self._file, "wb")
        body = json.dumps({"format": FORMAT, **header}).encode()
        self._z.write(_LENGTH.pack(len(body)) + body)

    def write(self, t: float, raw: dict[str, bytes]) -> None:
        for i, name in enumerate(self.pages):
            data = raw[name]
            if self._last.get(name) != data:
                self._last[name] = data
                self._z.write(_RECORD.pack(t, i, len(data)) + data)

    def close(self) -> None:
        self._z.close()
        self._file.close()


def _exactly(z, n: int) -> bytes:
    data = z.read(n)
    if len(data) != n:
        raise EOFError
    return data


def read(path):
    """The header, and an iterator of (t, pages) with every page as of t."""
    f = open(path, "rb")
    if f.read(len(MAGIC)) != MAGIC:
        f.close()
        raise ValueError(f"{path} is not a recording")
    z = lzma.LZMAFile(f)
    try:
        n = _LENGTH.unpack(_exactly(z, _LENGTH.size))[0]
        header = json.loads(_exactly(z, n))
    except Exception:
        z.close()
        f.close()
        raise
    return header, _ticks(z, f, list(header["pages"]))


def _ticks(z, f, pages):
    state: dict[str, bytes] = {}
    t_now = None
    try:
        while True:
            try:
                t, i, n = _RECORD.unpack(_exactly(z, _RECORD.size))
                data = _exactly(z, n)
            except (EOFError, lzma.LZMAError):
                break
            if t_now is not None and t != t_now:
                yield t_now, dict(state)
            t_now = t
            state[pages[i]] = data
        if t_now is not None:
            yield t_now, dict(state)
    finally:
        z.close()
        f.close()


# --- command line -------------------------------------------------------


def record(out: Path, hz: float, keep_names: bool) -> None:
    from .. import sim_info

    sim = None
    while sim is None:
        try:
            sim = sim_info.SimInfo()
        except RuntimeError as e:
            if sys.platform != "win32":
                raise SystemExit(str(e)) from None
            print("waiting for Assetto Corsa...", file=sys.stderr)
            time.sleep(2)
    try:
        st = sim.static
        writer = Writer(out, {
            "sim": "ac",
            "pages": {name: len(b) for name, b in sim.raw().items()},
            "hz": hz, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "game_version": [st.acVersion, st.smVersion],
            "scrubbed": [] if keep_names else list(sim_info.PLAYER_FIELDS)})
        st = None
        print(f"recording to {out}; Ctrl+C to stop", file=sys.stderr)
        t0 = time.monotonic()
        try:
            while True:
                raw = sim.raw()
                if not keep_names:
                    raw["static"] = sim_info.scrub(raw["static"])
                writer.write(time.monotonic() - t0, raw)
                time.sleep(1 / hz)
        except KeyboardInterrupt:
            pass
        finally:
            writer.close()
    finally:
        sim.close()


def info(path: Path) -> dict:
    header, ticks = read(path)
    first = last = None
    count = 0
    pages: dict[str, bytes] = {}
    for t, pages in ticks:
        first = t if first is None else first
        last, count = t, count + 1
    out = {**header, "ticks": count,
           "seconds": round(last - first, 1) if count else 0}
    if header.get("sim") == "ac" and "static" in pages:
        from .. import sim_info
        st = sim_info.SPageFileStatic.from_buffer_copy(pages["static"])
        out.update(car=st.carModel, track=st.track,
                   track_config=st.trackConfiguration)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m assetto_mcp.sims.recording",
        description="Record the game's shared memory, or describe a "
                    "recording.")
    sub = ap.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="record until Ctrl+C")
    rec.add_argument("out", type=Path)
    rec.add_argument("--hz", type=float, default=25.0)
    rec.add_argument("--keep-names", action="store_true",
                     help="keep the driver's name in the file")
    desc = sub.add_parser("info", help="what a recording holds")
    desc.add_argument("file", type=Path)
    args = ap.parse_args(argv)
    if args.command == "record":
        record(args.out, args.hz, args.keep_names)
    else:
        print(json.dumps(info(args.file), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
