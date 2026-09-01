"""Suspension analysis: damper histograms, ride height, roll balance.

The load-bearing question in this module is the sign convention. CSP
documents neither the units nor the direction of suspension travel, and
whether a rising number means compression decides whether the advice is
"add bump" or "add rebound". Getting it backwards is worse than declining
to answer, so most of what follows is about that.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module  # noqa: E402

from ac_race_engineer import db, suspension as susp  # noqa: E402


def _lap(rate_hz=333.0, n=1200, compression_is_positive=True,
         brake_zones=((0.15, 0.22), (0.55, 0.62)), amplitude=0.004,
         compress_samples=5, extend_samples=25):
    """A synthetic lap of suspension samples.

    The front dives under braking and settles back afterwards, in whichever
    direction the caller says compression is. The dive is deliberately
    faster than the settle, as it is on a real car -- weight transfers onto
    the nose quickly when the brakes go on, and the spring pushes it back
    against damping when they come off. That asymmetry is what a bump/
    rebound split is supposed to reveal, so a square wave (equal peaks both
    ways) would be a test that cannot fail informatively.
    """
    out = []
    step_ms = 1000.0 / rate_hz
    level = 0.0                       # physical compression, always >= 0
    up = amplitude / max(1, compress_samples)
    down = amplitude / max(1, extend_samples)
    for i in range(n):
        pos = i / n
        braking = any(lo <= pos <= hi for lo, hi in brake_zones)
        brake = 0.9 if braking else 0.0

        target = amplitude if braking else 0.0
        if level < target:
            level = min(target, level + up)
        elif level > target:
            level = max(target, level - down)

        offset = level if compression_is_positive else -level
        # Small ripple so the trace isn't perfectly smooth, but well below
        # the dive so it can't dominate the bins.
        ripple = 0.00002 * ((i % 7) - 3)

        out.append({
            "lap_count": 0,
            "t_ms": int(i * step_ms),
            "spline": pos,
            "brake": brake,
            "speed_kmh": 90.0 if braking else 180.0,
            "travel_fl": offset + ripple,
            "travel_fr": offset + ripple,
            "travel_rl": ripple,
            "travel_rr": ripple,
            "load_fl": 4000.0 + (900.0 if braking else 0.0),
            "load_fr": 4000.0 - (900.0 if braking else 0.0),
            "load_rl": 3000.0 + (200.0 if braking else 0.0),
            "load_rr": 3000.0 - (200.0 if braking else 0.0),
            "ride_f": 0.050 - (0.010 if braking else 0.0),
            "ride_r": 0.070,
            "plank_wear": 0.02,
        })
    return out


# --- sign convention ----------------------------------------------------


def test_compression_direction_is_read_from_braking():
    """Whichever way the front moves under braking is compression."""
    for positive in (True, False):
        conv = susp.infer_compression_sign(
            _lap(compression_is_positive=positive))
        assert conv["sign"] == (1 if positive else -1), (positive, conv)
        assert conv["confidence"] > 0.5, conv
    print("  convention recovered in both directions")


def _parked_on_the_brakes(n=7295, parked_samples=5905):
    """Sitting in the garage with the pedal held down.

    Reproduces a real out-lap: 7295 samples, of which 5905 read >60% brake
    because the car was stationary with the brake on. Those are the counts
    from the session, not a proportion of them -- the ratio is what decides
    whether the stop-start guard or the speed gate answers first, so
    inventing one would test a lap that never happened. The front travel
    does not move -- there is no load transfer -- but a tiny constant offset
    from the resting trace is enough for a median comparison to find a
    direction and report it at full confidence.
    """
    rolling = max(1, n - parked_samples)
    out = []
    for i in range(n):
        parked = i < parked_samples
        out.append({
            "lap_count": 0,
            "t_ms": int(i * 20),
            "spline": 0.0 if parked else (i - parked_samples) / rolling,
            "brake": 0.9 if parked else 0.0,
            "speed_kmh": 0.0 if parked else 120.0,
            # Parked sits fractionally lower than rolling: the wrong sign,
            # which is exactly what the old code latched onto.
            "travel_fl": -0.0057 if parked else 0.0,
            "travel_fr": -0.0057 if parked else 0.0,
            "travel_rl": 0.0, "travel_rr": 0.0,
            "load_fl": 4000.0, "load_fr": 4000.0,
            "load_rl": 3000.0, "load_rr": 3000.0,
            "ride_f": 0.050, "ride_r": 0.070, "plank_wear": 0.0,
        })
    return out


def test_a_car_parked_on_the_brakes_cannot_set_the_sign():
    """Regression: an out-lap inverted the sign at confidence 1.0.

    Session 9 produced sign=-1 from the out-lap and sign=+1 from a flying
    lap, both claiming confidence 1.0, because 'brake pressed' was taken as
    'weight transferring forward'. Since this sign decides bump vs rebound,
    a wrong answer is worse than no answer.
    """
    samples = _parked_on_the_brakes()
    # The numbers the docstring quotes are the session's; assert them here
    # so the fixture cannot drift away from the lap it claims to reproduce.
    assert len(samples) == 7295
    assert sum(1 for s in samples if s["brake"] > 0.6) == 5905

    got = susp.infer_compression_sign(samples)
    assert got["sign"] is None, got
    assert got["confidence"] == 0.0
    assert "slow" in got["basis"] or "km/h" in got["basis"], got["basis"]


def test_a_stop_start_lap_is_refused_even_above_the_speed_floor():
    """Crawling laps brake for most of their length; that isn't a braking zone."""
    samples = []
    for i in range(3000):
        braking = i % 10 < 8            # 80% of the lap on the brakes
        samples.append({
            "lap_count": 0, "t_ms": int(i * 20), "spline": i / 3000,
            "brake": 0.9 if braking else 0.0,
            "speed_kmh": 60.0,          # above the floor, still not racing
            "travel_fl": 0.004 if braking else 0.0,
            "travel_fr": 0.004 if braking else 0.0,
            "travel_rl": 0.0, "travel_rr": 0.0,
            "load_fl": 4000.0, "load_fr": 4000.0,
            "load_rl": 3000.0, "load_rr": 3000.0,
            "ride_f": 0.050, "ride_r": 0.070, "plank_wear": 0.0,
        })
    got = susp.infer_compression_sign(samples)
    assert got["sign"] is None, got
    assert "stop-start" in got["basis"], got["basis"]


