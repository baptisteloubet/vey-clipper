"""Découverte des vidéos à clipper.

Twitch : les VOD du jeu que les spectateurs ont le plus clippées (Helix). On
envoie au montage les clips spectateurs les plus vus de la VOD, recollés : ils
sont publics même quand la VOD est réservée aux abonnés.
YouTube : les vidéos récentes qui prennent le plus de vues à l'heure (Data API).
"""
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

import requests

from . import redact

HELIX = "https://api.twitch.tv/helix"
YT = "https://www.googleapis.com/youtube/v3"


@dataclass
class Candidate:
    platform: str            # twitch | youtube
    video_id: str
    url: str
    channel: str             # nom affiché
    channel_key: str         # login Twitch / id de chaîne YouTube
    title: str
    score: float
    language: str = ""
    source_start: float = None
    source_end: float = None
    clips: list = None       # Twitch : [{url, offset, duration, views}] à recoller
    allowed: bool = False
    pool: list = None        # Twitch : tous les clips de la VOD (pour remonter un live en cours)
    part: int = 0            # 0 = 1er montage de la VOD, puis 1, 2… avec les clips suivants
    game: str = ""           # jeu de la source : seuls les jeux de `twitch.game_names` sont publiés
    fresh_at: str = ""       # Twitch : date du clip le plus récent de la VOD (ISO)

    @property
    def base_key(self):
        return f"{self.platform}:{self.video_id}"

    @property
    def key(self):
        return self.base_key + (f"#{self.part}" if self.part else "")

    def to_dict(self):
        data = asdict(self)
        data.pop("pool")  # trop gros pour l'état, et recalculé à chaque recherche
        return data


def _norm(value):
    return str(value or "").strip().lstrip("@").lower()


def is_allowed(candidate, allowed_channels):
    names = {_norm(n) for n in (allowed_channels.get(candidate.platform) or [])}
    return bool(names & {_norm(candidate.channel), _norm(candidate.channel_key)})


def age_hours(iso, now):
    """Âge en heures d'une date ISO (Twitch : « 2026-11-20T00:42:11Z ») ; None si illisible."""
    try:
        then = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (now - then).total_seconds() / 3600)


def freshness(iso, now, tw):
    """Coefficient de fraîcheur (1 = tout frais) : demi-vie de `half_life_hours`."""
    half = tw.get("half_life_hours") or 0
    age = age_hours(iso, now) if iso else None
    if not half or age is None:
        return 1.0
    return 0.5 ** (age / half)


def pick_clips(clips, max_seconds):
    """Les clips les plus vus, sans chevauchement, `max_seconds` au total.

    `clips` : [{"url", "offset", "duration", "views"}]. Renvoyés dans l'ordre du
    stream. Plusieurs spectateurs clippent souvent le même moment : on ne garde
    que le plus vu.
    """
    picked, total = [], 0.0
    for clip in sorted(clips, key=lambda c: -c["views"]):
        start, end = clip["offset"], clip["offset"] + clip["duration"]
        if total + clip["duration"] > max_seconds:
            continue
        if any(start < p["offset"] + p["duration"] and p["offset"] < end for p in picked):
            continue
        picked.append(clip)
        total += clip["duration"]
    return sorted(picked, key=lambda c: c["offset"])


