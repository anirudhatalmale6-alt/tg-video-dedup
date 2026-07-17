#!/bin/sh
# Records the demo walkthrough to demo_walkthrough.mp4 (headless via Xvfb).
cd /var/lib/freelancer/projects/40588409
export DISPLAY=:99
rm -f demo_walkthrough.mp4
Xvfb :99 -screen 0 1060x720x24 >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
sleep 2
ffmpeg -y -f x11grab -video_size 1060x700 -framerate 15 -i :99.0+0+0 \
       -t 22 -pix_fmt yuv420p -vcodec libx264 -movflags +faststart \
       demo_walkthrough.mp4 >/tmp/ffmpeg.log 2>&1 &
FF_PID=$!
sleep 1
python3 demo.py >/tmp/demo.log 2>&1
sleep 1
kill -INT $FF_PID 2>/dev/null
wait $FF_PID 2>/dev/null
kill $XVFB_PID 2>/dev/null
echo "done"