def test_a_real_lap_still_infers_the_sign_after_the_speed_gate():
    """The gate must not cost us the answer on laps that do have braking."""
    for positive in (True, False):
        got = susp.infer_compression_sign(
            _lap(compression_is_positive=positive))
        assert got["sign"] == (1 if positive else -1), got
        assert got["confidence"] > 0.8, got
        assert "km/h" in got["basis"]


def test_bump_and_rebound_are_withheld_when_direction_is_unknown():
    """A lap with no braking cannot establish the convention.

    Reporting a bump/rebound split anyway would be a coin flip presented as
    a measurement, so the magnitudes are reported and the split is not.
    """
    hist = susp.damper_histogram(_lap(brake_zones=()), susp.SOURCE_WORKER)
    assert hist["available"] is True
    assert hist["sign_convention"]["sign"] is None
    front = hist["axles"]["front"]
    assert "bump_pct" not in front, front
    assert "rebound_pct" not in front, front
    assert "magnitude_pct" in front, front
    assert "Direction" in front["note"]
    print("  no braking ->", front["note"][:52] + "...")


def test_compression_lands_in_bump_not_rebound():
    """The end-to-end check on the sign: a lap that only ever compresses
    the front under braking must show that motion as bump."""
    hist = susp.damper_histogram(_lap(compression_is_positive=True),
                                 susp.SOURCE_WORKER)
    front = hist["axles"]["front"]
    assert hist["sign_convention"]["sign"] == 1
    assert front["peak_bump_mm_s"] > front["peak_rebound_mm_s"], front

    # And the mirror image: flipping the data's convention must not flip
    # the conclusion, because the convention is inferred, not assumed.
    flipped = susp.damper_histogram(_lap(compression_is_positive=False),
                                    susp.SOURCE_WORKER)
    ffront = flipped["axles"]["front"]
    assert flipped["sign_convention"]["sign"] == -1
    assert ffront["peak_bump_mm_s"] > ffront["peak_rebound_mm_s"], ffront
    print(f"  bump peak {front['peak_bump_mm_s']} vs rebound "
          f"{front['peak_rebound_mm_s']} mm/s, both conventions")


