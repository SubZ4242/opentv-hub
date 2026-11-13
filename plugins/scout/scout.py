#!/usr/bin/env python3
"""OpenTV Scout: films worth getting that are not in the Plex library yet.

A TrueNAS custom app (python:3.12-slim, no packages). Once a night at 03:00 (TZ) it fetches the
public film lists of Radarr's metadata service (trending, popular, IMDb popular, IMDb Top 250) and
follows the recommendations of the films already in the library, which is where the Turkish and
German films come from. The TVs tell it what is in the library (POST /api/library, TMDB and IMDb
ids from Plex) and read the lists (GET /api/scout); what is in the library is left out as it is
served, so a film that arrived at noon is gone from the lists at noon.

Nothing here needs a key or a login. Requests to the metadata service are spaced out and every
film is fetched once and kept (data/films), so a night costs a few hundred small requests at most.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

API = os.environ.get("SCOUT_API", "https://api.radarr.video/v1")
PORT = int(os.environ.get("PORT", "35120"))
DATA = Path(os.environ.get("DATA", "/app/data"))
HOUR = int(os.environ.get("SCOUT_HOUR", "3"))
# New films looked up per night: the library's own (for their language and recommendations) and
# the recommended ones (to know what they are).
LIBRARY_PER_NIGHT = int(os.environ.get("SCOUT_LIBRARY_PER_NIGHT", "600"))
CANDIDATES_PER_NIGHT = int(os.environ.get("SCOUT_CANDIDATES_PER_NIGHT", "400"))
PAUSE = float(os.environ.get("SCOUT_PAUSE", "0.35"))
ROW = int(os.environ.get("SCOUT_ROW", "30"))
FILMS = DATA / "films"
# Where games are on TV: liveonsat.com's listings, today and the next two days, read every 3 hours.
LIVEONSAT = os.environ.get("SCOUT_LIVEONSAT", "https://liveonsat.com/2day.php")
BROADCAST_HOURS = float(os.environ.get("SCOUT_BROADCAST_HOURS", "3"))
LOCK = threading.Lock()
STATE = {"updated": 0, "sections": [], "running": False, "note": ""}


def log(*a):
    print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def get(path: str):
    req = urllib.request.Request(API + path, headers={"User-Agent": "opentv-scout/1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def read(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
    tmp.replace(path)


def image(images, kind: str) -> str:
    for i in images or []:
        if i.get("CoverType") == kind and str(i.get("Url", "")).startswith("http"):
            return i["Url"]
    return ""


def slim(m: dict) -> dict:
    """What the TV shows of a film, German where the service has it."""
    de = next((t for t in m.get("Translations") or [] if t.get("Language") == "de-DE"), None) or \
        next((t for t in m.get("Translations") or [] if str(t.get("Language", "")).startswith("de")), {})
    tr = next((t for t in m.get("Translations") or [] if str(t.get("Language", "")).startswith("tr")), {})
    ratings = m.get("MovieRatings") or {}
    imdb = (ratings.get("Imdb") or {}).get("Value") or 0
    tmdb = (ratings.get("Tmdb") or {}).get("Value") or 0
    credits = m.get("Credits") or {}
    cast = [{"name": c.get("Name", ""), "role": c.get("Character", ""),
             "photo": image(c.get("Images"), "Headshot").replace("/t/p/original/", "/t/p/w185/")}
            for c in sorted(credits.get("Cast") or [], key=lambda c: c.get("Order", 99))[:8]]
    directors = [c.get("Name", "") for c in credits.get("Crew") or [] if c.get("Job") == "Director"][:2]
    cert = next((c.get("Certification", "") for c in m.get("Certifications") or [] if c.get("Country") == "DE"), "")
    original = m.get("OriginalLanguage") or ""
    title = (tr.get("Title") if original == "tr" and tr.get("Title") else de.get("Title")) or m.get("Title") or ""
    return {
        "tmdbId": m.get("TmdbId") or 0,
        "imdbId": m.get("ImdbId") or "",
        "title": title,
        "germanTitle": de.get("Title") or m.get("Title") or "",
        "originalTitle": m.get("OriginalTitle") or "",
        "language": original,
        "year": m.get("Year") or 0,
        "overview": (de.get("Overview") or m.get("Overview") or "").strip(),
        "runtime": m.get("Runtime") or 0,
        "genres": (m.get("Genres") or [])[:3],
        "posterFull": image(m.get("Images"), "Poster").replace("/t/p/original/", "/t/p/w500/"),
        "fanart": image(m.get("Images"), "Fanart").replace("/t/p/original/", "/t/p/w1280/"),
        "rating": ("%.1f" % imdb) if imdb else ("%.1f" % tmdb) if tmdb else "",
        "imdbRating": round(imdb, 1),
        "imdbVotes": (ratings.get("Imdb") or {}).get("Count") or 0,
        "certification": cert,
        "trailer": m.get("YoutubeTrailerId") or "",
        "director": ", ".join(d for d in directors if d),
        "cast": cast,
        "popularity": m.get("Popularity") or 0,
        "recommendations": [r.get("TmdbId") for r in m.get("Recommendations") or [] if r.get("TmdbId")][:12],
        "fetched": int(time.time()),
    }


def film(tmdb: int, fetch: bool = True) -> dict | None:
    path = FILMS / f"{tmdb}.json"
    have = read(path, None)
    if have is not None or not fetch:
        return have
    try:
        m = get(f"/movie/{tmdb}")
        time.sleep(PAUSE)
    except Exception as e:  # noqa: BLE001
        log("film", tmdb, "not fetched:", e)
        return None
    if isinstance(m, list):
        m = m[0] if m else None
    if not isinstance(m, dict) or not m.get("TmdbId"):
        write(path, {"tmdbId": tmdb, "title": "", "fetched": int(time.time())})
        return None
    s = slim(m)
    write(path, s)
    return s


def keep(m: dict) -> dict:
    s = slim(m)
    if s["tmdbId"]:
        write(FILMS / f"{s['tmdbId']}.json", s)
    return s


def library() -> dict:
    return read(DATA / "library.json", {"tmdb": [], "imdb": [], "at": 0})


def good(s: dict) -> bool:
    """Shown at all: it has a poster and a name, and is no adult or obscure entry."""
    return bool(s and s.get("title") and s.get("posterFull") and s.get("year"))


def build() -> None:
    """The night's work."""
    with LOCK:
        if STATE["running"]:
            return
        STATE["running"] = True
    try:
        t0 = time.time()
        lib = library()
        have = set(lib.get("tmdb") or [])
        lists = {}
        for name, path in (("trending", "/list/tmdb/trending"), ("popular", "/list/tmdb/popular"),
                           ("imdb", "/list/imdb/popular"), ("top", "/list/imdb/top250")):
            try:
                lists[name] = [keep(m) for m in get(path) if isinstance(m, dict)]
                log("list", name, len(lists[name]))
            except Exception as e:  # noqa: BLE001
                log("list", name, "failed:", e)
                lists[name] = []
            time.sleep(PAUSE)
        # The library's own films: their language and what goes with them.
        fetched = 0
        counts: dict[int, int] = {}
        by_lang: dict[str, dict[int, int]] = {"tr": {}, "de": {}}
        for tmdb in list(have):
            cached = film(tmdb, fetch=False)
            if cached is None and fetched < LIBRARY_PER_NIGHT:
                cached = film(tmdb)
                fetched += 1
            if not cached:
                continue
            for r in cached.get("recommendations") or []:
                counts[r] = counts.get(r, 0) + 1
                lang = cached.get("language")
                if lang in by_lang:
                    by_lang[lang][r] = by_lang[lang].get(r, 0) + 1
        log("library", len(have), "films,", fetched, "looked up,", len(counts), "recommended")
        # The recommended ones, the most named first; Turkish and German ones before the rest.
        order = sorted(by_lang["tr"], key=lambda i: -by_lang["tr"][i]) + sorted(by_lang["de"], key=lambda i: -by_lang["de"][i]) \
            + sorted(counts, key=lambda i: -counts[i])
        looked = 0
        for tmdb in order:
            if tmdb in have or film(tmdb, fetch=False) is not None:
                continue
            if looked >= CANDIDATES_PER_NIGHT:
                break
            film(tmdb)
            looked += 1

        def row(ids, want=None, least=0.0):
            out, seen = [], set()
            for i in ids:
                if i in seen:
                    continue
                seen.add(i)
                s = film(i, fetch=False)
                if not good(s) or (want and s.get("language") != want) or (s.get("imdbRating") or 0) < least:
                    continue
                out.append(i)
            return out

        sections = [
            {"id": "trending", "title": "Im Trend", "ids": [s["tmdbId"] for s in lists["trending"] if good(s)]},
            {"id": "popular", "title": "Beliebt", "ids": [s["tmdbId"] for s in lists["popular"] if good(s)]},
            {"id": "imdb", "title": "Beliebt bei IMDb", "ids": [s["tmdbId"] for s in lists["imdb"] if good(s)]},
            {"id": "tr", "title": "Türkische Filme", "ids": row(sorted(by_lang["tr"], key=lambda i: -by_lang["tr"][i]), "tr")},
            {"id": "de", "title": "Deutsche Filme", "ids": row(sorted(by_lang["de"], key=lambda i: -by_lang["de"][i]), "de")},
            {"id": "match", "title": "Passt zu deiner Mediathek", "ids": row(sorted(counts, key=lambda i: -counts[i]), least=6.0)},
            {"id": "top", "title": "IMDb Top 250", "ids": [s["tmdbId"] for s in lists["top"] if good(s)]},
        ]
        write(DATA / "sections.json", {"updated": int(time.time()), "sections": sections})
        log("built in %.0f s:" % (time.time() - t0), ", ".join(f"{s['id']} {len(s['ids'])}" for s in sections), "| looked up", looked)
    except Exception as e:  # noqa: BLE001
        log("build failed:", repr(e))
    finally:
        STATE["running"] = False


