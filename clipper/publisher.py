"""Publication des clips validés.

Buffer (offre gratuite : 3 canaux, clé d'API) publie sur Instagram et TikTok.
À défaut de clé Buffer : Instagram par l'API officielle de Meta, le reste par Upload-Post.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timedelta

import requests

from . import redact

UPLOAD_URL = "https://api.upload-post.com/api/upload"
INSTAGRAM_API = "https://graph.instagram.com/v26.0"
INSTAGRAM_REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
BUFFER_API = "https://api.buffer.com"
LITTERBOX_API = "https://litterbox.catbox.moe/resources/internals/api.php"
# litterbox filtre les envois sans navigateur déclaré : le 20/11, il a répondu 412
# au User-Agent de requests depuis GitHub Actions.
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"}


def host_temporarily(video_path, filename, session=None, keep="12h"):
    """Dépose la vidéo sur un hébergeur temporaire et renvoie son adresse directe.

    Buffer ne reçoit pas de fichier : il vient chercher la vidéo à une adresse
    publique, et un lien de partage Google Drive ne lui convient pas. Le fichier
    est effacé tout seul par l'hébergeur au bout de `keep` ; le clip est de toute
    façon destiné à être public.
    """
    http = session or requests.Session()
    with open(video_path, "rb") as fh:
        resp = http.post(LITTERBOX_API, data={"reqtype": "fileupload", "time": keep},
                         files={"fileToUpload": (filename, fh, "video/mp4")},
                         headers=BROWSER_HEADERS, timeout=900)
    url = (resp.text or "").strip()
    if resp.status_code >= 400 or not url.startswith("https://"):
        detail = re.sub(r"<[^>]+>|\s+", " ", url)[:120].strip()
        raise RuntimeError(f"hébergement temporaire refusé ({resp.status_code}) : {detail}")
    return url


class BufferPublisher:
    """Publication par Buffer (API GraphQL) : un post par canal, publié tout de suite."""

    YOUTUBE_GAMING = "20"   # catégorie « Jeux vidéo » de YouTube
    # Plafond du jour d'un réseau (le 20/11 : 2 Shorts par jour pour YouTube). Le clip sort
    # alors sans ce réseau : le retenter à chaque passage bloquerait les clips suivants.
    DAILY_LIMIT = re.compile(r"maximum number of posts", re.IGNORECASE)

    @classmethod
    def metadata(cls, platform, title):
        """Réglages propres à chaque réseau, joints au post ; None s'il n'en faut pas."""
        if platform == "instagram":
            return {"instagram": {"type": "reel", "shouldShareToFeed": True}}
        if platform == "youtube":
            # Un Short est une vidéo verticale de 3 min au plus : YouTube le classe tout seul.
            return {"youtube": {"title": (title or "")[:100], "categoryId": cls.YOUTUBE_GAMING,
                                "privacy": "public", "madeForKids": False}}
        return None
    CREATE = ("mutation CreatePost($input: CreatePostInput!) { createPost(input: $input) { __typename "
              "... on PostActionSuccess { post { id status } } ... on MutationError { message } } }")

    def __init__(self, env, session=None, sleep=time.sleep):
        self.key = env.get("BUFFER_API_KEY") or ""
        self.http = session or requests.Session()
        self.sleep = sleep
        self.sent = {}          # nom du clip → {plateforme: id du post Buffer}, gardé dans l'état
        self._channels = None

    @property
    def configured(self):
        return bool(self.key)

    def use_state(self, state, now):
        self.sent = state.setdefault("buffer_sent", {})

    def _gql(self, query, variables=None):
        resp = self.http.post(BUFFER_API, json={"query": query, "variables": variables or {}}, timeout=120,
                              headers={"Authorization": f"Bearer {self.key}",
                                       "Content-Type": "application/json"})
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or data.get("errors"):
            detail = "; ".join(str(e.get("message")) for e in data.get("errors") or []) or resp.text[:300]
            raise RuntimeError(f"Buffer {resp.status_code} : {detail[:400]}")
        return data.get("data") or {}

    def channels(self):
        """{réseau: id du canal} pour les comptes connectés à Buffer (premier canal de chaque réseau)."""
        if self._channels is None:
            found = {}
            for org in self._gql("query { account { organizations { id name } } }")["account"]["organizations"]:
                org_id = re.sub(r"[^A-Za-z0-9_-]", "", org["id"])
                listed = self._gql('query { channels(input: { organizationId: "%s" }) { id name service } }'
                                   % org_id)["channels"]
                for channel in listed:
                    found.setdefault(str(channel.get("service") or "").lower(), channel["id"])
            self._channels = found
        return self._channels

    def publish(self, video_path, filename, title, caption, platforms, video_url=None):
        done = self.sent.setdefault(filename, {})
        todo = [p for p in platforms if p not in done]   # un réseau déjà servi n'est jamais reposté
        if not todo:
            return {"buffer_posts": {p: i for p, i in done.items() if i}}
        channels = self.channels()
        missing = [p for p in todo if p not in channels]
        if missing:
            raise RuntimeError(f"Buffer : aucun canal connecté pour {', '.join(missing)} "
                               f"(canaux : {', '.join(sorted(channels)) or 'aucun'})")
        # Lien Drive public le temps de l'envoi (fourni par l'agent) ; litterbox en dernier recours.
        url = video_url or host_temporarily(video_path, filename, self.http)
        errors = []
        for platform in todo:
            entry = {"text": caption or title, "channelId": channels[platform],
                     "schedulingType": "automatic", "mode": "shareNow", "needsApproval": False,
                     "assets": [{"video": {"url": url}}]}
            extra = self.metadata(platform, title)
            if extra:
                entry["metadata"] = extra
            try:
                result = self._gql(self.CREATE, {"input": entry})["createPost"]
                if "post" not in result:
                    raise RuntimeError(f"Buffer : {result.get('message') or result.get('__typename')}")
                self._wait_sent(result["post"]["id"], platform)
                done[platform] = result["post"]["id"]
            except Exception as exc:
                if self.DAILY_LIMIT.search(str(exc)):
                    done[platform] = None   # noté comme traité : pas de nouvel essai pour ce clip
                    print(f"[publication] Buffer → {platform} : plafond du jour atteint, "
                          f"{filename} sort sans ce réseau", flush=True)
                    continue
                errors.append(f"{platform} : {exc}")
        if errors:
            raise RuntimeError(" | ".join(errors))
        return {"buffer_posts": {p: i for p, i in done.items() if i}}

    def _wait_sent(self, post_id, platform, tries=6, poll_seconds=30):
        """Attend que Buffer ait publié (ou refusé) le post. Sans réponse nette, on le tient pour parti."""
        safe = re.sub(r"[^A-Za-z0-9_-]", "", post_id)
        query = 'query { post(input: { id: "%s" }) { id status error { message } } }' % safe
        for _ in range(tries):
            self.sleep(poll_seconds)
            post = self._gql(query).get("post") or {}
            status = str(post.get("status") or "").lower()
            if status == "sent":
                # Le lien du post n'est pas écrit : le journal est public, le compte ne l'est pas ici.
                print(f"[publication] Buffer → {platform} : publié", flush=True)
                return
            if status == "error" or post.get("error"):
                raise RuntimeError(f"refusé par le réseau : {(post.get('error') or {}).get('message') or status}")
        print(f"[publication] Buffer → {platform} : encore en cours d'envoi, à vérifier dans Buffer", flush=True)

    def metrics(self, post_ids, batch=20):
        """{id du post: {"metrics": {type: valeur}, "updated_at": date}} pour des posts déjà publiés.

        Une seule requête pour `batch` posts (alias GraphQL) : l'offre gratuite compte les
        requêtes. Buffer relève les chiffres des réseaux une fois par jour ; un post tout
        juste publié n'en a pas encore, et un chiffre absent ne veut pas dire zéro.
        """
        ids = [i for i in dict.fromkeys(post_ids) if re.fullmatch(r"[A-Za-z0-9_-]+", str(i or ""))]
        out = {}
        for start in range(0, len(ids), batch):
            chunk = ids[start:start + batch]
            query = "query { %s }" % " ".join(
                'p%d: post(input: { id: "%s" }) { id metrics { type value } metricsUpdatedAt }' % (n, pid)
                for n, pid in enumerate(chunk))
            data = self._gql(query)
            for n, pid in enumerate(chunk):
                post = data.get(f"p{n}") or {}
                values = {}
                for metric in post.get("metrics") or []:
                    try:
                        values[str(metric["type"])] = float(metric["value"])
                    except (KeyError, TypeError, ValueError):
                        continue
                out[pid] = {"metrics": values, "updated_at": post.get("metricsUpdatedAt")}
        return out

    def info(self):
        """Canaux connectés et forme attendue des posts : pour vérifier le branchement sans rien publier."""
        lines = [f"Canaux Buffer : {json.dumps(self.channels(), ensure_ascii=False)}"]
        for name in ("CreatePostInput", "PostInputMetaData", "InstagramPostMetadataInput",
                     "TikTokPostMetadataInput", "YoutubePostMetadataInput", "YoutubePrivacy",
                     "PostStatus", "PostType"):
            try:
                found = self._gql('query { __type(name: "%s") { kind inputFields { name type { name kind ofType '
                                  '{ name kind } } } enumValues { name } } }' % name).get("__type")
            except Exception as exc:
                lines.append(f"{name} : {exc}")
                continue
            if not found:
                lines.append(f"{name} : type inconnu")
                continue
            fields = [f"{f['name']}:{f['type'].get('name') or (f['type'].get('ofType') or {}).get('name')}"
                      f"{'!' if f['type'].get('kind') == 'NON_NULL' else ''}"
                      for f in found.get("inputFields") or []]
            values = [v["name"] for v in found.get("enumValues") or []]
            lines.append(f"{name} : {', '.join(fields or values)}")
        return "\n".join(lines)


class UploadPostPublisher:
    def __init__(self, env, publishing, session=None):
        self.key = env.get("UPLOAD_POST_API_KEY")
        self.user = env.get("UPLOAD_POST_USER")
        self.publishing = publishing
        self.http = session or requests.Session()

    @property
    def configured(self):
        return bool(self.key and self.user)

    def publish(self, video_path, filename, title, caption, platforms=None):
        platforms = list(self.publishing["platforms"] if platforms is None else platforms)
        text = caption or title
        data = [("user", self.user), ("title", title[:100]), ("async_upload", "true")]
        data += [("platform[]", p) for p in platforms]
        if "tiktok" in platforms:
            data += [("tiktok_title", text[:2200]), ("post_mode", self.publishing["tiktok_post_mode"])]
        if "instagram" in platforms:
            data += [("instagram_title", text[:2200]), ("media_type", "REELS")]
        if "youtube" in platforms:
            data += [("youtube_title", title[:100]), ("youtube_description", text[:4900]),
                     ("privacyStatus", "public")]
        with open(video_path, "rb") as fh:
            resp = self.http.post(UPLOAD_URL, headers={"Authorization": f"Apikey {self.key}"},
                                  data=data, files={"video": (filename, fh, "video/mp4")}, timeout=600)
        if resp.status_code not in (200, 201, 202):
            raise RuntimeError(f"Upload-Post {resp.status_code} : {resp.text[:300]}")
        return resp.json()


class InstagramPublisher:
    """Reels publiés par l'API officielle (« Instagram API with Instagram Login »).

    Trois appels : créer un conteneur à partir d'une adresse publique de la vidéo,
    attendre qu'Instagram l'ait récupérée et traitée, publier le conteneur.
    """

    REFRESH_EVERY = timedelta(days=7)   # le jeton vit 60 jours ; on le prolonge bien avant

    def __init__(self, env, session=None, sleep=time.sleep):
        self.env_token = env.get("INSTAGRAM_ACCESS_TOKEN") or ""
        self.token = self.env_token
        self.user_id = env.get("INSTAGRAM_USER_ID") or ""
        self.http = session or requests.Session()
        self.sleep = sleep

    @property
    def configured(self):
        return bool(self.token and self.user_id)

    def _answer(self, resp):
        if resp.status_code >= 400:
            raise RuntimeError(f"Instagram {resp.status_code} : {resp.text[:300]}")
        return resp.json()

    def _get(self, url, **params):
        return self._answer(self.http.get(url, params=params, timeout=120,
                                          headers={"Authorization": f"Bearer {self.token}"}))

    def _post(self, url, **data):
        return self._answer(self.http.post(url, data=data, timeout=120,
                                           headers={"Authorization": f"Bearer {self.token}"}))

    def use_state(self, state, now):
        """Prolonge le jeton et le garde dans l'état de l'agent.

        Le jeton fourni (secret INSTAGRAM_ACCESS_TOKEN) expire au bout de 60 jours.
        Prolongé toutes les semaines, il ne s'éteint jamais ; un nouveau jeton mis
        dans le secret reprend la main dès le passage suivant.
        """
        if not self.configured:
            return
        seed = hashlib.sha256(self.env_token.encode()).hexdigest()[:12]
        kept = state.get("instagram") or {}
        if kept.get("seed") == seed and kept.get("token"):
            self.token = kept["token"]
            redact.mask(self.token)   # jeton prolongé : différent du secret, GitHub ne le masque pas seul
        else:
            kept = {"seed": seed, "token": self.env_token, "refreshed_at": now.isoformat()}
            state["instagram"] = kept
            return  # jeton tout neuf : rien à prolonger (il faut 24 h d'ancienneté)
        if now - datetime.fromisoformat(kept["refreshed_at"]) < self.REFRESH_EVERY:
            return
        try:
            data = self._answer(self.http.get(INSTAGRAM_REFRESH_URL, timeout=60, params={
                "grant_type": "ig_refresh_token", "access_token": self.token}))
        except Exception as exc:
            print(f"[publication] jeton Instagram non prolongé : {exc}", flush=True)
            return
        self.token = data.get("access_token") or self.token
        redact.mask(self.token)
        state["instagram"] = {"seed": seed, "token": self.token, "refreshed_at": now.isoformat()}

    def publish(self, video_url, caption, wait_seconds=300, poll_seconds=10):
        created = self._post(f"{INSTAGRAM_API}/{self.user_id}/media", media_type="REELS",
                             video_url=video_url, caption=(caption or "")[:2200], share_to_feed="true")
        container = created["id"]
        status, waited = {}, 0
        while True:
            status = self._get(f"{INSTAGRAM_API}/{container}", fields="status_code,status")
            code = status.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED") or waited >= wait_seconds:
                raise RuntimeError(f"Instagram n'a pas accepté la vidéo ({code}) : "
                                   f"{str(status.get('status') or '')[:200]}")
            self.sleep(poll_seconds)
            waited += poll_seconds
        published = self._post(f"{INSTAGRAM_API}/{self.user_id}/media_publish", creation_id=container)
        return {"instagram_media_id": published.get("id")}


class Publisher:
    """Aiguille les plateformes : tout par Buffer s'il a sa clé ; sinon Instagram en direct
    s'il a son jeton, et le reste par Upload-Post."""

    def __init__(self, env, publishing, session=None, sleep=time.sleep):
        self.publishing = publishing
        self.buffer = BufferPublisher(env, session, sleep)
        self.instagram = InstagramPublisher(env, session, sleep)
        self.upload_post = UploadPostPublisher(env, publishing, session)

    def _routes(self):
        platforms = list(self.publishing["platforms"])
        if self.buffer.configured:
            return [], []
        direct = ["instagram"] if "instagram" in platforms and self.instagram.configured else []
        return direct, [p for p in platforms if p not in direct]

    @property
    def configured(self):
        if self.buffer.configured:
            return bool(self.publishing["platforms"])
        direct, rest = self._routes()
        return bool(direct or rest) and (not rest or self.upload_post.configured)

    @property
    def needs_public_url(self):
        # Buffer comme Instagram en direct viennent chercher la vidéo à une adresse publique.
        return self.buffer.configured or bool(self._routes()[0])

    def use_state(self, state, now):
        self.buffer.use_state(state, now)
        if not self.buffer.configured:
            self.instagram.use_state(state, now)

    @property
    def has_metrics(self):
        return self.buffer.configured   # seul Buffer rend les chiffres des posts

    def metrics(self, post_ids):
        return self.buffer.metrics(post_ids)

    def publish(self, video_path, filename, title, caption, video_url=None):
        if self.buffer.configured:
            return self.buffer.publish(video_path, filename, title, caption, list(self.publishing["platforms"]),
                                       video_url=video_url)
        direct, rest = self._routes()
        result = {}
        # Upload-Post d'abord : s'il échoue, rien n'est encore sorti sur Instagram
        # et le clip, resté dans « Validé », sera retenté en entier.
        if rest:
            result.update(self.upload_post.publish(video_path, filename, title, caption, platforms=rest) or {})
        if direct:
            if not video_url:
                raise RuntimeError("Instagram : pas d'adresse publique pour la vidéo")
            result.update(self.instagram.publish(video_url, caption or title))
        return result