# --- tier honesty -------------------------------------------------------


def test_render_rate_histograms_are_labelled_as_body_motion():
    """60Hz cannot describe damper valving and must not claim to.

    Nyquist for a 60Hz stream is 30Hz; real damper content runs well past
    it, so the high-speed bins are aliasing artefacts.
    """
    hist = susp.damper_histogram(_lap(rate_hz=60, n=400), susp.SOURCE_APP)
    assert hist["available"] is True
    assert "caution" in hist
    assert "body motion" in hist["caution"]
    assert "aliased" in hist["caution"]
    print("  render-rate caution:", hist["caution"][:56] + "...")


def test_physics_rate_histograms_carry_no_caution():
    hist = susp.damper_histogram(_lap(rate_hz=333), susp.SOURCE_WORKER)
    assert "caution" not in hist, hist.get("caution")
    assert hist["source"] == susp.SOURCE_WORKER
    print("  333Hz data reported without a caveat")


def test_a_short_sample_run_is_not_a_histogram():
    hist = susp.damper_histogram(_lap(n=50), susp.SOURCE_WORKER)
    assert hist["available"] is False
    assert "need" in hist["reason"]
    print(" ", hist["reason"])


def test_effective_rate_is_measured_not_assumed():
    fast = susp.summarise(_lap(rate_hz=333), susp.SOURCE_WORKER)
    slow = susp.summarise(_lap(rate_hz=60), susp.SOURCE_APP)
    assert 300 < fast["sample_rate_hz"] < 400, fast["sample_rate_hz"]
    assert 50 < slow["sample_rate_hz"] < 70, slow["sample_rate_hz"]
    print(f"  measured {fast['sample_rate_hz']}Hz and "
          f"{slow['sample_rate_hz']}Hz")


def test_duplicate_physics_frames_do_not_invent_velocity():
    """The same physics step observed twice has dt = 0.

    Dividing by that would produce an infinite damper velocity, or with a
    wall clock, a very large fictional one.
    """
    samples = _lap(n=400)
    doubled = []
    for s in samples:
        doubled.append(s)
        doubled.append(dict(s))          # identical timestamp
    hist = susp.damper_histogram(doubled, susp.SOURCE_WORKER)
    baseline = susp.damper_histogram(samples, susp.SOURCE_WORKER)
    assert hist["axles"]["front"]["peak_mm_s"] == \
        baseline["axles"]["front"]["peak_mm_s"]
    print("  duplicate frames ignored, peak unchanged")


# --- ride height --------------------------------------------------------


def test_ride_height_reports_spread_and_where_it_is_lowest():
    report = susp.ride_height_report(_lap())
    assert report["available"] is True
    # Planted: front sits at 50mm, dropping to 40mm under braking.
    assert abs(report["front_mm"]["max"] - 50.0) < 0.5, report["front_mm"]
    assert abs(report["front_mm"]["min"] - 40.0) < 0.5, report["front_mm"]
    # Rake is rear minus front, so it grows as the nose drops.
    assert report["rake_mm"]["max"] > report["rake_mm"]["min"]
    # Front and rear are ranked separately: the front is almost always the
    # lower of the two, so a shared ranking would hide the rear entirely.
    lows = report["lowest_points"]
    assert lows["front"] and lows["rear"], lows
    lowest = lows["front"][0]
    assert any(lo <= lowest["spline"] <= hi
               for lo, hi in ((0.13, 0.24), (0.53, 0.64))), lowest
    print(f"  front {report['front_mm']['min']}-{report['front_mm']['max']}mm,"
          f" lowest at spline {lowest['spline']}")


def test_ride_height_survives_missing_channels():
    samples = [dict(s) for s in _lap(n=300)]
    for s in samples:
        s.pop("ride_f")
    assert susp.ride_height_report(samples)["available"] is False
    print("  no ride height -> reported unavailable, not a crash")


# --- roll balance -------------------------------------------------------


def test_front_load_transfer_share_is_computed():
    roll = susp.roll_balance(_lap())
    assert roll["available"] is True
    # Planted: 1800N of front transfer against 400N rear -> ~82% front.
    assert 78 < roll["front_load_transfer_pct"] < 86, roll
    print(f"  front takes {roll['front_load_transfer_pct']}% of the transfer")


