"""Le cycle de l'agent. Chaque passage est court et sans attente :

1. collecte   : récupère les clips des montages terminés → dossier « À valider »
2. publication: publie ce qui a été déplacé dans « Validé » → « Publié »
3. découverte : cherche de nouvelles vidéos et les envoie au montage

Tout l'état (vidéos déjà vues, montages en cours, publications) vit dans un
fichier JSON du stockage, donc l'agent peut tourner n'importe où, en cron.
"""
import os
import re
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import dataclasses

from . import discovery, stats
from .engine import EngineBusy, QuotaExceeded
from .storage import split_description


def _now():
    return datetime.now(timezone.utc)


def _since(iso, now):
    return now - datetime.fromisoformat(iso)


def slug(text, limit=50):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()[:limit].strip("-") or "clip"


def _plain(text):
    """Texte écrit par l'IA, sans ses hashtags : ceux de la légende viennent du thème."""
    return re.sub(r"\s*#\w+", "", text or "").strip()


def clip_score(clip):
    """Note de 0 à 100 que le montage donne au clip, None s'il n'en donne pas."""
    try:
        return float(clip["predicted_score"])
    except (KeyError, TypeError, ValueError):
        return None


def clip_seconds(clip):
    """Durée du clip en secondes, None si le montage ne la donne pas."""
    try:
        return round(float(clip["end"]) - float(clip["start"]), 1)
    except (KeyError, TypeError, ValueError):
        return None


def build_description(cfg, clip, source, rewritten=None):
    """1re ligne = titre, suite = légende (c'est ce texte qui sera publié).

    `rewritten` : {"hook", "context"} réécrits pour le compte de clips (clipper/writer.py).
    """
    theme = cfg["theme"]
    title = (clip.get("video_title_for_youtube_short") or clip.get("title") or theme["name"]).strip()
    # Accroche et contexte écrits par l'IA pour ce clip ; à défaut, le titre.
    hook = (clip.get("viral_hook_text") or "").strip() or title
    about = _plain(clip.get("video_description_for_instagram")
                   or clip.get("video_description_for_tiktok")) or title
    if rewritten:
        hook, about = rewritten["hook"], rewritten["context"]
        # Le titre sert de titre au Short YouTube : lui aussi à la 3e personne, sans emoji.
        title = re.sub(r"[^\w\s'’.,:!?%-]", "", hook).strip() or title
    channel = source.get("channel") or "?"
    login = source.get("channel_key") or slug(channel).replace("-", "")
    credit = theme["credit_line"].format(channel=channel, platform=source["platform"].capitalize(),
                                         login=login)
    tags = list(theme["hashtags"])
    own = "#" + re.sub(r"[^a-z0-9_]", "", login.lower())
    if len(own) > 1 and own not in tags:
        tags.append(own)  # le streamer en hashtag : ses spectateurs retrouvent le clip
    # Le nom du compte de clips vient d'un secret (dépôt public) : sans lui, la ligne
    # du modèle qui le cite est retirée plutôt que publiée à moitié.
    account = ((cfg.get("env") or {}).get("ACCOUNT_HANDLE") or "").strip().lstrip("@")
    template = theme["caption_template"]
    if not account:
        template = "\n".join(line for line in template.split("\n") if "{account}" not in line)
    caption = template.format(title=title, hook=hook, description=about,
                              credit=credit, channel=channel, login=login, account=account,
                              hashtags=" ".join(tags)).strip()
    # 1re ligne = titre (YouTube), suite = légende publiée.
    return f"{title}\n\n{caption}\n"


