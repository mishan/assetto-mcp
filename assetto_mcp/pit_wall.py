"""The pit wall without a chat: the recorder and the dashboard pages, for a
stream or a tablet, until Ctrl+C.

A chat opened alongside works as usual. The instances share the database,
so the engineer can still put laps on screen and read the driver's
questions.
"""

import signal
import sys
import time


def main() -> None:
    from . import bridge
    # Left running all evening, so it waits out a chat's server holding the
    # port rather than giving up after a few minutes.
    bridge.BIND_RETRY_SECONDS = float("inf")
    from . import server  # importing it starts the recorder and the bridge

    base = f"http://127.0.0.1:{server._bridge.port}/dash"
    print(f"Pit wall:  {base}\nOverlay:   {base}/overlay")
    if server._bridge.lan_url:
        print(f"Phone:     {server._bridge.lan_url}")
    if server._bridge.error:
        print(server._bridge.error)
    print("Ctrl+C to stop.", flush=True)
    # A service manager or `kill` stops it with SIGTERM; the recorder claim
    # still has to be handed back on the way out.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        # Not Event.wait(): on Windows that blocks Ctrl+C.
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server._shutdown()


if __name__ == "__main__":
    main()