def test_straight_line_running_is_not_counted_as_load_transfer():
    """In a straight line the left/right difference is noise.

    Averaging it in drags every session toward a meaningless 50%.
    """
    flat = _lap(brake_zones=())
    for s in flat:
        for w in ("fl", "fr", "rl", "rr"):
            s[f"load_{w}"] = 3500.0
    roll = susp.roll_balance(flat)
    assert roll["available"] is False, roll
    assert "cornering" in roll["reason"]
    print(" ", roll["reason"])


# --- top level and storage ---------------------------------------------


def test_summarise_covers_all_three_questions():
    out = susp.summarise(_lap(), susp.SOURCE_WORKER)
    assert out["available"] is True
    assert out["dampers"]["available"] is True
    assert out["ride_height"]["available"] is True
    assert out["roll"]["available"] is True
    print("  dampers, ride height and roll all reported")


def test_compact_summary_is_small_enough_for_lap_summary():
    import json
    compact = susp.compact(susp.summarise(_lap(), susp.SOURCE_WORKER))
    size = len(json.dumps(compact))
    assert size < 400, f"{size} bytes is too much for lap_summary's budget"
    assert compact["source"] == susp.SOURCE_WORKER
    assert "front_load_transfer_pct" in compact
    print(f"  compact summary is {size} bytes")


def test_storage_round_trip_and_tier_separation():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        app_rows = _lap(rate_hz=60, n=300)
        worker_rows = _lap(rate_hz=333, n=900)

        assert db.store_suspension_batch(conn, sid, "app", app_rows) == 300
        assert db.store_suspension_batch(conn, sid, "worker",
                                         worker_rows) == 900
        # A resent batch must not duplicate.
        assert db.store_suspension_batch(conn, sid, "app", app_rows) == 0

        assert db.best_suspension_source(conn, sid, 0) == "worker"
        got = db.get_suspension_samples(conn, sid, 0)
        # Both tiers come back: they carry different channels, and the
        # analysis is what keeps them apart.
        assert len(got) == 1200, len(got)
        only_app = db.get_suspension_samples(conn, sid, 0, "app")
        assert len(only_app) == 300
        print("  both tiers stored and retrievable, separable by source")
        conn.close()


def test_the_two_tiers_share_a_clock_without_colliding():
    """Worker t_ms counts from worker start; app t_ms is car.timestamp.

    Two clocks, both landing on ~3ms multiples. With `source` in the primary
    key they coexist; without it, INSERT OR IGNORE silently drops whichever
    arrives second -- and that is the app row, the only one carrying ride
    height and wheel loads.
    """
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        # Deliberately identical timestamps in both tiers.
        rows = _lap(rate_hz=333, n=400)
        assert db.store_suspension_batch(conn, sid, "worker", rows) == 400
        assert db.store_suspension_batch(conn, sid, "app", rows) == 400
        assert conn.execute(
            "SELECT COUNT(*) FROM suspension_samples").fetchone()[0] == 800
        print("  identical timestamps coexist across tiers")
        conn.close()


def test_analysis_never_differentiates_across_a_tier_boundary():
    """The failure this design exists to prevent.

    The two tiers read different APIs with independently undocumented zero
    points. Differentiating across the seam turns the offset between them
    into a single enormous velocity, which lands in the high-speed bump bin
    and reads exactly like valving that packs down over kerbs.
    """
    worker = [dict(s, source="worker") for s in _lap(rate_hz=333, n=900)]
    # Same motion, but 30mm of zero-point offset and a slower clock.
    app = [dict(s, source="app",
                travel_fl=s["travel_fl"] + 0.030,
                travel_fr=s["travel_fr"] + 0.030,
                travel_rl=s["travel_rl"] + 0.030,
                travel_rr=s["travel_rr"] + 0.030)
           for s in _lap(rate_hz=60, n=300)]

    mixed = susp.summarise(worker + app)
    clean = susp.summarise(worker)

    assert mixed["source"] == "worker", mixed["source"]
    assert mixed["dampers"]["axles"]["front"]["peak_bump_mm_s"] == \
        clean["dampers"]["axles"]["front"]["peak_bump_mm_s"], (
            "app rows leaked into the worker histogram")
    assert mixed["tiers_present"] == {"worker": 900, "app": 300}, mixed
    assert 300 < mixed["sample_rate_hz"] < 400, mixed["sample_rate_hz"]
    print("  histogram unchanged by 300 offset app rows;",
          mixed["tiers_present"])


