"""python -m tracker {live,replay,build-track,validate}"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

log = logging.getLogger("tracker")


def load_replay(args):
    """Returns (messages, meta, source_name)."""
    if args.file:
        from .sources import recording
        return recording.load(args.file), {}, "recording"
    if args.source in ("auto", "archive"):
        from .sources import archive
        try:
            msgs, path = archive.load(args.year, args.event, args.session)
            return msgs, {"path": path}, "archive"
        except archive.ArchiveUnavailable as e:
            if args.source == "archive":
                raise
            log.warning("%s", e)
            log.warning("falling back to OpenF1")
    from .sources import openf1_replay
    msgs, meta = openf1_replay.load(args.year, args.event, args.session, latency=args.latency)
    return msgs, meta, "openf1"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tracker", description="F1 live position estimator")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    lv = sub.add_parser("live", help="connect to the live timing feed")
    lv.add_argument("--port", type=int, default=8000)
    lv.add_argument("--track", help="track model name in tracks/ (default: from SessionInfo)")

    def replay_args(sp):
        sp.add_argument("--year", type=int, default=2025)
        sp.add_argument("--event", default="São Paulo Grand Prix")
        sp.add_argument("--session", default="Race")
        sp.add_argument("--source", choices=["auto", "archive", "openf1"], default="auto",
                        help="archive = F1 static archive (spec), openf1 = rebuilt from OpenF1")
        sp.add_argument("--file", help="replay a raw recording from recordings/ instead")
        sp.add_argument("--latency", type=float, default=1.0,
                        help="openf1 source: seconds of feed latency added to timing messages")
        sp.add_argument("--track", help="track model name in tracks/")

    rp = sub.add_parser("replay", help="replay a past session through the same pipeline")
    replay_args(rp)
    rp.add_argument("--speed", type=float, default=5.0)
    rp.add_argument("--start", type=float, default=-1.0,
                    help="minutes relative to the race start to begin (default -1)")
    rp.add_argument("--port", type=int, default=8000)

    vp = sub.add_parser("validate", help="estimate vs real positions, per-lap error stats + plot")
    replay_args(vp)

    bt = sub.add_parser("build-track", help="build tracks/<circuit>.json from OpenF1")
    bt.add_argument("--circuit", default="Interlagos")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")

    if args.cmd == "build-track":
        from .trackbuild import build_track
        tr = build_track(args.circuit)
        path = tr.save()
        print(f"saved {path}: length {tr.length:.0f} m, ref lap {tr.lap_time:.3f} s, gates "
              f"{[round(g) for g in tr.gates]}, pit entry {tr.pit_entry_s:.0f} exit {tr.pit_exit_s:.0f}")
        return 0

    if args.cmd == "live":
        from .server import run_live
        try:
            asyncio.run(run_live(args.track, args.port))
        except KeyboardInterrupt:
            pass
        return 0

    from .engine import Engine, track_for_session
    from .state import SessionState
    from .track import Track

    msgs, meta, src = load_replay(args)
    st = SessionState()
    for t, topic, data, initial in msgs:
        if initial and topic == "SessionInfo":
            st.load_initial(topic, data)
    track = Track.load(track_for_session(st, args.track))
    log.info("%d messages from %s, track %s", len(msgs), src, track.name)

    if args.cmd == "validate":
        from .validate import run_validation
        return run_validation(msgs, meta, src, track, args)

    from .server import run_replay
    t_start = meta.get("t_start") or next((m[0] for m in msgs if m[1] == "SessionStatus"
                                           and (m[2] or {}).get("Status") == "Started"), msgs[0][0])
    start = max(msgs[0][0], t_start + args.start * 60)
    try:
        asyncio.run(run_replay(Engine(track), msgs, start, args.speed, args.port))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
