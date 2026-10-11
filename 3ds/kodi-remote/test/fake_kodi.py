"""Minimal stand-in for Kodi's JSON-RPC web server, for testing kodi.c on a computer.

Usage: fake_kodi.py PORT CALL_LOG
Every request is appended to CALL_LOG as one JSON line.
"""
import base64
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

AUTH = "Basic " + base64.b64encode(b"kodi:secret").decode()
LONG_TITLE = "The Extraordinarily Long Title of a Movie That Will Never Fit On a 3DS Screen " * 3

MOVIES = [{"movieid": 1, "label": "Amélie", "year": 2001}, {"movieid": 2, "label": LONG_TITLE, "year": 0}]
MOVIES += [{"movieid": 100 + i, "label": f"Generated Movie {i}", "year": 1990 + i % 30} for i in range(1500)]
SHOWS = [{"tvshowid": 7, "label": "Severance"}]
SEASONS = {7: [{"season": 1, "label": "Season 1"}, {"season": 2, "label": "Season 2"}]}
EPISODES = {(7, 2): [{"episodeid": 70, "label": "2x01. Hello, Ms. Cobel"}, {"episodeid": 71, "label": "2x02. Goodbye, Mrs. Selvig"}]}

state = {"playing": None}


def result_for(method, params):
    if method == "VideoLibrary.GetMovies":
        return {"movies": MOVIES, "limits": {"total": len(MOVIES)}}
    if method == "VideoLibrary.GetTVShows":
        return {"tvshows": SHOWS}
    if method == "VideoLibrary.GetSeasons":
        return {"seasons": SEASONS.get(params["tvshowid"], [])}
    if method == "VideoLibrary.GetEpisodes":
        return {"episodes": EPISODES.get((params["tvshowid"], params["season"]), [])}
    if method == "Player.Open":
        state["playing"] = params["item"]
        return "OK"
    if method == "Player.GetActivePlayers":
        return [{"playerid": 1, "type": "video"}] if state["playing"] else []
    if method == "Player.GetProperties":
        return {"percentage": 25.5, "speed": 0,
                "time": {"hours": 0, "minutes": 12, "seconds": 3, "milliseconds": 0},
                "totaltime": {"hours": 1, "minutes": 2, "seconds": 0, "milliseconds": 0}}
    if method == "Player.GetItem":
        return {"item": {"label": "Hello, Ms. Cobel", "showtitle": "Severance", "type": "episode"}}
    if method in ("Player.Seek", "Player.PlayPause", "Player.Stop", "Input.ExecuteAction", "Input.Home"):
        return "OK"
    return None


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.headers.get("Authorization") != AUTH:
            self.send_response(401)
            self.end_headers()
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with open(sys.argv[2], "a") as log:
            log.write(json.dumps(request) + "\n")
        result = result_for(request["method"], request.get("params", {}))
        if result is None:
            response = {"id": 1, "jsonrpc": "2.0", "error": {"code": -32601, "message": "Method not found."}}
        else:
            response = {"id": 1, "jsonrpc": "2.0", "result": result}
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
