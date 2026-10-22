#!/bin/sh
# The README's pictures of the pit wall and the stream overlay, taken with
# shotbox (https://github.com/mishan/shotbox) in a sealed session from
# scripts/pit_wall_demo.py. Arguments go to the demo, e.g. --session 41.
#
#   scripts/shoot_pit_wall.sh --db ~/.assetto-mcp/telemetry.db
set -e
cd "$(dirname "$0")/.."
browser=${CHROME:-google-chrome}
demo="python3 $PWD/scripts/pit_wall_demo.py $*"

# shoot OUT PAGE WxH TITLE [shotbox options]
shoot() {
  out=$1 page=$2 size=$3 title=$4; shift 4
  shotbox shoot "docs/$out" --dark --net loopback --wait "window:$title" \
    --settle 5 "$@" -- sh -c "
      $demo > \"\$SHOTBOX_SCRATCH/demo.log\" 2>&1 &
      until grep -q http \"\$SHOTBOX_SCRATCH/demo.log\"; do sleep 0.2; done
      exec $browser --no-first-run --no-default-browser-check \
        --force-dark-mode --user-data-dir=\"\$HOME/chrome\" \
        --window-position=0,0 --window-size=$size \
        --app=http://127.0.0.1:19666/$page"
}

shoot pit-wall.png dash 1440,1000 'Pit Wall' --screen 1440x1000
shoot overlay.png dash/overlay 1280,230 'Pit Wall Overlay' \
  --screen 1280x800 --crop 1280x230+0+0