def parse_iso_duration(value):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Twitch
# --------------------------------------------------------------------------- #
class TwitchDiscovery:
    def __init__(self, cfg, session=None, now=None):
        self.cfg = cfg
        self.http = session or requests.Session()
        self.now = now or datetime.now(timezone.utc)
        self._headers = None

    def _get(self, path, params):
        if self._headers is None:
            env = self.cfg["env"]
            resp = self.http.post("https://id.twitch.tv/oauth2/token", data={
                "client_id": env["TWITCH_CLIENT_ID"], "client_secret": env["TWITCH_CLIENT_SECRET"],
                "grant_type": "client_credentials"}, timeout=30)
            resp.raise_for_status()
            token = resp.json()["access_token"]
            redact.mask(token)
            self._headers = {"Client-Id": env["TWITCH_CLIENT_ID"], "Authorization": f"Bearer {token}"}
        resp = self.http.get(f"{HELIX}/{path}", params=params, headers=self._headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _clips(self, since, **who):
        clips, cursor = [], None
        for _ in range(3):  # Helix renvoie les clips par vues décroissantes
            params = {"first": 100, "started_at": _iso(since), "ended_at": _iso(self.now), **who}
            if cursor:
                params["after"] = cursor
            data = self._get("clips", params)
            clips += data.get("data", [])
            cursor = (data.get("pagination") or {}).get("cursor")
            if not cursor:
                break
        return clips

    def discover(self):
        src = self.cfg["sources"]
        tw = src["twitch"]
        since = self.now - timedelta(hours=src["lookback_hours"])
        games = self._get("games", [("name", n) for n in tw["game_names"]]).get("data", [])
        game_ids = {g["id"] for g in games}
        game_names = {g["id"]: g.get("name", "") for g in games}
        if not game_ids:
            print(f"[twitch] jeu introuvable : {tw['game_names']}")
            return []

        clips = []
        logins = [_norm(n) for n in src["allowed_channels"].get("twitch") or []]
        if logins:
            users = self._get("users", [("login", l) for l in logins[:100]]).get("data", [])
            for user in users:
                clips += [c for c in self._clips(since, broadcaster_id=user["id"])
                          if c.get("game_id") in game_ids]
        if not src["only_allowed_channels"]:
            for gid in game_ids:
                clips += self._clips(since, game_id=gid)

        languages = {l.lower() for l in src["languages"]}
        by_vod = {}
        for clip in {c["id"]: c for c in clips}.values():
            if not clip.get("video_id") or clip.get("vod_offset") is None:
                continue  # VOD supprimée ou clip sans position
            if languages and (clip.get("language") or "").lower()[:2] not in languages:
                continue
            by_vod.setdefault(clip["video_id"], []).append(clip)

        out = []
        max_seconds = self.cfg["clipping"]["max_minutes_per_source"] * 60
        for vod_id, vod_clips in by_vod.items():
            views = sum(c.get("view_count", 0) for c in vod_clips)
            if views < tw["min_clip_views"]:
                continue
            first = max(vod_clips, key=lambda c: c.get("view_count", 0))
            pool = [{"url": c["url"], "offset": float(c["vod_offset"]),
                     "duration": float(c.get("duration") or 30), "views": c.get("view_count", 0)}
                    for c in vod_clips]
            picked = pick_clips(pool, max_seconds)
            fresh_at = max((c.get("created_at") or "" for c in vod_clips), default="")
            login = (first.get("broadcaster_login")
                     or re.sub(r".*twitch\.tv/([^/]+)/clip/.*", r"\1", first.get("url", "")))
            out.append(Candidate(
                platform="twitch", video_id=vod_id, url=f"https://www.twitch.tv/videos/{vod_id}",
                channel=first.get("broadcaster_name", ""), channel_key=_norm(login),
                title=first.get("title", ""), score=float(views) * freshness(fresh_at, self.now, tw),
                language=first.get("language", ""), clips=picked, pool=pool,
                game=game_names.get(first.get("game_id"), ""), fresh_at=fresh_at))
        return sorted(out, key=lambda c: -c.score)


# --------------------------------------------------------------------------- #
# YouTube
# --------------------------------------------------------------------------- #
class YouTubeDiscovery:
    def __init__(self, cfg, session=None, now=None):
        self.cfg = cfg
        self.http = session or requests.Session()
        self.now = now or datetime.now(timezone.utc)

    def _get(self, path, params):
        resp = self.http.get(f"{YT}/{path}", timeout=30,
                             params={**params, "key": self.cfg["env"]["YOUTUBE_API_KEY"]})
        resp.raise_for_status()
        return resp.json()

    def _channel_id(self, name):
        name = str(name).strip()
        if re.fullmatch(r"UC[\w-]{22}", name):
            return name
        items = self._get("channels", {"part": "id", "forHandle": "@" + name.lstrip("@")}).get("items", [])
        return items[0]["id"] if items else None

    def _search(self, since, **extra):
        params = {"part": "id", "type": "video", "order": "viewCount", "maxResults": 25,
                  "publishedAfter": _iso(since), **extra}
        return [i["id"]["videoId"] for i in self._get("search", params).get("items", [])
                if i.get("id", {}).get("videoId")]

    def discover(self):
        src = self.cfg["sources"]
        yt = src["youtube"]
        since = self.now - timedelta(hours=src["lookback_hours"])
        lang = (src["languages"] or [""])[0]
        ids = []
        for name in src["allowed_channels"].get("youtube") or []:
            cid = self._channel_id(name)
            if not cid:
                print(f"[youtube] chaîne introuvable : {name}")
                continue
            for query in yt["queries"]:
                ids += self._search(since, channelId=cid, q=query)
        if not src["only_allowed_channels"]:
            for query in yt["queries"]:
                extra = {"q": query}
                if lang:
                    extra["relevanceLanguage"] = lang
                ids += self._search(since, **extra)
        ids = list(dict.fromkeys(ids))

        out = []
        for i in range(0, len(ids), 50):
            data = self._get("videos", {"part": "snippet,statistics,contentDetails",
                                        "id": ",".join(ids[i:i + 50])})
            for item in data.get("items", []):
                sn = item["snippet"]
                if sn.get("liveBroadcastContent") in ("live", "upcoming"):
                    continue
                minutes = parse_iso_duration(item["contentDetails"].get("duration")) / 60
                views = int(item.get("statistics", {}).get("viewCount", 0))
                if not (yt["min_minutes"] <= minutes <= yt["max_minutes"]) or views < yt["min_views"]:
                    continue
                language = (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "")
                if src["languages"] and language and language.lower()[:2] not in src["languages"]:
                    continue
                published = datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
                hours = max(1.0, (self.now - published).total_seconds() / 3600)
                out.append(Candidate(
                    platform="youtube", video_id=item["id"],
                    url=f"https://www.youtube.com/watch?v={item['id']}",
                    channel=sn.get("channelTitle", ""), channel_key=sn.get("channelId", ""),
                    title=sn.get("title", ""), score=round(views / hours, 1), language=language))
        return sorted(out, key=lambda c: -c.score)


def discover(cfg, session=None, now=None):
    """Tous les candidats, plateformes alternées, meilleurs d'abord."""
    src, env = cfg["sources"], cfg["env"]
    lists = []
    if src["twitch"]["enabled"] and env.get("TWITCH_CLIENT_ID") and env.get("TWITCH_CLIENT_SECRET"):
        lists.append(TwitchDiscovery(cfg, session, now))
    if src["youtube"]["enabled"] and env.get("YOUTUBE_API_KEY"):
        lists.append(YouTubeDiscovery(cfg, session, now))
    results = []
    for finder in lists:
        try:
            results.append(finder.discover())
        except Exception as exc:  # une source en panne n'arrête pas l'autre
            print(f"[découverte] {type(finder).__name__} en échec : {exc}")
    merged = []
    for rank in range(max((len(r) for r in results), default=0)):
        merged += [r[rank] for r in results if rank < len(r)]
    for cand in merged:
        cand.allowed = is_allowed(cand, src["allowed_channels"])
    return merged