def served() -> dict:
    """The rows as they stand now, without what the library has."""
    data = read(DATA / "sections.json", {"updated": 0, "sections": []})
    lib = library()
    have, have_imdb = set(lib.get("tmdb") or []), set(lib.get("imdb") or [])
    out = []
    for sec in data.get("sections") or []:
        items = []
        for i in sec.get("ids") or []:
            s = film(i, fetch=False)
            if not good(s) or i in have or (s.get("imdbId") and s["imdbId"] in have_imdb):
                continue
            items.append({k: v for k, v in s.items() if k not in ("recommendations", "fetched", "popularity")})
            if len(items) >= ROW:
                break
        if items:
            out.append({"id": sec["id"], "title": sec["title"], "items": items})
    return {"updated": data.get("updated", 0), "library": len(have), "libraryAt": lib.get("at", 0),
            "building": STATE["running"], "sections": out}


# ------------------------------------------------------------- broadcasts

def broadcasts_fetch() -> None:
    """liveonsat's fixtures with the channels that show them, for today and the next two days."""
    import html as htmllib
    import re
    today = datetime.date.today()
    end = today + datetime.timedelta(days=2)
    url = (f"{LIVEONSAT}?start_dd={today.day}&start_mm={today.month}&start_yyyy={today.year}"
           f"&end_dd={end.day}&end_mm={end.month}&end_yyyy={end.year}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (opentv-scout)"})
    with urllib.request.urlopen(req, timeout=90) as r:
        page = r.read().decode("utf-8", "replace")
    games = []
    for part in page.split("<!-- around fixture text 3-->")[1:]:
        t = re.search(r'class = fLeft style="[^"]*">([^<]+)</div>', part)
        ts = re.search(r'data-timestamp="(\d+)"', part)
        if not t or not ts:
            continue
        chans = [htmllib.unescape(c).strip() for c in re.findall(r'onmouseout="return nd\(\);">([^<]+)</a>', part)]
        chans = list(dict.fromkeys(c for c in chans if c))
        games.append({"title": htmllib.unescape(t.group(1)).strip(), "start": int(ts.group(1)) * 1000, "channels": chans})
    write(DATA / "broadcasts.json", {"updated": int(time.time()), "games": games})
    log("broadcasts:", len(games), "games")


