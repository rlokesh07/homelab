#!/bin/sh
# Builds kodi.c for this computer and runs it against a fake Kodi.
set -eu
cd "$(dirname "$0")"

PORT=18080
TMP=$(mktemp -d)
trap 'kill "$SERVER" 2>/dev/null; rm -rf "$TMP"' EXIT

cc -Wall -Wno-format-truncation -I../source -o "$TMP/host_test" host_test.c ../source/kodi.c ../source/cJSON.c
python3 fake_kodi.py "$PORT" "$TMP/calls.jsonl" &
SERVER=$!
sleep 1

"$TMP/host_test" "$PORT"

python3 - "$TMP/calls.jsonl" <<'EOF'
import json, sys
calls = [json.loads(line) for line in open(sys.argv[1])]
by_method = {}
for call in calls:
    by_method.setdefault(call["method"], []).append(call.get("params"))
expected = {
    "Player.Open": [{"item": {"episodeid": 70}}, {"item": {"movieid": 2}}],
    "Player.Seek": [{"playerid": 1, "value": {"percentage": 42.5}}],
    "Player.PlayPause": [{"playerid": 1}],
    "Player.Stop": [{"playerid": 1}],
    "Input.ExecuteAction": [{"action": "volumeup"}],
    "VideoLibrary.GetEpisodes": [{"tvshowid": 7, "season": 2, "sort": {"method": "episode"}}],
}
bad = {m: (by_method.get(m), want) for m, want in expected.items() if by_method.get(m) != want}
for method, (got, want) in bad.items():
    print(f"FAIL {method}: sent {got}, expected {want}")
print("requests match" if not bad else "requests differ")
sys.exit(1 if bad else 0)
EOF
