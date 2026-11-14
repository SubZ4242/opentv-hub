#!/usr/bin/env python3
"""
OpenTV Aufnahmen: records TV Abo games on the NAS while they run and serves them to the Fire TVs.

The TVs send their plan (POST /api/plan): the games of their TV Abo with the one Dispatcharr channel
TV Abo shows each on. A few minutes before kick-off this service records that channel (ffmpeg,
stream copy, HLS with growing playlist) and, when it stalls, connects to the same channel again:
one version per game, the one the TV would play. (Until 8.11.2026 it took the best of all versions
and switched between them; beIN SPORTS 1's 4K version, not TV Abo's, had its sound ahead of the
picture, and a game ended up in two versions.) The recording stops after ESPN calls the game over and is deleted two hours later.

ESPN's key events (goals, red cards, halves) are fetched while the game runs; their wall clock plus
the stream's delay behind real time gives their place in the recording.

Only the standard library and ffmpeg/ffprobe. Settings and state live in /data.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "35090"))
DATA = os.environ.get("DATA", "/data")
ROOT = os.environ.get("ROOT", "/app/recordings")
DEFAULT_DIR = os.environ.get("REC_DIR", "/app/recordings")
# Dispatcharr inside the home network (its streams answer there only). As a hub setting; without
# one, the address the TVs send with their plans.
DISPATCHARR = os.environ.get("DISPATCHARR", "").rstrip("/")
# Seconds a channel's picture arrives behind the real game, measured 7.11.2026 against ESPN's
# clock and the scoreboard in the picture: beIN SPORTS 3 25 s, S Sport 1 53 s. Others: DELAY.
DELAY = float(os.environ.get("DELAY", "40"))
DELAYS = [(r"s ?sport", 53), (r"bein", 25)]


def delay_of(channel):
    n = (channel or "").lower()
    for pattern, d in DELAYS:
        if re.search(pattern, n):
            return d
    return DELAY
PRE = 5 * 60          # start this long before kick-off
KEEP = 2 * 3600 * 1000  # keep this long after the end (ms)
SEGMENT = 3           # HLS segment target, seconds
STALL = 20            # no new segment for this long: connect again
ESPN = "https://site.api.espn.com/apis/site/v2/sports/"
MARKER = ".opentv-aufnahme"

lock = threading.RLock()
state = {"plans": {}, "recordings": {}, "dir": DEFAULT_DIR, "dispatcharr": DISPATCHARR}
workers = {}


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def now_ms():
    return int(time.time() * 1000)


def save():
    with lock:
        tmp = os.path.join(DATA, "state.json.tmp")
        with open(tmp, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(DATA, "state.json"))


def load():
    try:
        with open(os.path.join(DATA, "state.json")) as f:
            state.update(json.load(f))
    except (OSError, ValueError):
        pass
    # The hub's setting wins over what an older state remembers.
    if DISPATCHARR:
        state["dispatcharr"] = DISPATCHARR


def get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "OpenTV-Aufnahmen/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def safe_dir(d):
    """The folder for recordings: under the internal pool only (the container sees nothing else)."""
    d = os.path.normpath(d or DEFAULT_DIR)
    return d if d == ROOT or d.startswith(ROOT + "/") else DEFAULT_DIR


def folder_name(game):
    day = datetime.fromtimestamp(game["start"] / 1000).strftime("%Y-%m-%d %H%M")
    title = re.sub(r'[\\/:*?"<>|]+', " ", game.get("title", "Spiel")).strip()
    return f"{day} {title}"[:120]


# ---------------------------------------------------------------- ESPN

def summary(rec):
    sport = rec.get("sport") or "soccer"
    league = rec.get("league") or "all"
    return get_json(f"{ESPN}{sport}/{league}/summary?event={rec['id']}")


def wall(s):
    try:
        return int(datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


MARKS = {"kickoff": "Anstoß", "halftime": "Halbzeit", "start-2nd-half": "2. Halbzeit",
         "end-regular-time": "Ende 90 Min.", "start-extra-time": "Verlängerung", "start-shootout": "Elfmeterschießen",
         "full-time": "Abpfiff", "end-of-game": "Abpfiff"}


def events_of(js):
    """Goals, red cards and the halves from ESPN's summary, in order, each with its wall clock."""
    out, home, away = [], 0, 0
    comp = (js.get("header") or {}).get("competitions", [{}])[0]
    sides = {}
    for c in comp.get("competitors", []):
        sides[str((c.get("team") or {}).get("id"))] = c.get("homeAway")
    for k in js.get("keyEvents", []):
        t = ((k.get("type") or {}).get("type") or "").lower()
        w = wall(k.get("wallclock"))
        if not w:
            continue
        team = (k.get("team") or {}).get("displayName", "")
        who = ""
        for p in k.get("participants") or []:
            who = ((p.get("athlete") or {}).get("displayName")) or who
            break
        minute = (k.get("clock") or {}).get("displayValue", "")
        if k.get("scoringPlay"):
            side = sides.get(str((k.get("team") or {}).get("id")))
            if side == "home":
                home += 1
            elif side == "away":
                away += 1
            kind = "own" if "own" in t else "pen" if "penalty" in t else "goal"
            out.append({"type": "goal", "kind": kind, "wall": w, "minute": minute, "who": who, "team": team,
                        "side": side or "", "score": f"{home}:{away}"})
        elif "red-card" in t:
            out.append({"type": "red", "wall": w, "minute": minute, "who": who, "team": team,
                        "side": sides.get(str((k.get("team") or {}).get("id")), "")})
        elif t in MARKS:
            out.append({"type": "mark", "wall": w, "minute": minute, "text": MARKS[t]})
    status = (comp.get("status") or {}).get("type") or {}
    score = ""
    cs = comp.get("competitors", [])
    if len(cs) == 2:
        h = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
        a = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
        score = f"{h.get('score', '')}:{a.get('score', '')}" if h.get("score") not in (None, "") else ""
    return {"events": out, "game": status.get("state", ""), "clock": (comp.get("status") or {}).get("displayClock", ""),
            "detail": status.get("shortDetail", ""), "score": score}