def test_ride_height_still_comes_from_the_render_rate_rows():
    """The worker cannot see ride height or wheel loads at all.

    So when both tiers are present, the damper analysis must use the worker
    rows and everything else must use the app rows -- taking "the best
    source" for all of it would throw away the only rows that have them.
    """
    worker = [dict(s, source="worker", ride_f=None, ride_r=None,
                   load_fl=None, load_fr=None, load_rl=None, load_rr=None)
              for s in _lap(rate_hz=333, n=900)]
    app = [dict(s, source="app") for s in _lap(rate_hz=60, n=300)]

    out = susp.summarise(worker + app)
    assert out["dampers"]["available"] is True
    assert out["ride_height"]["available"] is True, out["ride_height"]
    assert out["roll"]["available"] is True, out["roll"]
    print("  dampers from the worker, ride height and roll from the app rows")


def test_a_mislabelled_tier_is_caught_by_its_own_rate():
    """A worker that never produces would have its fallback labelled 333Hz.

    The client sets the label, so the label is checked against the evidence:
    rows claiming to be physics-rate that arrived at 10Hz get the same
    warning as render-rate data rather than a clean bill of health.
    """
    lying = [dict(s, source="worker") for s in _lap(rate_hz=10, n=400)]
    out = susp.summarise(lying)
    assert out["dampers"]["available"] is True
    assert "caution" in out["dampers"], out["dampers"]
    assert "mislabelling" in out["dampers"]["caution"]
    assert out["dampers"]["measured_rate_hz"] < 20
    print(" ", out["dampers"]["caution"][:60] + "...")


def test_lua_and_python_batch_limits_agree():
    """Bind the constants that must match, rather than trusting a comment.

    A WORKER_RING that disagrees with the worker's BUFFER corrupts every
    drained sample silently, and a client buffer above the server's cap
    means every POST is rejected.
    """
    from ac_race_engineer import bridge as B
    lua_dir = Path(__file__).resolve().parents[1] / "lua_app/race_engineer"
    app = (lua_dir / "race_engineer.lua").read_text(encoding="utf-8")
    wrk = (lua_dir / "suspension_worker.lua").read_text(encoding="utf-8")

    def const(text, name):
        for line in text.splitlines():
            if line.strip().startswith(f"local {name}") and "=" in line:
                return int(line.split("=")[1].split("--")[0].strip())
        raise AssertionError(f"{name} not found")

    ring = const(app, "WORKER_RING")
    buf = const(wrk, "BUFFER")
    client_max = const(app, "SUSP_BUFFER_MAX")
    assert ring == buf, f"WORKER_RING={ring} but worker BUFFER={buf}"
    assert client_max <= B.MAX_SUSPENSION_BATCH, (
        f"client buffers {client_max} but the bridge caps at "
        f"{B.MAX_SUSPENSION_BATCH}, so every full POST would be rejected")
    print(f"  ring {ring} == buffer {buf}; client {client_max} <= server "
          f"{B.MAX_SUSPENSION_BATCH}")