class Agent:
    def __init__(self, cfg, storage, engine, publisher, now=None, finder=None, writer=None):
        self.cfg, self.storage, self.engine, self.publisher = cfg, storage, engine, publisher
        self.writer = writer
        self.now = now or _now()
        self.finder = finder or (lambda: discovery.discover(cfg, now=self.now))
        self.state = storage.load_state()
        for key, default in (("sources", {}), ("clips", {}), ("published", []), ("leads", [])):
            self.state.setdefault(key, default)
        if hasattr(publisher, "use_state"):
            publisher.use_state(self.state, self.now)  # jeton Instagram prolongé et gardé dans l'état

    def reset(self):
        """Repart de zéro : l'état actuel est gardé en copie, puis vidé.

        Les compteurs du jour, les vidéos déjà montées et les publications sont oubliés ;
        les clips présents dans Drive ne sont pas touchés.
        """
        backup = self.storage.backup_state(f"{self.now:%Y-%m-%d-%H%M}")
        stats.backfill(self.state)   # les clips déjà publiés restent mesurés après la remise à zéro
        kept = {k: self.state[k] for k in ("instagram", "stats", "stats_read_at") if k in self.state}
        self.state = {"sources": {}, "clips": {}, "published": [], "leads": [],
                      "reset_at": self.now.isoformat(), **kept}
        self.save()
        return backup

    def save(self):
        self.state["last_run"] = self.now.isoformat()
        self.storage.save_state(self.state)

    def log(self, message):
        print(message, flush=True)

    # ---------------------------------------------------------------- collecte
    def collect(self):
        added = 0
        for key, source in self.state["sources"].items():
            if source["status"] != "submitted":
                continue
            try:
                job = self.engine.status(source["job_id"])
            except Exception as exc:
                self.log(f"[collecte] {key} : statut indisponible ({exc})")
                continue
            if job.get("status") == "failed":
                # Le moteur n'a pas de champ « erreur » : la raison est dans les dernières lignes de son journal.
                tail = [str(line).strip() for line in (job.get("logs") or []) if str(line).strip()][-12:]
                reason = str(job.get("error") or (tail[-1] if tail else "") or "montage en échec")
                source.update(status="failed", error=reason[:300])
                self.log(f"[collecte] {key} : montage en échec ({reason[:200]})")
                for line in tail:
                    self.log(f"    | {line[:300]}")
                # Un montage perdu se remplace tout de suite, sans attendre le délai entre deux recherches.
                self.state.pop("last_discovery", None)
                continue
            if job.get("status") != "completed":
                if _since(source["submitted_at"], self.now) > timedelta(hours=12):
                    source.update(status="failed", error="montage sans réponse depuis 12 h")
                continue
            clips = (job.get("result") or {}).get("clips") or []
            kept = self._kept(clips)
            done = set(source.setdefault("clips_stored", []))
            for index, clip in enumerate(clips):
                if index in done or index not in kept:
                    continue
                title = clip.get("video_title_for_youtube_short") or clip.get("title") or ""
                # La note de l'IA en tête du nom : dans Drive, les meilleurs se repèrent d'un coup d'œil.
                score = clip_score(clip)
                note = "" if score is None else f"note{score:.0f}_"
                # L'identifiant de la source évite que deux montages d'un même streamer
                # (live remonté plusieurs fois) donnent le même nom de fichier.
                name = (f"{self.now:%Y-%m-%d}_{note}{slug(source.get('channel'), 20)}_{slug(title)}"
                        f"_{slug(key)[-6:]}-{index + 1}.mp4")
                rewritten = self._rewrite(clip, source, index)
                description = build_description(self.cfg, clip, source, rewritten)
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, "clip.mp4")
                    self.engine.download(clip, path)
                    self.storage.put_clip(path, name, description, "review")
                # Durée, note et accroche : ce que la mesure croisera avec les vues.
                self.state["clips"][name] = {"source": key, "source_url": source["url"],
                                             "channel": source.get("channel"),
                                             "added_at": self.now.isoformat(),
                                             "seconds": clip_seconds(clip), "score": score,
                                             "hook": (rewritten or {}).get("hook")
                                                     or (clip.get("viral_hook_text") or "").strip()}
                source["clips_stored"].append(index)
                added += 1
                self.save()  # un clip déposé n'est jamais redéposé
            source["status"] = "done"
            dropped = f" ({len(clips) - len(kept)} écarté(s) au tri)" if len(kept) < len(clips) else ""
            self.log(f"[collecte] {key} : {len(kept)} clip(s) à valider{dropped}")
        return added

    def _rewrite(self, clip, source, index):
        """Accroche et contexte réécrits pour le compte de clips ; None = légende du montage gardée."""
        if not (self.writer and self.writer.configured):
            return None
        try:
            words = ""
            if hasattr(self.engine, "transcript"):
                words = self.engine.transcript(source["job_id"], clip, index)
            return self.writer.rewrite(clip, source, words, self.cfg["theme"]["name"])
        except Exception as exc:
            self.log(f"[légende] {source.get('channel')} clip {index + 1} : légende du montage gardée ({exc})")
            return None

    def _kept(self, clips):
        """Indices des clips gardés : au-dessus de `min_score`, puis les `keep_best` mieux notés.

        Un clip sans note n'est jamais écarté par le seuil.
        """
        c = self.cfg["clipping"]
        floor = c.get("min_score") or 0
        ranked = [i for i in range(len(clips))
                  if clip_score(clips[i]) is None or clip_score(clips[i]) >= floor]
        ranked.sort(key=lambda i: -(clip_score(clips[i]) or 0))  # tri stable : ordre du montage à égalité
        if c.get("keep_best"):
            ranked = ranked[:c["keep_best"]]
        return set(ranked)

    def _clips_per_source(self):
        """Clips attendus d'un montage, une fois le tri fait."""
        c = self.cfg["clipping"]
        return min(c["target_clips"], c.get("keep_best") or c["target_clips"])

    # ------------------------------------------------------------- publication
    def publish(self):
        if self.cfg["publishing"].get("auto_approve"):
            for clip in self.storage.list_clips("review"):
                self.storage.move(clip, "review", "approved")
                self.log(f"[publication] {clip.name} validé automatiquement")
        self.copy_for_manual()  # retente les copies restées en échec
        approved = self.storage.list_clips("approved")
        # Garde-fou : on ne publie que les clips d'un jeu du thème (GTA VI).
        games = {g.lower() for g in self.cfg["sources"]["twitch"]["game_names"]}
        kept = []
        for clip in approved:
            game = self._clip_game(clip.name)
            if game is not None and game.lower() not in games:
                self.storage.move(clip, "approved", "rejected")
                self.log(f"[publication] {clip.name} refusé : jeu « {game or 'inconnu'} » hors thème")
            else:
                kept.append(clip)
        approved = kept
        if not approved:
            return 0
        pub_platforms = self.cfg["publishing"]["platforms"]
        if pub_platforms and not self.publisher.configured:
            self.log(f"[publication] {len(approved)} clip(s) validé(s) mais la publication sur "
                     f"{', '.join(pub_platforms)} n'est pas configurée (clé Buffer, jeton Instagram "
                     f"ou Upload-Post)")
            return 0
        pub = self.cfg["publishing"]
        last_day = [p for p in self.state["published"]
                    if _since(p["at"], self.now) < timedelta(hours=24)]
        budget = min(pub["max_per_run"], pub["max_per_day"] - len(last_day))
        count = 0
        already = {p["name"] for p in self.state["published"]}
        for clip in [c for c in approved if c.name in already]:
            self.storage.move(clip, "approved", "published")  # déplacement resté en plan
        approved = [c for c in approved if c.name not in already]
        approved.sort(key=self._freshness_key, reverse=True)  # le plus frais sort en premier
        for clip in approved[:max(0, budget)]:
            title, caption = split_description(clip.description)
            title = title or os.path.splitext(clip.name)[0]
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, "clip.mp4")
                    result = None
                    if pub_platforms:  # sinon : tout est posté à la main
                        self.storage.fetch(clip, "approved", path)
                        result = self._send(clip, path, title, caption)
            except Exception as exc:
                self.log(f"[publication] {clip.name} : échec ({exc})")
                self.state["clips"].setdefault(clip.name, {})["publish_error"] = str(exc)[:300]
                continue
            # Enregistré avant le déplacement : un clip publié n'est jamais republié.
            record = {"name": clip.name, "at": self.now.isoformat(), "platforms": pub["platforms"],
                      "request_id": (result or {}).get("request_id"),
                      "instagram_media_id": (result or {}).get("instagram_media_id"),
                      "buffer_posts": (result or {}).get("buffer_posts")}
            if pub.get("manual_platforms"):
                record["manual_pending"] = True
            self.state["published"].append(record)
            info = self.state["clips"].get(clip.name) or {}
            stats.register(self.state, clip.name, record["buffer_posts"], record["at"],
                           {k: info.get(k) for k in ("channel", "seconds", "score", "hook")})
            self.save()
            self.storage.move(clip, "approved", "published")
            self.log(f"[publication] {clip.name} → {', '.join(pub['platforms']) or 'à la main uniquement'}")
            count += 1
        self.copy_for_manual()
        return count

    def _freshness_key(self, clip):
        """Date du live d'origine (dernier clip Twitch), à défaut celle du dépôt dans Drive."""
        info = self.state["clips"].get(clip.name) or {}
        source = self.state["sources"].get(info.get("source"), {})
        return source.get("fresh_at") or source.get("submitted_at") or info.get("added_at") or ""

    def _send(self, clip, path, title, caption):
        """Publie un clip ; s'il le faut, il est rendu public dans Drive juste le temps de l'envoi."""
        if not getattr(self.publisher, "needs_public_url", False):
            return self.publisher.publish(path, clip.name, title, caption)
        url = self.storage.share(clip)
        try:
            return self.publisher.publish(path, clip.name, title, caption, video_url=url)
        finally:
            try:
                self.storage.unshare(clip)
            except Exception as exc:
                self.log(f"[publication] {clip.name} : partage public non retiré ({exc})")

    def _clip_game(self, name):
        """Jeu de la source d'un clip, "" si inconnu, None si le clip n'est pas suivi par l'agent."""
        info = self.state["clips"].get(name)
        if info is None:
            return None  # déposé à la main dans « Validé » : on laisse passer
        source = self.state["sources"].get(info.get("source"), {})
        if source.get("platform") == "youtube":
            return self.cfg["sources"]["twitch"]["game_names"][0]  # recherches YouTube = thème
        return source.get("game", "")

    def copy_for_manual(self):
        """Copie les clips publiés dans le dossier des plateformes postées à la main (TikTok).

        Une seule copie par clip ; une copie en échec est retentée au passage suivant.
        """
        pending = [p for p in self.state["published"] if p.get("manual_pending")]
        if not pending:
            return 0
        published = {c.name: c for c in self.storage.list_clips("published")}
        manual = ", ".join(self.cfg["publishing"].get("manual_platforms") or [])
        copied = 0
        for record in pending:
            clip = published.get(record["name"])
            if clip is None:
                continue  # pas encore (ou plus) dans « Publié » : on réessaiera
            try:
                # La copie ne garde que la légende : le texte à coller tel quel dans l'app.
                caption = split_description(clip.description)[1] or clip.description
                self.storage.copy(dataclasses.replace(clip, description=caption), "published", "manual")
            except Exception as exc:
                self.log(f"[publication] {clip.name} : copie pour {manual} en échec ({exc})")
                continue
            record.pop("manual_pending")
            self.save()
            self.log(f"[publication] {clip.name} → à poster à la main ({manual})")
            copied += 1
        return copied

    # ------------------------------------------------------------------ mesure
    def measure(self):
        """Relève vues et abonnés gagnés des clips publiés, et réécrit le relevé dans le stockage."""
        read = stats.collect(self.state, self.publisher, self.now, self.cfg, self.log)
        if read:
            self.storage.save_report(stats.REPORT_NAME, stats.to_csv(self.state))
        return read

    # -------------------------------------------------------------- découverte
    def _montage_caps(self):
        """Montages encore permis maintenant (moteur et plafond du jour), stock mis à part."""
        clipping, sources = self.cfg["clipping"], self.state["sources"]
        in_flight = sum(1 for s in sources.values() if s["status"] == "submitted")
        today = sum(1 for s in sources.values()
                    if s.get("submitted_at") and _since(s["submitted_at"], self.now) < timedelta(hours=24))
        return min(clipping["max_jobs_in_flight"] - in_flight, clipping["max_sources_per_day"] - today)

    def _is_fresh(self, cand):
        """Live tout juste clippé : il passe devant le stock pour sortir vite."""
        hours = self.cfg["sources"]["twitch"].get("fresh_hours") or 0
        age = discovery.age_hours(cand.fresh_at, self.now) if cand.fresh_at else None
        return bool(hours) and age is not None and age < hours

    def _montage_budget(self):
        """Nombre de montages à lancer maintenant pour tenir le stock de clips."""
        clipping, sources = self.cfg["clipping"], self.state["sources"]
        pub = self.cfg["publishing"]
        in_flight = sum(1 for s in sources.values() if s["status"] == "submitted")
        # On ne monte que ce qu'il faut : garder d'avance `stock_days` jours de
        # clips (à valider + validés + montages en cours) pour tenir l'objectif.
        per_source = self._clips_per_source()
        stock = (len(self.storage.list_clips("review")) + len(self.storage.list_clips("approved"))
                 + in_flight * per_source)
        missing = pub["min_per_day"] * pub["stock_days"] - stock
        need = -(-missing // per_source) if missing > 0 else 0
        return min(self._montage_caps(), need)

    def _discovery_due(self):
        last = self.state.get("last_discovery")
        interval = timedelta(hours=self.cfg["sources"]["interval_hours"])
        return not (last and _since(last, self.now) < interval)

    def needs_engine(self):
        """Vrai si ce passage a besoin du moteur de montage : un montage à relever ou à lancer.

        Sert au lancement sur GitHub Actions, où le moteur n'est démarré que s'il le faut.
        """
        if any(s["status"] == "submitted" for s in self.state["sources"].values()):
            return True
        if self._montage_caps() <= 0 or not self._discovery_due():
            return False
        return bool(self.discover_and_submit(dry_run=True))

    def discover_and_submit(self, dry_run=False):
        sources = self.state["sources"]
        budget = self._montage_budget()
        if not dry_run:
            # Chaque recherche consomme du quota (YouTube : 10 000 unités/jour,
            # 100 par requête) : on ne cherche que si un montage est permis,
            # et pas plus d'une fois par `interval_hours`.
            if self._montage_caps() <= 0 or not self._discovery_due():
                return 0
            self.state["last_discovery"] = self.now.isoformat()

        candidates = self.finder()
        only_allowed = self.cfg["sources"]["only_allowed_channels"]
        fresh = []
        for cand in candidates:
            if cand.key not in sources:
                fresh.append(cand)
            else:
                follow = self._next_part(cand)
                if follow:
                    fresh.append(follow)
        eligible = [c for c in fresh if c.allowed or not only_allowed]
        # Pistes : chaînes qui marchent mais non autorisées → à contacter.
        self.state["leads"] = [c.to_dict() for c in fresh if c not in eligible][:20]
        if budget <= 0:
            # Stock plein : seuls les lives tout juste clippés passent, pour sortir vite.
            eligible = [c for c in eligible if self._is_fresh(c)]
            budget = self._montage_caps()
        if dry_run:
            return eligible

        submitted = 0
        for cand in eligible:
            if submitted >= budget:
                break
            record = {**cand.to_dict(), "submitted_at": self.now.isoformat()}
            try:
                job_id = self.engine.submit(cand)
            except QuotaExceeded as exc:
                self.log(f"[montage] quota OpenShorts épuisé : {exc}")
                break
            except EngineBusy:
                self.log("[montage] moteur occupé, on réessaiera au prochain passage")
                break
            except Exception as exc:
                self.log(f"[montage] {cand.key} : envoi en échec ({exc})")
                continue
            if job_id is None:
                sources[cand.key] = {**record, "status": "skipped", "error": "qualité source trop basse"}
                continue
            sources[cand.key] = {**record, "status": "submitted", "job_id": job_id}
            self.save()
            self.log(f"[montage] {cand.key} ({cand.channel}) envoyé : {cand.title[:60]}")
            submitted += 1
        return submitted

    def _next_part(self, cand):
        """Live en cours (ou long) déjà monté : nouveau montage avec les clips pas encore utilisés."""
        if not cand.pool:
            return None
        tw = self.cfg["sources"]["twitch"]
        parts = [s for k, s in self.state["sources"].items()
                 if k == cand.base_key or k.startswith(cand.base_key + "#")]
        if any(s["status"] == "submitted" for s in parts):
            return None  # on attend la fin du montage en cours de cette VOD
        last = max((s["submitted_at"] for s in parts if s.get("submitted_at")), default=None)
        if last and _since(last, self.now) < timedelta(minutes=tw["resubmit_minutes"]):
            return None
        used = [c for s in parts if s["status"] in ("submitted", "done") for c in (s.get("clips") or [])]
        # Ni les clips déjà montés, ni un autre clip du même moment.
        remaining = [c for c in cand.pool
                     if not any(c["offset"] < u["offset"] + u["duration"]
                                and u["offset"] < c["offset"] + c["duration"] for u in used)]
        views = sum(c["views"] for c in remaining)
        picked = discovery.pick_clips(remaining, self.cfg["clipping"]["max_minutes_per_source"] * 60)
        if views < tw["min_clip_views"] or sum(c["duration"] for c in picked) < tw["min_resubmit_seconds"]:
            return None
        return dataclasses.replace(cand, part=len(parts), clips=picked,
                                   score=float(views) * discovery.freshness(cand.fresh_at, self.now, tw))

    # ------------------------------------------------------------------- cycle
    def run(self, wait_minutes=0, sleep=time.sleep):
        """Un passage. `wait_minutes` > 0 : on attend la fin des montages avant de rendre la main."""
        steps = [("collecte", self.collect), ("publication", self.publish), ("mesure", self.measure),
                 ("découverte", self.discover_and_submit)]
        if wait_minutes:
            steps.append(("attente", lambda: self.wait_for_jobs(wait_minutes, sleep)))
            # Clips d'un montage tout juste fini : publiés dans la foulée, sans attendre le passage suivant.
            steps.append(("publication du montage", self.publish))
        results = {}
        for name, step in steps:
            try:
                results[name] = step()
            except Exception as exc:  # une étape en panne ne bloque pas les autres
                self.log(f"[{name}] erreur : {exc}")
                results[name] = None
            self.save()
        self.log(self.goal_report())
        return results

    def wait_for_jobs(self, minutes, sleep=time.sleep, poll_seconds=30):
        """Attend la fin des montages en cours et dépose leurs clips (au plus `minutes`).

        Sur GitHub Actions le moteur disparaît avec la machine à la fin du passage :
        un montage qui n'a pas fini à temps est perdu, donc marqué en échec tout de
        suite plutôt que de bloquer les montages suivants pendant 12 h.
        """
        def pending():
            return [k for k, s in self.state["sources"].items() if s["status"] == "submitted"]

        deadline = time.monotonic() + minutes * 60
        added, polls = 0, 0
        while pending():
            added += self.collect()
            self.save()
            if not pending() or time.monotonic() >= deadline:
                break
            if polls % 10 == 0:  # une ligne toutes les 5 minutes : le passage n'est pas muet
                self.log(f"[attente] montage en cours ({', '.join(pending())})")
            polls += 1
            sleep(poll_seconds)
        for key in pending():
            self.state["sources"][key].update(status="failed",
                                              error=f"montage non terminé en {minutes:g} min")
            self.state.pop("last_discovery", None)
            self.log(f"[attente] {key} : montage non terminé en {minutes:g} min, abandonné")
        if added:
            self.publish()  # validation automatique éventuelle des clips tout juste déposés
        return added

    def goal_report(self):
        """Où en est l'objectif de publications par jour, et ce qui le bloque."""
        goal = self.cfg["publishing"]["min_per_day"]
        done = sum(1 for p in self.state["published"] if _since(p["at"], self.now) < timedelta(hours=24))
        approved = len(self.storage.list_clips("approved"))
        review = len(self.storage.list_clips("review"))
        line = f"Objectif {goal}/jour : {done} publié(s) sur 24 h, {approved} validé(s) en attente, {review} à valider"
        if done + approved >= goal:
            return line
        missing = goal - done - approved
        if review:
            more = "" if review >= missing else " (les suivants sont en cours de montage ou de recherche)"
            return f"⚠ {line} → validez {min(review, missing)} clip(s) dans Drive pour tenir l'objectif{more}"
        if any(s["status"] == "submitted" for s in self.state["sources"].values()):
            return f"{line} → montages en cours"
        if not any(self.cfg["sources"]["allowed_channels"].values()) and self.cfg["sources"]["only_allowed_channels"]:
            return f"⚠ {line} → aucune chaîne autorisée dans config.yaml, l'agent n'a rien à clipper"
        return f"⚠ {line} → pas assez de clips : ajoutez des chaînes ou baissez les seuils de vues"

    def summary(self):
        by_status = {}
        for source in self.state["sources"].values():
            by_status[source["status"]] = by_status.get(source["status"], 0) + 1
        lines = [f"Dernier passage : {self.state.get('last_run', 'jamais')}",
                 f"Vidéos sources  : {by_status or 'aucune'}"]
        lines.append(self.goal_report())
        if self.cfg["publishing"].get("auto_approve"):
            lines.append("⚠ Validation automatique ACTIVE : les clips sont publiés sans relecture")
        for key in ("review", "approved", "published", "rejected", "manual"):
            lines.append(f"{self.cfg['storage']['folders'][key]:<15} : {len(self.storage.list_clips(key))} clip(s)")
        if self.state["leads"]:
            lines.append("Pistes (chaînes non autorisées qui performent) :")
            lines += [f"  - {l['channel']} ({l['platform']}) score {l['score']:.0f} {l['url']}"
                      for l in self.state["leads"][:10]]
        return "\n".join(lines)