# ---------------------------------------------------------------- channels

def probe(uuid):
    """What a channel delivers now: (score, text) or (None, error). Opens the stream for a few seconds."""
    url = f"{state['dispatcharr']}/proxy/ts/stream/{uuid}"
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-rw_timeout", "12000000", "-analyzeduration", "4000000",
                            "-probesize", "6000000", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height,r_frame_rate,avg_frame_rate", "-of", "json", url],
                           capture_output=True, timeout=30, text=True)
        s = json.loads(p.stdout or "{}").get("streams", [])
        if not s:
            return None, (p.stderr or "no picture").strip()[:120]
        v = s[0]
        h = int(v.get("height") or 0)
        num, _, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 0
        if fps <= 0 or fps > 120:
            num, _, den = (v.get("r_frame_rate") or "0/1").partition("/")
            fps = float(num) / float(den or 1) if float(den or 1) else 0
        # 1080p50 > 720p50 > 1080p25 > 720p25: sport moves.
        return h + (400 if fps > 40 else 0), f"{h}p{round(fps)}"
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        return None, str(e)[:120]


# ---------------------------------------------------------------- recording

class Worker(threading.Thread):
    def __init__(self, rid):
        super().__init__(daemon=True)
        self.rid = rid
        self.stop_now = threading.Event()
        self.proc = None

    def rec(self):
        return state["recordings"][self.rid]

    def update(self, **kw):
        with lock:
            self.rec().update(kw)
        save()

    def run(self):
        try:
            self.record()
        except Exception as e:  # noqa: BLE001 - keep the service up
            log("recording failed", self.rid, repr(e))
            self.update(state="done", ended=now_ms(), deleteAt=now_ms() + KEEP, error=str(e)[:200])
        finally:
            finish_playlist(self.rec())
            workers.pop(self.rid, None)

    def finished(self):
        """Whether the game is over: ESPN says so (three minutes ago: the picture runs up to a minute behind), or it has run far too long."""
        r = self.rec()
        if self.stop_now.is_set():
            return True
        if now_ms() > r["start"] + 4 * 3600_000:
            return True
        post = r.get("postAt")
        return bool(post) and now_ms() > post + 3 * 60_000

    def follow(self):
        """ESPN's events and state, every minute while recording."""
        r = self.rec()
        if now_ms() - r.get("checked", 0) < 60_000:
            return
        try:
            ev = events_of(summary(r))
            with lock:
                r["checked"] = now_ms()
                r["events"] = ev
                if ev["game"] == "post" and not r.get("postAt"):
                    r["postAt"] = now_ms()
                    log("game over", r["title"], ev["score"])
            save()
        except Exception as e:  # noqa: BLE001
            with lock:
                r["checked"] = now_ms()
            log("ESPN", r["id"], repr(e)[:120])

    def record(self):
        r = self.rec()
        folder = r["folder"]
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, MARKER), "w") as f:
            f.write(r["id"])
        log("recording", r["title"], "->", folder)
        # The channel TV Abo shows the game on: the TV sends it first (alone since 8.11.2026).
        cands = r.get("candidates", [])
        c = dict(cands[0]) if cands else None
        # Going on after a restart: the channel it recorded before, so a game stays one version.
        was = next((x for x in cands if r.get("channel") and x.get("name") == r.get("channel")), None)
        if was is not None:
            c = dict(was)
        if c is not None:
            score, what = probe(c["uuid"])
            log("  channel", c.get("name"), "->", what)
            if score is not None:
                c["what"] = what
        fails = 0
        while not self.finished():
            if c is None:
                time.sleep(10)
                self.follow()
                continue
            self.update(state="recording", channel=c.get("name", ""), channelWhat=c.get("what", ""))
            ok = self.run_ffmpeg(c, folder)
            if self.finished():
                break
            fails = 0 if ok else fails + 1
            log("  again:", c.get("name"))
            time.sleep(2 if fails < 3 else 15)
        self.follow()
        r = self.rec()
        r["checked"] = 0
        self.follow()
        self.update(state="done", ended=now_ms(), deleteAt=now_ms() + KEEP)
        log("done", r["title"])

    def run_ffmpeg(self, c, folder):
        """Records channel c into the folder's playlist until it stalls or the game ends; True when it ran a while."""
        url = f"{state['dispatcharr']}/proxy/ts/stream/{c['uuid']}"
        playlist = os.path.join(folder, "index.m3u8")
        # A second run (the channel again after a stall) goes on in the same playlist, marked as a break.
        flags = "append_list+discont_start+program_date_time+temp_file" if os.path.exists(playlist) else "program_date_time+temp_file"
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
               "-rw_timeout", "15000000", "-fflags", "+discardcorrupt", "-i", url,
               "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
               "-f", "hls", "-hls_time", str(SEGMENT), "-hls_list_size", "0", "-hls_playlist_type", "event",
               "-hls_flags", flags,
               "-hls_segment_filename", os.path.join(folder, "s%05d.ts"), playlist]
        began = time.time()
        log("  ffmpeg", c.get("name"), c.get("what", ""))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        err = []
        threading.Thread(target=lambda: [err.append(l) for l in self.proc.stderr], daemon=True).start()
        last = time.time()
        seen = newest(folder)
        while self.proc.poll() is None:
            time.sleep(2)
            self.follow()
            n = newest(folder)
            if n != seen:
                seen, last = n, time.time()
                with lock:
                    r = self.rec()
                    if not r.get("began"):
                        r["began"] = now_ms() - SEGMENT * 1000
                    r["updated"] = now_ms()
            if self.finished():
                self.proc.send_signal(signal.SIGINT)
                try:
                    self.proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                return True
            if time.time() - last > STALL:
                log("  stalled:", c.get("name"))
                self.proc.send_signal(signal.SIGINT)
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                break
        if err:
            log("  ffmpeg says:", " | ".join(x.strip() for x in err[-3:])[:300])
        return time.time() - began > 60