def broadcasts_loop() -> None:
    while True:
        try:
            broadcasts_fetch()
        except Exception as e:  # noqa: BLE001
            log("broadcasts failed:", repr(e)[:200])
        time.sleep(BROADCAST_HOURS * 3600)


def broadcasts(start: int, end: int) -> dict:
    data = read(DATA / "broadcasts.json", {"updated": 0, "games": []})
    games = [g for g in data.get("games") or [] if start <= g.get("start", 0) <= end]
    return {"updated": data.get("updated", 0), "source": "liveonsat.com", "games": games}


def nightly() -> None:
    if not (DATA / "sections.json").is_file():
        build()
    while True:
        now = datetime.datetime.now()
        nxt = now.replace(hour=HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += datetime.timedelta(days=1)
        time.sleep(max(60, (nxt - now).total_seconds()))
        build()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code: int, body) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/health"):
            self.send(200, {"ok": True, "service": "opentv-scout", "updated": read(DATA / "sections.json", {}).get("updated", 0),
                            "building": STATE["running"]})
        elif self.path.startswith("/api/scout"):
            self.send(200, served())
        elif self.path.startswith("/api/broadcasts"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            now = int(time.time() * 1000)
            start = int((q.get("from") or [now - 4 * 3600_000])[0])
            end = int((q.get("to") or [now + 3 * 86400_000])[0])
            self.send(200, broadcasts(start, end))
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4_000_000:
            self.send(413, {"error": "too large"})
            return
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self.send(400, {"error": "json"})
            return
        if self.path.startswith("/api/library"):
            tmdb = sorted({int(i) for i in body.get("tmdb") or [] if str(i).isdigit()})
            imdb = sorted({str(i) for i in body.get("imdb") or [] if str(i).startswith("tt")})
            if len(tmdb) < 5:
                self.send(400, {"error": "too few"})
                return
            first = not library().get("tmdb")
            write(DATA / "library.json", {"tmdb": tmdb, "imdb": imdb, "at": int(time.time())})
            log("library from", self.client_address[0], len(tmdb), "films")
            # The first library ever: the rows that follow it are made now, not next night.
            if first:
                threading.Thread(target=build, daemon=True).start()
            self.send(200, {"ok": True, "films": len(tmdb)})
        elif self.path.startswith("/api/refresh"):
            threading.Thread(target=build, daemon=True).start()
            self.send(200, {"ok": True})
        else:
            self.send(404, {"error": "not found"})


if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=nightly, daemon=True).start()
    threading.Thread(target=broadcasts_loop, daemon=True).start()
    log("opentv-scout on", PORT, "| nightly at", f"{HOUR:02d}:00")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