def test_the_lua_app_assigns_no_implicit_globals():
    """Lua creates a global on assignment to an undeclared name.

    That is silent, and it bites twice: the value leaks into the shared
    environment, and a typo or a leftover from a refactor reads back as nil
    forever. This exact bug shipped in review -- a `suspTier` variable
    survived the change that replaced it, was never declared and never set
    to 'worker', so the app's capture-tier indicator would have shown the
    degraded marker permanently no matter what was actually running.
    """
    # This is the only check for this bug class, so "luaparser is missing"
    # must not read as "passed". require() raises under AC_TESTS_STRICT --
    # CI and run_tests.py --no-skip -- and returns True on a machine that
    # genuinely lacks it, where skipping is the right answer.
    import lua_harness
    if lua_harness.require("luaparser", "the implicit-globals check"):
        # Raises for pytest and run_tests.py, prints for the bare standalone
        # runner, which has no notion of a skip -- hence the return.
        lua_harness.skip("pip install luaparser for the implicit-globals check")
        return

    from luaparser import ast, astnodes

    lua_dir = Path(__file__).resolve().parents[1] / "lua_app/race_engineer"
    # Names Lua and CSP provide. Anything assigned that isn't declared local
    # and isn't one of these is a new global.
    provided = {
        "script", "windowMain", "windowSettings",       # CSP entry points
        "ac", "ui", "web", "physics", "sim", "car",     # CSP namespaces
        "JSON", "rgbm", "vec2", "vec3", "worker",
        "math", "string", "table", "os", "io", "tostring", "tonumber",
        "type", "pcall", "ipairs", "pairs", "print", "select", "setmetatable",
    }

    offenders = []
    for path in sorted(lua_dir.glob("*.lua")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        declared = set(provided)
        for node in ast.walk(tree):
            # `local x`, `local function f`, function params, loop vars.
            if isinstance(node, astnodes.LocalAssign):
                declared.update(t.id for t in node.targets
                                if isinstance(t, astnodes.Name))
            elif isinstance(node, astnodes.LocalFunction):
                if isinstance(node.name, astnodes.Name):
                    declared.add(node.name.id)
            elif isinstance(node, (astnodes.Function, astnodes.AnonymousFunction)):
                declared.update(a.id for a in getattr(node, "args", [])
                                if isinstance(a, astnodes.Name))
            elif isinstance(node, astnodes.Fornum):
                declared.add(node.target.id)
            elif isinstance(node, astnodes.Forin):
                declared.update(t.id for t in node.targets
                                if isinstance(t, astnodes.Name))

        for node in ast.walk(tree):
            if not isinstance(node, astnodes.Assign):
                continue
            for target in node.targets:
                # Only bare names create globals; a.b = 1 does not.
                if isinstance(target, astnodes.Name) and \
                        target.id not in declared:
                    offenders.append(f"{path.name}: {target.id}")

    assert not offenders, (
        "assignment to undeclared name(s) creates a global:\n  "
        + "\n  ".join(sorted(set(offenders))))
    print(f"  no implicit globals in {len(list(lua_dir.glob('*.lua')))} files")


def test_the_shared_struct_layouts_are_identical():
    """The app and the worker map the same memory.

    A field added to one and not the other silently misaligns every
    subsequent field -- damper travel read as a spline position, say. There
    is no error for this at runtime, just wrong numbers.
    """
    lua_dir = Path(__file__).resolve().parents[1] / "lua_app/race_engineer"

    def struct(name):
        text = (lua_dir / name).read_text(encoding="utf-8")
        i = text.index("ac.connect(")
        depth, j = 0, i
        while True:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        lines = [l.split("--")[0].strip() for l in text[i:j + 1].splitlines()]
        # The ring size is spelled with a different constant name on each
        # side; its value is pinned by the test above.
        return [l.replace("WORKER_RING", "N").replace("BUFFER", "N")
                for l in lines if l]

    app = struct("race_engineer.lua")
    wrk = struct("suspension_worker.lua")
    assert app == wrk, "\n".join(
        f"  app: {a!r}\n  wrk: {b!r}" for a, b in zip(app, wrk) if a != b)
    assert "ac.StructItem.key('ac_race_engineer.suspension')" in app[1]
    print(f"  {len(app)} lines, identical on both sides")


def test_out_of_range_fields_are_reported_not_just_nulled():
    """A units disagreement must read as one, not as an empty report."""
    from support import post
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        br, _ = _bridge(path, conn)
        try:
            # Millimeters where meters were expected: every travel field is
            # out of range, so every one becomes null.
            rows = [dict(s, travel_fl=s["travel_fl"] * 1000,
                         travel_fr=s["travel_fr"] * 1000)
                    for s in _lap(n=30)]
            rows = [dict(r, travel_fl=4000.0, travel_fr=4000.0) for r in rows]
            code, body = post(br.port, "/suspension",
                              {"source": "app", "samples": rows})
            assert code == 200, body
            assert body["stored"] == 30
            assert body["rejected_fields"]["travel_fl"] == 30, body
            assert "meters" in body["hint"]
            print("  rejected fields reported:", body["rejected_fields"])
        finally:
            br.stop()
            conn.close()


def test_pruning_keeps_recent_laps():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        for lap in range(30):
            rows = [dict(s, lap_count=lap) for s in _lap(n=100)]
            db.store_suspension_batch(conn, sid, "worker", rows)
        removed = db.prune_suspension_samples(conn, sid, keep_laps=5)
        assert removed > 0
        laps = {r["lap_count"] for r in db.suspension_lap_counts(conn, sid)}
        assert max(laps) == 29 and len(laps) == 5, sorted(laps)
        print(f"  pruned {removed} rows, kept laps {sorted(laps)}")
        conn.close()


# --- the wire, end to end ----------------------------------------------


def _bridge(path, conn):
    import time
    from ac_race_engineer import bridge as B
    from support import FakeCollector
    sid = make_session(conn)
    br = B.Bridge(path, FakeCollector(session_id=sid, running=True), 0)
    br.start()
    time.sleep(0.2)
    assert br.error is None, br.error
    return br, sid


def test_samples_survive_the_round_trip_through_http():
    """The wire format the Lua app has to produce, exercised not assumed."""
    from support import post
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        br, sid = _bridge(path, conn)
        try:
            rows = _lap(rate_hz=333, n=600)
            code, body = post(br.port, "/suspension",
                              {"source": "worker", "samples": rows})
            assert code == 200 and body["ok"], body
            assert body["stored"] == 600, body

            stored = db.get_suspension_samples(conn, sid, 0)
            assert len(stored) == 600
            out = susp.summarise(stored, "worker")
            assert out["dampers"]["available"] is True
            assert out["dampers"]["sign_convention"]["sign"] == 1
            print("  600 samples posted, stored and analyzed")
        finally:
            br.stop()
            conn.close()


def test_bad_source_is_refused():
    """The tier decides which analyzes are honest, so it cannot be guessed."""
    from support import post
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        br, _ = _bridge(path, conn)
        try:
            for bad in (None, "", "physics", 1, {"a": 1}):
                code, _ = post(br.port, "/suspension",
                               {"source": bad, "samples": []})
                assert code == 400, (bad, code)
            code, _ = post(br.port, "/suspension", {"samples": []})
            assert code == 400
            print("  unknown source refused")
        finally:
            br.stop()
            conn.close()


def test_one_bad_sample_does_not_cost_the_batch():
    from support import post
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        br, _ = _bridge(path, conn)
        try:
            rows = _lap(n=300)[:10]
            rows.append({"lap_count": "nope", "t_ms": 1, "spline": 0.5})
            rows.append({"t_ms": 2})               # missing fields
            rows.append("not even a dict")
            # Absurd values are dropped to null, not stored as-is.
            rows[0] = dict(rows[0], load_fl=1e30, travel_fl=99.0)
            code, body = post(br.port, "/suspension",
                              {"source": "app", "samples": rows})
            assert code == 200, body
            assert body["stored"] == 10, body
            assert body["skipped"] == 3, body
            first = conn.execute(
                "SELECT load_fl, travel_fl FROM suspension_samples"
                " ORDER BY t_ms LIMIT 1").fetchone()
            assert first["load_fl"] is None and first["travel_fl"] is None
            print("  10 stored, 3 skipped, absurd values nulled")
        finally:
            br.stop()
            conn.close()


def test_batch_is_refused_when_nothing_is_recording():
    import time
    from ac_race_engineer import bridge as B
    from support import FakeCollector, age_session, post
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        stale = make_session(conn)
        age_session(conn, stale, 3 * 86400)
        br = B.Bridge(path, FakeCollector(running=False), 0)
        br.start()
        time.sleep(0.2)
        try:
            code, body = post(br.port, "/suspension",
                              {"source": "app", "samples": _lap(n=20)})
            assert code == 200 and body["ok"] is False, body
            # No `stored` key at all. The Lua client keys its success
            # accounting off `ok`, not off the presence of `stored` --
            # treating a missing `stored` as success counted these as sent
            # and showed a healthy status line over discarded data.
            assert "stored" not in body, body
            assert conn.execute(
                "SELECT COUNT(*) FROM suspension_samples").fetchone()[0] == 0
            print("  refused with ok=false and no 'stored' key")
        finally:
            br.stop()
            conn.close()


def test_concurrent_posts_do_not_lose_prune_increments():
    """The bridge is threaded, so two tiers can post at the same moment.

    `counter += 1` is a read and a write. A lost increment makes the
    periodic sweep drift, and if collisions keep landing on the same residue
    it can stop firing altogether -- on the highest-volume table in the
    database.
    """
    import threading
    import time as _time
    from support import post, temp_db
    with temp_db() as path:
        conn = db.connect(path)
        br, _ = _bridge(path, conn)
        try:
            attempts, threads = 24, []
            rows = _lap(n=5)
            outcomes: list[int | None] = []
            guard = threading.Lock()

            def already_stored(i):
                """Did attempt i's batch reach the table?

                Its own connection, because this runs on a worker thread and
                SQLite objects do not cross threads.
                """
                probe = db.connect(path)
                try:
                    return probe.execute(
                        "SELECT COUNT(*) FROM suspension_samples"
                        " WHERE lap_count = ?", (i,)).fetchone()[0] > 0
                finally:
                    probe.close()

            def fire(i):
                # Distinct lap_counts so nothing is deduplicated away and
                # every request that lands really does write.
                batch = [dict(s, lap_count=i) for s in rows]
                code = None
                for attempt in range(4):
                    try:
                        code, _ = post(br.port, "/suspension",
                                       {"source": "app", "samples": batch})
                        break
                    except OSError:
                        # The OS refused or reset the connection, which a
                        # loaded Windows runner does under two dozen
                        # simultaneous connects. That is not the server
                        # losing a write, so retry rather than record a
                        # miss: recording it is what made this a CI flake,
                        # failing the quorum check below at 5 of 24.
                        #
                        # Unless the request was served and only the reply
                        # was lost -- sending it again would write twice and
                        # be counted once, failing the invariant under test
                        # for a reason that has nothing to do with it.
                        if already_stored(i):
                            code = 200
                            break
                        _time.sleep(0.05 * (attempt + 1))
                with guard:
                    outcomes.append(code)

            for i in range(attempts):
                t = threading.Thread(target=fire, args=(i,))
                threads.append(t)
                t.start()
            for t in threads:
                t.join()

            landed = sum(1 for c in outcomes if c == 200)
            # Guard against the test passing by doing nothing: if almost
            # nothing landed there was no concurrency to speak of and the
            # assertion below proves little. With the retry above this is a
            # floor nothing should reach, not a coin toss.
            assert landed >= attempts // 2, (
                f"only {landed} of {attempts} posts landed even with "
                f"retries; too few to say anything about concurrent "
                f"increments")
            # The honest invariant: the counter matches the requests the
            # server actually handled, not the ones the client attempted.
            assert br._susp_writes == landed, (
                f"counted {br._susp_writes} writes for {landed} handled "
                f"posts -- increments were lost")
            stored = conn.execute(
                "SELECT COUNT(*) FROM suspension_samples").fetchone()[0]
            assert stored == landed * len(rows), stored
            print(f"  {landed}/{attempts} posts landed, all counted, "
                  f"{stored} rows")
        finally:
            br.stop()
            conn.close()