def newest(folder):
    try:
        segs = [f for f in os.listdir(folder) if f.endswith(".ts")]
        return max(segs) if segs else ""
    except OSError:
        return ""


def finish_playlist(r):
    """A finished recording's playlist ends with ENDLIST, so players see it as a whole."""
    p = os.path.join(r.get("folder", ""), "index.m3u8")
    try:
        with open(p) as f:
            text = f.read()
        if "#EXT-X-ENDLIST" not in text:
            with open(p, "a") as f:
                f.write("#EXT-X-ENDLIST\n")
    except OSError:
        pass


# ---------------------------------------------------------------- schedule

def tick():
    t = now_ms()
    with lock:
        plans = {}
        # The newest plan first: the channels the guide names change until shortly before kick-off.
        for tv, p in sorted(state["plans"].items(), key=lambda kv: -kv[1].get("at", 0)):
            for g in p.get("games", []):
                plans.setdefault(g["id"], g)
        for gid, g in plans.items():
            r = state["recordings"].get(gid) or same_game(g)
            if r is not None and r["id"] != gid:
                # The same game under another id (another TV, another subscription): one recording.
                if gid not in r.setdefault("ids", [r["id"]]):
                    r["ids"].append(gid)
                    log("same game", g["title"], gid, "->", r["id"])
                continue
            if r is None and g["start"] - PRE * 1000 <= t < g.get("end", g["start"] + 7200_000) and g.get("candidates"):
                folder = os.path.join(safe_dir(state.get("dir")), folder_name(g))
                r = dict(g, state="starting", folder=folder, planned=t)
                state["recordings"][gid] = r
                log("start", g["title"])
            if r is not None and r.get("state") != "done" and gid not in workers:
                w = Worker(gid)
                workers[gid] = w
                w.start()
        for gid, r in list(state["recordings"].items()):
            if r.get("state") == "done" and r.get("deleteAt", 0) < t and gid not in workers:
                delete(r)
                del state["recordings"][gid]
    save()