def test_the_table_appears_on_an_upgraded_database():
    """A user with an existing telemetry.db must get the new table."""
    import sqlite3
    import time
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "old.db"
        raw = sqlite3.connect(path)
        raw.executescript(
            "CREATE TABLE sessions (id INTEGER PRIMARY KEY,"
            " started_at REAL NOT NULL, car TEXT NOT NULL,"
            " track TEXT NOT NULL, track_config TEXT NOT NULL DEFAULT '',"
            " tyre_compound TEXT NOT NULL DEFAULT '', air_temp REAL,"
            " road_temp REAL, setup_name TEXT NOT NULL DEFAULT '');"
            "CREATE TABLE laps (id INTEGER PRIMARY KEY,"
            " session_id INTEGER NOT NULL, lap_number INTEGER NOT NULL,"
            " lap_time_ms INTEGER NOT NULL, valid INTEGER NOT NULL DEFAULT 1,"
            " completed_at REAL NOT NULL);")
        raw.execute("INSERT INTO sessions (started_at, car, track)"
                    " VALUES (?,?,?)", (time.time(), "carx", "mugello"))
        raw.commit()
        raw.close()

        conn = db.connect(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == \
            db.SCHEMA_VERSION
        n = db.store_suspension_batch(conn, 1, "app", _lap(n=50))
        assert n == 50
        assert len(db.get_suspension_samples(conn, 1, 0)) == 50
        print("  suspension table created on an existing database")
        conn.close()


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