def fold(text):
    import unicodedata
    t = unicodedata.normalize("NFD", (text or "").lower().replace("ı", "i"))
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in t if not unicodedata.combining(c))).strip()


def same_game(g):
    """A recording of this game under another id: same sides (title) and kick-off within 15 minutes."""
    for r in state["recordings"].values():
        if fold(r.get("title")) == fold(g.get("title")) and abs(r.get("start", 0) - g.get("start", 0)) <= 15 * 60_000:
            return r
    return None


def delete(r):
    folder = r.get("folder", "")
    if folder and os.path.exists(os.path.join(folder, MARKER)) and safe_dir(os.path.dirname(folder)) == os.path.dirname(folder):
        shutil.rmtree(folder, ignore_errors=True)
        log("deleted", folder)


def resume():
    """After a restart: unfinished recordings go on (ffmpeg appends to their playlist)."""
    with lock:
        for gid, r in state["recordings"].items():
            if r.get("state") in ("recording", "starting"):
                r["state"] = "starting"


def scheduler():
    while True:
        try:
            tick()
        except Exception as e:  # noqa: BLE001
            log("tick", repr(e))
        time.sleep(10)


# ---------------------------------------------------------------- positions

def segments(folder):
    """
    The playlist's segments as (wall ms when their start came from the channel, seconds, place in
    the recording in seconds). The time comes from when each file was finished, not from ffmpeg's
    PROGRAM-DATE-TIME: that counts from ffmpeg's start, and Dispatcharr first sends several seconds
    it holds at once. Per run of ffmpeg (between discontinuities) the median of finished time less
    content end gives the run's offset; the first files of a run (the burst) are left out of it.
    """
    runs, run, at, dur = [], [], 0.0, None
    try:
        with open(os.path.join(folder, "index.m3u8")) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    for line in lines:
        if line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0] or 0)
        elif line.startswith("#EXT-X-DISCONTINUITY"):
            if run:
                runs.append(run)
            run = []
        elif line and not line.startswith("#"):
            if dur is not None:
                try:
                    done = os.path.getmtime(os.path.join(folder, line)) * 1000
                except OSError:
                    done = None
                run.append([line, dur, at, done])
                at += dur
            dur = None
    if run:
        runs.append(run)
    out = []
    for run in runs:
        steady = [s for s in run[3:] if s[3]] or [s for s in run if s[3]]
        offs = sorted(s[3] - (s[2] + s[1]) * 1000 for s in steady)
        off = offs[len(offs) // 2] if offs else 0
        for name, d, a, done in run:
            out.append((int(a * 1000 + off), d, a))
    return out


def place(segs, wall_ms):
    """Where the moment {wall_ms} (as it arrived from the channel) is in the recording, in seconds; None if not (yet)."""
    if not segs or wall_ms < segs[0][0]:
        return None
    for w, d, at in reversed(segs):
        if w <= wall_ms:
            off = (wall_ms - w) / 1000
            return round(at + off, 1) if off <= d + 1 else None
    return None


# ---------------------------------------------------------------- HTTP

def public(r):
    keys = ("id", "title", "league", "sport", "start", "end", "state", "channel", "channelWhat", "began", "ended",
            "deleteAt", "homeLogo", "awayLogo", "updated")
    out = {k: r.get(k) for k in keys if r.get(k) is not None}
    out["url"] = f"/rec/{r['id']}/index.m3u8"
    out["ids"] = r.get("ids", [r["id"]])
    out["delay"] = delay_of(r.get("channel"))
    ev = r.get("events") or {}
    out["score"] = ev.get("score", "")
    out["clock"] = ev.get("detail", "")
    out["uuids"] = [c.get("uuid") for c in r.get("candidates", [])]
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8", cache=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/recordings":
            with lock:
                recs = sorted((public(r) for r in state["recordings"].values()), key=lambda r: r["start"])
            return self.send(200, {"recordings": recs, "dir": state.get("dir"), "now": now_ms()})
        m = re.match(r"^/api/events/([\w.-]+)$", path)
        if m:
            with lock:
                r = state["recordings"].get(m.group(1))
                if r is None:
                    return self.send(404, {"error": "unknown"})
                out = dict(r.get("events") or {"events": []})
                out.update(public(r))
                folder = r.get("folder", "")
            segs = segments(folder)
            evs = []
            lag = delay_of(r.get("channel"))
            out["delay"] = lag
            for e in out.get("events", []):
                e = dict(e)
                t = e["wall"] + int(lag * 1000)
                e["pos"] = place(segs, t)
                # Before the recording began: how long before (negative), for the game's minute.
                if e["pos"] is None and segs and t < segs[0][0]:
                    e["at"] = round((t - segs[0][0]) / 1000, 1)
                else:
                    e["at"] = e["pos"]
                evs.append(e)
            out["events"] = evs
            out["length"] = round(segs[-1][2] + segs[-1][1], 1) if segs else 0
            return self.send(200, out)
        m = re.match(r"^/rec/([\w.-]+)/([\w.-]+\.(m3u8|ts))$", path)
        if m:
            with lock:
                r = state["recordings"].get(m.group(1))
            if r is None:
                return self.send(404, "")
            f = os.path.join(r["folder"], m.group(2))
            try:
                if m.group(3) == "m3u8":
                    with open(f, "rb") as fh:
                        return self.send(200, fh.read(), "application/vnd.apple.mpegurl")
                size = os.path.getsize(f)
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                if self.command == "HEAD":
                    return
                with open(f, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile, 256 * 1024)
                return
            except OSError:
                return self.send(404, "")
        if path == "/":
            return self.send(200, "OpenTV Aufnahmen\n", "text/plain")
        if path == "/health":
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except ValueError:
            return self.send(400, {"error": "json"})
        if path == "/api/plan":
            tv = str(body.get("tv") or self.client_address[0])
            games = []
            for g in body.get("games", []):
                try:
                    games.append({"id": str(g["id"]), "title": str(g.get("title", "")), "league": str(g.get("league", "")),
                                  "sport": str(g.get("sport", "soccer")), "start": int(g["start"]), "end": int(g.get("end") or 0),
                                  "homeLogo": str(g.get("homeLogo", "")), "awayLogo": str(g.get("awayLogo", "")),
                                  "candidates": [{"uuid": str(c["uuid"]), "name": str(c.get("name", ""))}
                                                 for c in g.get("candidates", []) if c.get("uuid")]})
                except (KeyError, ValueError, TypeError):
                    continue
            with lock:
                state["plans"][tv] = {"games": games, "at": now_ms()}
                if body.get("dir"):
                    state["dir"] = safe_dir(body["dir"])
                if not DISPATCHARR and body.get("dispatcharr"):
                    state["dispatcharr"] = str(body["dispatcharr"]).rstrip("/")
                elif DISPATCHARR:
                    state["dispatcharr"] = DISPATCHARR
            save()
            return self.send(200, {"ok": True, "games": len(games), "dir": state["dir"]})
        m = re.match(r"^/api/stop/([\w.-]+)$", path)
        if m and m.group(1) in workers:
            workers[m.group(1)].stop_now.set()
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


def main():
    os.makedirs(DATA, exist_ok=True)
    load()
    resume()
    threading.Thread(target=scheduler, daemon=True).start()
    log("OpenTV Aufnahmen on port", PORT, "dir", state.get("dir"))
    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            # A TV that stops a download midway (seeking) is no error worth a traceback.
            import sys
            if not isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
                super().handle_error(request, client_address)

    Server(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
