import dataclasses
import os
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

import contextlib
import io

from clipper import config, discovery, redact, stats, writer as writer_mod
from clipper import engine as engine_mod
from clipper.discovery import Candidate, parse_iso_duration, pick_clips
from clipper.engine import OpenShortsEngine, QuotaExceeded
from clipper.pipeline import Agent, build_description, slug
from clipper.publisher import BufferPublisher, InstagramPublisher, Publisher, UploadPostPublisher
from clipper.storage import LocalStorage, split_description

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


class FakeEngine:
    def __init__(self):
        self.jobs, self.submitted, self.fail_with = {}, [], None

    def submit(self, cand):
        if self.fail_with:
            raise self.fail_with
        self.submitted.append(cand)
        job_id = f"job{len(self.submitted)}"
        self.jobs[job_id] = {"status": "processing"}
        return job_id

    def finish(self, job_id, n=2):
        self.jobs[job_id] = {"status": "completed", "result": {"clips": [
            {"title": f"Braquage raté {i}", "video_description_for_tiktok": "Il a tout perdu #gta6",
             "video_url": f"/videos/{job_id}/c{i}.mp4"} for i in range(n)]}}

    def status(self, job_id):
        return self.jobs[job_id]

    def download(self, clip, dest):
        with open(dest, "wb") as fh:
            fh.write(b"mp4:" + clip["video_url"].encode())


class FakePublisher:
    configured = True

    def __init__(self):
        self.posts, self.fail = [], False

    def publish(self, path, filename, title, caption):
        if self.fail:
            raise RuntimeError("boom")
        self.posts.append((filename, title, caption, open(path, "rb").read()))
        return {"request_id": f"r{len(self.posts)}"}


def cand(n, platform="twitch", channel="streamer"):
    return Candidate(platform, f"v{n}", f"https://example/{n}", channel, channel, f"Titre {n}",
                     score=100 - n, source_start=10, source_end=1810, game="Grand Theft Auto VI")


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.load(None, env={})
        self.cfg["sources"]["allowed_channels"]["twitch"] = ["streamer"]
        self.storage = LocalStorage(self.tmp.name, self.cfg["storage"]["folders"])
        self.engine, self.publisher = FakeEngine(), FakePublisher()
        self.found = [cand(1), cand(2), cand(3), cand(4, channel="inconnu")]

    def agent(self, now=NOW):
        def finder():
            for c in self.found:
                c.allowed = discovery.is_allowed(c, self.cfg["sources"]["allowed_channels"])
            return self.found
        return Agent(self.cfg, self.storage, self.engine, self.publisher, now=now, finder=finder)

    def test_fresh_live_jumps_a_full_stock(self):
        self.cfg["sources"]["twitch"]["fresh_hours"] = 3
        self.cfg["publishing"]["stock_days"] = 1
        self.cfg["clipping"]["max_jobs_in_flight"] = 1
        for i in range(3):  # stock plein : 3 clips déjà validés
            path = os.path.join(self.tmp.name, f"c{i}.mp4")
            open(path, "wb").close()
            self.storage.put_clip(path, f"old{i}.mp4", "", "approved")
        old, new = cand(1), cand(2)
        old.fresh_at = (NOW - timedelta(hours=20)).isoformat()
        new.fresh_at = (NOW - timedelta(minutes=40)).isoformat()
        self.found = [old, new]
        self.agent().discover_and_submit()
        self.assertEqual([c.video_id for c in self.engine.submitted], ["v2"])

    def test_stale_lives_wait_when_the_stock_is_full(self):
        self.cfg["sources"]["twitch"]["fresh_hours"] = 3
        self.cfg["publishing"]["stock_days"] = 1
        for i in range(3):
            path = os.path.join(self.tmp.name, f"c{i}.mp4")
            open(path, "wb").close()
            self.storage.put_clip(path, f"old{i}.mp4", "", "approved")
        old = cand(1)
        old.fresh_at = (NOW - timedelta(hours=20)).isoformat()
        self.found = [old]
        self.assertFalse(self.agent().needs_engine())

    def test_freshness_halves_views_every_half_life(self):
        tw = {"half_life_hours": 6}
        self.assertAlmostEqual(discovery.freshness((NOW - timedelta(hours=12)).isoformat(), NOW, tw), 0.25)
        self.assertEqual(discovery.freshness("", NOW, tw), 1.0)
        self.assertEqual(discovery.freshness("2026-11-20T06:00:00Z", NOW, {"half_life_hours": 0}), 1.0)

    def test_full_cycle(self):
        # 1er passage : 2 montages lancés (max_jobs_in_flight), rien d'autre.
        self.agent().run()
        self.assertEqual([c.video_id for c in self.engine.submitted], ["v1", "v2"])
        self.assertEqual(self.storage.list_clips("review"), [])

        # Le montage 1 se termine : ses clips arrivent dans « À valider ».
        self.engine.finish("job1")
        agent = self.agent(NOW + timedelta(minutes=30))
        agent.run()
        review = self.storage.list_clips("review")
        self.assertEqual(len(review), 2)
        self.assertIn("Crédit : streamer (Twitch)", review[0].description)
        # 2 clips à valider + 1 montage en cours = stock suffisant : pas de 3e montage.
        self.assertEqual([c.video_id for c in self.engine.submitted], ["v1", "v2"])
        self.assertIn("validez 2 clip(s)", agent.goal_report())
        # La chaîne non autorisée n'est jamais clippée, seulement signalée.
        self.assertEqual([l["channel"] for l in agent.state["leads"]], ["inconnu"])

        # Rien n'est publié sans validation.
        self.assertEqual(self.publisher.posts, [])

        # Validation = déplacer le fichier. Un seul clip publié par passage.
        self.storage.move(review[0], "review", "approved")
        self.storage.move(review[1], "review", "approved")
        self.agent(NOW + timedelta(hours=1)).run()
        self.assertEqual(len(self.publisher.posts), 1)
        name, title, caption, data = self.publisher.posts[0]
        self.assertEqual(title, "Braquage raté 0")
        self.assertIn("#gtavi", caption)
        self.assertTrue(data.startswith(b"mp4:/videos/job1/"))
        self.assertEqual(len(self.storage.list_clips("published")), 1)
        self.assertEqual(len(self.storage.list_clips("approved")), 1)

        # Les passages suivants ne redéposent ni ne republient rien.
        self.agent(NOW + timedelta(hours=2)).run()
        self.assertEqual(len(self.publisher.posts), 2)
        self.agent(NOW + timedelta(hours=3)).run()
        self.assertEqual(len(self.publisher.posts), 2)
        self.assertEqual(len(self.storage.list_clips("review")), 0)

    def test_stock_is_refilled_to_hold_the_daily_goal(self):
        self.agent().run()
        self.engine.finish("job1", n=1)
        self.engine.finish("job2", n=1)
        self.agent(NOW + timedelta(minutes=30)).run()
        # 2 clips en stock pour un objectif de 6, mais recherche faite il y a 30 min : on attend.
        self.assertEqual(len(self.engine.submitted), 2)
        self.agent(NOW + timedelta(hours=3)).run()   # délai passé : un montage de plus
        self.assertEqual([c.video_id for c in self.engine.submitted], ["v1", "v2", "v3"])
        self.cfg["sources"]["allowed_channels"]["twitch"] = []
        for clip in self.storage.list_clips("review"):
            self.storage.move(clip, "review", "rejected")
        self.assertIn("montages en cours", self.agent().goal_report())
        self.engine.jobs["job3"] = {"status": "failed"}
        agent = self.agent()
        agent.run()
        self.assertIn("aucune chaîne autorisée", agent.goal_report())

    def test_edited_caption_is_what_gets_published(self):
        self.agent().run()
        self.engine.finish("job1", n=1)
        self.agent().run()
        clip = self.storage.list_clips("review")[0]
        path = os.path.join(self.tmp.name, self.cfg["storage"]["folders"]["review"], clip.name + ".txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("Mon titre\n\nMa légende #perso\n")
        self.storage.move(clip, "review", "approved")
        self.agent().run()
        self.assertEqual(self.publisher.posts[0][1:3], ("Mon titre", "Ma légende #perso"))

    def test_daily_caps(self):
        self.cfg["publishing"].update(max_per_run=10, max_per_day=3)
        self.cfg["clipping"].update(max_jobs_in_flight=10, max_sources_per_day=2)
        self.agent().run()
        self.assertEqual(len(self.engine.submitted), 2)
        self.engine.finish("job1", n=5)
        self.agent().run()
        for clip in self.storage.list_clips("review"):
            self.storage.move(clip, "review", "approved")
        self.agent().run()
        self.agent().run()
        self.assertEqual(len(self.publisher.posts), 3)
        self.agent(NOW + timedelta(hours=25)).run()
        self.assertEqual(len(self.publisher.posts), 5)

    def test_publish_failure_keeps_clip_approved(self):
        self.agent().run()
        self.engine.finish("job1", n=1)
        self.agent().run()
        self.storage.move(self.storage.list_clips("review")[0], "review", "approved")
        self.publisher.fail = True
        self.agent().run()
        self.assertEqual(len(self.storage.list_clips("approved")), 1)
        self.publisher.fail = False
        self.agent().run()
        self.assertEqual(len(self.storage.list_clips("published")), 1)

    def test_only_theme_game_is_published(self):
        self.found = [cand(1), dataclasses.replace(cand(2), game="Grand Theft Auto V")]
        self.cfg["publishing"].update(auto_approve=True, max_per_run=10)
        self.agent().run()
        self.engine.finish("job1", n=1)
        self.engine.finish("job2", n=1)
        self.agent().run()
        self.assertEqual(len(self.publisher.posts), 1)
        self.assertIn("job1", self.publisher.posts[0][3].decode())
        rejected = self.storage.list_clips("rejected")
        self.assertEqual(len(rejected), 1)

    def test_manual_only_publishing_without_upload_post(self):
        self.cfg["publishing"].update(auto_approve=True, platforms=[],
                                      manual_platforms=["instagram", "tiktok"])
        self.publisher.configured = False
        self.agent().run()
        self.engine.finish("job1", n=1)
        self.agent().run()
        self.assertEqual(self.publisher.posts, [])
        self.assertEqual(len(self.storage.list_clips("published")), 1)
        self.assertEqual(len(self.storage.list_clips("manual")), 1)

    def test_caption_is_consistent(self):
        source = {"platform": "twitch", "channel": "Chowh1", "channel_key": "chowh1"}
        desc = build_description(self.cfg, {"title": "Il rate tout", "video_description_for_tiktok": "bla #rp"}, source)
        self.assertEqual(desc, "Il rate tout\n\nIl rate tout\n\n🎥 Crédit : Chowh1 (Twitch)\n"
                               "#gta6 #gtavi #gaming #clips #chowh1\n")

    def test_auto_approve_and_manual_tiktok_folder(self):
        self.cfg["publishing"].update(auto_approve=True, manual_platforms=["tiktok"])
        self.agent().run()
        self.engine.finish("job1", n=2)
        # Collecte puis publication dans le même passage, sans validation humaine.
        self.agent().run()
        self.assertEqual(len(self.publisher.posts), 1)
        manual = self.storage.list_clips("manual")
        self.assertEqual([c.name for c in manual], [self.publisher.posts[0][0]])
        self.assertIn("Crédit : streamer (Twitch)", manual[0].description)
        self.assertFalse(manual[0].description.startswith("Braquage raté 0\n\nBraquage"))
        self.agent(NOW + timedelta(minutes=30)).run()
        self.agent(NOW + timedelta(hours=1)).run()
        # Chaque clip publié n'est copié qu'une fois.
        self.assertEqual(len(self.publisher.posts), 2)
        self.assertEqual(len(self.storage.list_clips("manual")), 2)
        self.assertIn("Validation automatique ACTIVE", self.agent().summary())

    def test_manual_copy_is_retried(self):
        self.cfg["publishing"].update(auto_approve=True, manual_platforms=["tiktok"])
        self.agent().run()
        self.engine.finish("job1", n=1)
        copy, calls = self.storage.copy, []

        def broken(*args):
            calls.append(args)
            raise RuntimeError("Drive indisponible")
        self.storage.copy = broken
        self.agent().run()
        self.assertEqual((len(self.publisher.posts), len(calls)), (1, 1))
        self.storage.copy = copy
        self.agent().run()
        self.assertEqual(len(self.publisher.posts), 1)   # pas republié
        self.assertEqual(len(self.storage.list_clips("manual")), 1)

    def test_ongoing_live_is_clipped_again_with_new_clips(self):
        self.cfg["clipping"].update(max_jobs_in_flight=1, max_minutes_per_source=2)
        self.cfg["sources"]["interval_hours"] = 0.4
        self.cfg["sources"]["twitch"]["min_resubmit_seconds"] = 90

        def clip(i, offset, views, dur=60):
            return {"url": f"https://clips/{i}", "offset": offset, "duration": dur, "views": views}
        live = cand(1)
        live.pool = [clip(1, 0, 500), clip(2, 100, 400)]
        live.clips = discovery.pick_clips(live.pool, 120)
        self.found = [live]
        self.agent().run()
        self.assertEqual([c.key for c in self.engine.submitted], ["twitch:v1"])
        self.engine.finish("job1", n=1)
        # Le live continue : 4 nouveaux clips, dont 1 du même moment qu'un clip déjà monté.
        live.pool += [clip(3, 110, 900), clip(4, 3600, 300), clip(5, 3700, 300), clip(6, 3800, 300)]
        self.agent(NOW + timedelta(minutes=30)).run()
        self.assertEqual(len(self.engine.submitted), 1)   # moins d'une heure : on attend
        self.agent(NOW + timedelta(minutes=70)).run()
        second = self.engine.submitted[1]
        self.assertEqual(second.key, "twitch:v1#1")
        self.assertEqual([c["url"] for c in second.clips], ["https://clips/4", "https://clips/5"])
        self.engine.finish("job2", n=1)
        # Plus assez de nouveaux clips (60 s < 90 s) : pas de 3e montage.
        self.agent(NOW + timedelta(hours=3)).run()
        self.assertEqual(len(self.engine.submitted), 2)

    def test_quota_and_failed_job(self):
        self.engine.fail_with = QuotaExceeded("plus de minutes")
        self.assertEqual(self.agent().run()["découverte"], 0)
        self.engine.fail_with = None
        agent = self.agent(NOW + timedelta(hours=3))
        agent.run()
        self.engine.jobs["job1"] = {"status": "failed", "error": "download"}
        agent = self.agent(NOW + timedelta(hours=6))
        agent.run()
        self.assertEqual(agent.state["sources"]["twitch:v1"]["status"], "failed")
        # une source en échec n'est pas renvoyée en boucle
        self.assertNotIn("v1", [c.video_id for c in self.engine.submitted[1:]])

    def test_best_clips_are_kept_and_named_by_score(self):
        self.cfg["clipping"].update(keep_best=2, min_score=50)
        self.agent().run()
        self.engine.jobs["job1"] = {"status": "completed", "result": {"clips": [
            {"title": f"Clip {s}", "predicted_score": s, "video_url": f"/v/{s}.mp4"}
            for s in (40, 95, 70, 80)]}}
        self.agent(NOW + timedelta(minutes=30)).run()
        names = sorted(c.name for c in self.storage.list_clips("review"))
        self.assertEqual(len(names), 2)
        self.assertIn("_note80_streamer_clip-80_", names[0])
        self.assertIn("_note95_streamer_clip-95_", names[1])
        # Les clips écartés au tri ne reviennent pas aux passages suivants.
        self.agent(NOW + timedelta(hours=1)).run()
        self.assertEqual(len(self.storage.list_clips("review")), 2)

    def test_min_score_never_drops_unscored_clips(self):
        self.cfg["clipping"]["min_score"] = 90
        self.agent().run()
        self.engine.finish("job1", n=2)   # clips sans note
        self.agent(NOW + timedelta(minutes=30)).run()
        self.assertEqual(len(self.storage.list_clips("review")), 2)

    def test_hook_and_context_in_caption(self):
        self.cfg["theme"]["caption_template"] = "{hook}\n\n{description}\n\n{credit}\n{hashtags}"
        source = {"platform": "twitch", "channel": "Chowh1", "channel_key": "chowh1"}
        clip = {"title": "L'arnaque absurde", "viral_hook_text": "Non mais c'est quoi ça ?",
                "video_description_for_instagram": "Un triple plongeon pour l'assurance. #gta5 #fail"}
        self.assertEqual(build_description(self.cfg, clip, source),
                         "L'arnaque absurde\n\nNon mais c'est quoi ça ?\n\n"
                         "Un triple plongeon pour l'assurance.\n\n"
                         "🎥 Crédit : Chowh1 (Twitch)\n#gta6 #gtavi #gaming #clips #chowh1\n")
        # Sans accroche ni contexte de l'IA, le titre les remplace.
        self.assertIn("\n\nSeul\n\nSeul\n\n", build_description(self.cfg, {"title": "Seul"}, source))

    def test_single_pass_waits_for_the_edit(self):
        self.cfg["clipping"]["max_jobs_in_flight"] = 1
        agent = self.agent()
        self.assertTrue(agent.needs_engine())
        self.assertEqual(self.engine.submitted, [])   # « plan » ne lance rien
        naps = []

        def nap(seconds):
            naps.append(seconds)
            self.engine.finish("job1", n=2)
        result = agent.run(wait_minutes=30, sleep=nap)
        self.assertEqual((result["découverte"], result["attente"], naps), (1, 2, [30]))
        self.assertEqual(len(self.storage.list_clips("review")), 2)
        self.assertEqual(agent.state["sources"]["twitch:v1"]["status"], "done")
        # Recherche faite il y a 30 min, rien en cours : le moteur n'est pas démarré.
        self.assertFalse(self.agent(NOW + timedelta(minutes=30)).needs_engine())

    def test_unfinished_edit_is_dropped_at_the_end_of_a_single_pass(self):
        self.cfg["clipping"]["max_jobs_in_flight"] = 1
        agent = self.agent()
        result = agent.run(wait_minutes=0.0001, sleep=lambda seconds: None)
        self.assertEqual(result["attente"], 0)
        source = agent.state["sources"]["twitch:v1"]
        self.assertEqual(source["status"], "failed")
        self.assertIn("non terminé", source["error"])
        # Un montage resté « en cours » (ancienne machine) fait démarrer le moteur pour être relevé.
        source["status"] = "submitted"
        agent.save()
        self.assertTrue(self.agent(NOW + timedelta(minutes=5)).needs_engine())

    def test_reset_starts_over_and_keeps_a_copy(self):
        self.agent().run()
        self.engine.finish("job1", n=1)
        self.agent(NOW + timedelta(minutes=30)).run()
        agent = self.agent(NOW + timedelta(hours=1))
        agent.state["instagram"] = {"seed": "s", "token": "t", "refreshed_at": NOW.isoformat()}
        backup = agent.reset()
        self.assertEqual(backup, "etat-agent-2026-11-20-1300.json")
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "_agent", backup)))
        fresh = self.agent(NOW + timedelta(hours=1))
        self.assertEqual((fresh.state["sources"], fresh.state["published"]), ({}, []))
        self.assertEqual(fresh.state["instagram"]["token"], "t")   # le jeton prolongé est gardé
        # Les clips déjà dans Drive ne sont pas touchés, et les montages peuvent repartir.
        self.assertEqual(len(self.storage.list_clips("review")), 1)
        self.assertTrue(fresh.needs_engine())

    def test_clip_is_public_only_while_instagram_fetches_it(self):
        events = []
        self.storage.share = lambda clip: events.append("share") or f"https://public/{clip.id}"
        self.storage.unshare = lambda clip: events.append("unshare")

        class DirectPublisher(FakePublisher):
            needs_public_url = True

            def publish(self, path, filename, title, caption, video_url=None):
                events.append(video_url)
                return super().publish(path, filename, title, caption)
        self.publisher = DirectPublisher()
        self.agent().run()
        self.engine.finish("job1", n=1)
        self.agent(NOW + timedelta(minutes=30)).run()
        clip = self.storage.list_clips("review")[0]
        self.storage.move(clip, "review", "approved")
        self.publisher.fail = True
        self.agent(NOW + timedelta(hours=1)).run()
        self.assertEqual(events, ["share", f"https://public/{clip.id}", "unshare"])   # retiré même en échec
        self.assertEqual(len(self.storage.list_clips("approved")), 1)
        self.publisher.fail = False
        self.agent(NOW + timedelta(hours=2)).run()
        self.assertEqual(len(self.storage.list_clips("published")), 1)
        self.assertEqual(events[3:], ["share", f"https://public/{clip.id}", "unshare"])


class FakeWriter:
    configured = True

    def __init__(self, fail=False):
        self.fail, self.seen = fail, []

    def rewrite(self, clip, source, transcript, game):
        self.seen.append((source["channel"], transcript, game))
        if self.fail:
            raise RuntimeError("réécriture refusée : encore à la 1re personne")
        return {"hook": f"{source['channel']} rate son braquage sur {game}", "context": "Il perd tout en dix secondes."}


class MeasuredPublisher(FakePublisher):
    """Publication par Buffer simulée : chaque clip a un post par réseau, avec des chiffres."""
    has_metrics = True

    def __init__(self):
        super().__init__()
        self.asked, self.numbers = [], {}

    def publish(self, path, filename, title, caption):
        super().publish(path, filename, title, caption)
        n = len(self.posts)
        return {"buffer_posts": {"instagram": f"ig{n}", "tiktok": f"tt{n}"}}

    def metrics(self, post_ids):
        self.asked.append(list(post_ids))
        return {pid: {"metrics": self.numbers.get(pid, {}), "updated_at": "2026-11-21T06:00:00Z"}
                for pid in post_ids}


class CaptionAndStatsTest(unittest.TestCase):
    def setUp(self):
        AgentTest.setUp(self)
        self.cfg["theme"]["caption_template"] = "{hook}\n\n{description}\n\n{credit}\n{hashtags}"

    def scored_job(self):
        self.engine.jobs["job1"] = {"status": "completed", "result": {"clips": [
            {"title": "J'ai tout perdu", "viral_hook_text": "J'ai ruiné ma partie 😭", "predicted_score": 88,
             "start": 12.0, "end": 44.5, "video_url": "/v/1.mp4"}]}}

    def agent(self, now=NOW, writer=None):
        made = AgentTest.agent(self, now)
        made.writer = writer
        return made

    def test_caption_is_rewritten_for_the_clip_account(self):
        self.engine.transcript = lambda job_id, clip, index: f"paroles de {job_id} clip {index}"
        self.agent().run()
        self.scored_job()
        fake = FakeWriter()
        agent = self.agent(NOW + timedelta(minutes=30), writer=fake)
        agent.run()
        clip = self.storage.list_clips("review")[0]
        self.assertIn("streamer rate son braquage sur GTA VI", clip.description)
        self.assertEqual(split_description(clip.description)[0], "streamer rate son braquage sur GTA VI")
        self.assertIn("Il perd tout en dix secondes.", clip.description)
        self.assertNotIn("J'ai ruiné", split_description(clip.description)[1])
        self.assertEqual(fake.seen, [("streamer", "paroles de job1 clip 0", "GTA VI")])
        # Ce que la mesure croisera avec les vues est gardé avec le clip.
        info = agent.state["clips"][clip.name]
        self.assertEqual((info["seconds"], info["score"], info["hook"]),
                         (32.5, 88.0, "streamer rate son braquage sur GTA VI"))

    def test_a_refused_rewrite_keeps_the_caption_of_the_edit(self):
        self.agent().run()
        self.scored_job()
        self.agent(NOW + timedelta(minutes=30), writer=FakeWriter(fail=True)).run()
        clip = self.storage.list_clips("review")[0]
        self.assertIn("J'ai ruiné ma partie", clip.description)

    def published_clip(self):
        """Un clip monté, validé et publié par Buffer ; renvoie son nom."""
        self.publisher = MeasuredPublisher()
        self.cfg["publishing"]["auto_approve"] = True
        self.agent().run()
        self.scored_job()
        self.agent(NOW + timedelta(minutes=30)).run()
        return self.storage.list_clips("published")[0].name

    def test_views_and_followers_are_read_per_clip(self):
        name = self.published_clip()
        published_at = NOW + timedelta(minutes=30)
        # Publié à l'instant : Buffer n'a encore aucun chiffre, rien n'est écrit.
        self.assertEqual(self.publisher.asked, [["ig1", "tt1"]])
        report_path = os.path.join(self.tmp.name, "_agent", stats.REPORT_NAME)
        self.assertFalse(os.path.exists(report_path))
        # Moins de 12 h plus tard : pas de nouvelle requête.
        self.agent(published_at + timedelta(hours=3)).run()
        self.assertEqual(len(self.publisher.asked), 1)
        # Le lendemain : les chiffres sont là, et servent de point à 24 h.
        self.publisher.numbers = {"ig1": {"views": 380, "follows": 2, "reactions": 14},
                                  "tt1": {"views": 760, "averageTimeWatched": 11.5}}
        agent = self.agent(published_at + timedelta(hours=25))
        agent.run()
        entry = agent.state["stats"][name]
        self.assertEqual((entry["channel"], entry["seconds"], entry["score"]), ("streamer", 32.5, 88.0))
        self.assertEqual(entry["posts"]["instagram"]["h24"], {"views": 380, "follows": 2, "reactions": 14})
        self.assertNotIn("h72", entry["posts"]["instagram"])
        with open(report_path, encoding="utf-8") as fh:
            rows = fh.read().splitlines()
        self.assertEqual(len(rows), 2)
        self.assertIn(";youtube vues;", rows[0])
        self.assertTrue(rows[0].startswith("clip;publié le;streamer;durée (s);note;accroche;instagram vues;"))
        cells = dict(zip(rows[0].split(";"), rows[1].split(";")))
        self.assertEqual((cells["streamer"], cells["durée (s)"], cells["note"]), ("streamer", "32.5", "88"))
        self.assertEqual((cells["instagram vues"], cells["instagram abonnés gagnés"], cells["instagram vues à 24 h"]),
                         ("380", "2", "380"))
        self.assertEqual((cells["tiktok vues"], cells["tiktok durée moyenne vue (s)"], cells["tiktok abonnés gagnés"]),
                         ("760", "11.5", ""))   # un chiffre absent reste vide, pas zéro
        text = stats.report(agent.state)
        self.assertIn("Clips suivis : 1, dont 1 avec des chiffres", text)
        self.assertRegex(text, r"streamer\s+1\s+380\s+760\s+-\s+2")
        self.assertRegex(text, r"30 à 45 s\s+1\s+380")
        self.assertRegex(text, r"note 80\+\s+1\s+380")
        # Au bout de 7 jours, le clip n'est plus interrogé.
        self.agent(published_at + timedelta(days=8)).run()
        self.assertEqual(len(self.publisher.asked), 2)

    def test_clips_published_before_the_measure_and_a_reset_stay_tracked(self):
        self.publisher = MeasuredPublisher()
        agent = self.agent()
        agent.state["published"].append({"name": "ancien.mp4", "at": NOW.isoformat(),
                                         "buffer_posts": {"instagram": "ig_old"}})
        agent.run()
        self.assertIn(["ig_old"], self.publisher.asked)
        agent.reset()
        fresh = self.agent(NOW + timedelta(hours=13))
        self.assertEqual(fresh.state["published"], [])
        fresh.run()
        self.assertEqual(self.publisher.asked[-1], ["ig_old"])

    def test_no_measure_without_buffer(self):
        self.agent().run()   # FakePublisher : pas de chiffres disponibles
        self.assertEqual(stats.report(self.agent().state).splitlines()[0],
                         "Clips suivis : 0, dont 0 avec des chiffres (dernier relevé : jamais)")


class WriterTest(unittest.TestCase):
    SOURCE = {"channel": "Anyme023", "platform": "twitch"}

    def writer(self, answer):
        session = FakeSession([("generativelanguage", {"candidates": [{"content": {"parts": [{"text": answer}]}}]})])
        made = writer_mod.CaptionWriter({"GEMINI_API_KEY": "k"}, {"rewrite_captions": True}, session=session)
        return made, session

    def test_rewrite_names_the_streamer_and_drops_hashtags(self):
        made, session = self.writer('```json\n{"hook": "Anyme023 ruine sa partie de GTA VI 😭 #gta6", '
                                    '"context": "Il vend sa voiture par erreur."}\n```')
        clip = {"title": "J'ai tout perdu", "viral_hook_text": "J'ai ruiné ma partie"}
        self.assertEqual(made.rewrite(clip, self.SOURCE, "j'ai vendu la voiture", "GTA VI"),
                         {"hook": "Anyme023 ruine sa partie de GTA VI 😭", "context": "Il vend sa voiture par erreur."})
        url, kw = session.calls[0]
        self.assertIn("models/gemini-3.1-flash-lite:generateContent", url)
        self.assertEqual(kw["headers"]["x-goog-api-key"], "k")
        prompt = kw["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("Streamer : Anyme023", prompt)
        self.assertIn("j'ai vendu la voiture", prompt)
        made.rewrite(clip, self.SOURCE, "", "GTA VI")   # sans paroles : le titre et l'accroche seuls
        prompt = session.calls[1][1]["json"]["contents"][0]["parts"][0]["text"]
        self.assertNotIn("Paroles du clip", prompt)
        self.assertIn("Tu n'as pas les paroles", prompt)

    def test_unusable_rewrites_are_refused(self):
        for answer, reason in (
                ('{"hook": "J\'ai ruiné ma partie avec Anyme023", "context": "Il perd tout."}', "1re personne"),
                ('{"hook": "Anyme023 perd tout", "context": "Il ruine ma partie."}', "1re personne"),
                ('{"hook": "Le pire choix sur GTA VI", "context": "Il perd tout."}', "pas nommé"),
                ('{"hook": "Anyme023 perd tout", "context": ""}', "vide"),
                ('{"hook": "Anyme023 perd tout", "context": "Le contenu du clip n\'est pas disponible."}', "tâche"),
                ('pas du json', "illisible")):
            made, _ = self.writer(answer)
            with self.assertRaisesRegex(RuntimeError, reason):
                made.rewrite({"title": "T"}, self.SOURCE, "", "GTA VI")
        # « ma », « mes » ou « je » à l'intérieur d'un mot ne comptent pas.
        self.assertIsNone(writer_mod.check({"hook": "Anyme023 maîtrise GTA VI", "context": "Le jeu rame, il mesure les dégâts."},
                                           "Anyme023"))

    def test_writer_is_off_without_key_or_setting(self):
        self.assertFalse(writer_mod.CaptionWriter({"GEMINI_API_KEY": "k"}, {"rewrite_captions": False}).configured)
        self.assertFalse(writer_mod.CaptionWriter({}, {"rewrite_captions": True}).configured)
        self.assertEqual(writer_mod.CaptionWriter({"GEMINI_MODEL": "autre"}, {}).model, "autre")


class HelpersTest(unittest.TestCase):
    def test_pick_clips(self):
        def c(offset, views, duration=30):
            return {"url": f"u{offset}", "offset": offset, "duration": duration, "views": views}
        clips = [c(100, 10), c(5000, 500), c(5010, 900), c(9000, 600), c(200, 50, duration=60)]
        # 5000 chevauche 5010 (plus vu) : écarté. Ordre du stream. 60 s max.
        self.assertEqual([x["offset"] for x in pick_clips(clips, 60)], [5010, 9000])
        self.assertEqual([x["offset"] for x in pick_clips(clips, 600)], [100, 200, 5010, 9000])

    def test_small_helpers(self):
        self.assertEqual(parse_iso_duration("PT1H2M3S"), 3723)
        self.assertEqual(slug("Évasion ÉPIQUE à Vice City !"), "evasion-epique-a-vice-city")
        self.assertEqual(split_description("Titre\n\nLégende\n#tag\n"), ("Titre", "Légende\n#tag"))
        cfg = config.load(None, env={})
        text = build_description(cfg, {"title": "T", "video_description_for_tiktok": "desc #gta6"},
                                 {"channel": "Bob", "platform": "youtube"})
        self.assertEqual(text.splitlines()[0], "T")
        self.assertEqual(text.count("#gta6"), 1)


    def test_oauth_client_from_credentials_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "credentials.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"installed": {"client_id": "id-json", "client_secret": "secret-json"}}')
            cfg = config.load_oauth_client(config.load(None, env={}), path)
            self.assertEqual(cfg["env"]["GOOGLE_OAUTH_CLIENT_ID"], "id-json")
            self.assertEqual(cfg["env"]["GOOGLE_OAUTH_CLIENT_SECRET"], "secret-json")
            env = {"GOOGLE_OAUTH_CLIENT_ID": "id-env", "GOOGLE_OAUTH_CLIENT_SECRET": "secret-env"}
            cfg = config.load_oauth_client(config.load(None, env=env), path)
            self.assertEqual(cfg["env"]["GOOGLE_OAUTH_CLIENT_ID"], "id-env")
            cfg = config.load_oauth_client(config.load(None, env={}), os.path.join(tmp, "absent.json"))
            self.assertEqual(cfg["env"]["GOOGLE_OAUTH_CLIENT_ID"], "")


    def test_update_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# Drive\nGOOGLE_DRIVE_FOLDER_ID=      # commentaire\nAUTRE=1\n")
            config.update_dotenv({"GOOGLE_DRIVE_FOLDER_ID": "f1", "GOOGLE_OAUTH_REFRESH_TOKEN": "r1"}, path)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "# Drive\nGOOGLE_DRIVE_FOLDER_ID=f1\nAUTRE=1\n"
                                            "GOOGLE_OAUTH_REFRESH_TOKEN=r1\n")


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.text = payload, status, str(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _answer(self, url, **kw):
        self.calls.append((url, kw))
        for fragment, payload in self.routes:
            if fragment in url:
                return FakeResponse(payload(kw) if callable(payload) else payload)
        raise AssertionError(f"appel inattendu : {url}")

    def get(self, url, **kw):
        return self._answer(url, **kw)

    def post(self, url, **kw):
        return self._answer(url, **kw)

    def put(self, url, **kw):
        if "data" in kw and hasattr(kw["data"], "read"):
            kw["data"] = kw["data"].read()
        return self._answer(url, **kw)


class DiscoveryTest(unittest.TestCase):
    def cfg(self, **env):
        cfg = config.load(None, env=env)
        cfg["sources"]["only_allowed_channels"] = False
        return cfg

    def test_twitch_ranks_vods_by_viewer_clips(self):
        def clip(i, vod, offset, views, lang="fr"):
            return {"id": f"c{i}", "video_id": vod, "vod_offset": offset, "view_count": views,
                    "duration": 30, "language": lang, "broadcaster_name": f"S{vod}",
                    "title": f"clip {i}", "url": f"https://www.twitch.tv/s{vod}/clip/x", "game_id": "g6"}
        clips = [clip(1, "11", 4000, 900), clip(2, "11", 4300, 300), clip(3, "22", 50, 5000, "en"),
                 clip(4, "33", 600, 50), clip(5, "", None, 9999), clip(6, "44", 100, 700)]
        session = FakeSession([("oauth2/token", {"access_token": "t"}),
                               ("/games", {"data": [{"id": "g6", "name": "Grand Theft Auto VI"}]}),
                               ("/clips", {"data": clips, "pagination": {}})])
        cfg = self.cfg(TWITCH_CLIENT_ID="id", TWITCH_CLIENT_SECRET="s")
        cfg["sources"]["youtube"]["enabled"] = False
        cfg["sources"]["allowed_channels"]["twitch"] = []
        found = discovery.discover(cfg, session=session, now=NOW)
        self.assertEqual([c.video_id for c in found], ["11", "44"])  # en, <200 vues, sans VOD exclus
        self.assertEqual([c["offset"] for c in found[0].clips], [4000.0, 4300.0])
        self.assertIsNone(found[0].source_start)
        self.assertEqual(found[0].channel_key, "s11")
        self.assertEqual(found[0].url, "https://www.twitch.tv/videos/11")
        self.assertEqual(found[0].game, "Grand Theft Auto VI")

    def test_youtube_filters_and_scores(self):
        def video(vid, views, duration, hours_ago, live="none", lang="fr"):
            return {"id": vid, "snippet": {
                        "title": vid, "channelTitle": "Chaine", "channelId": "UC" + "x" * 22,
                        "liveBroadcastContent": live, "defaultAudioLanguage": lang,
                        "publishedAt": (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")},
                    "statistics": {"viewCount": str(views)}, "contentDetails": {"duration": duration}}
        videos = [video("a", 100000, "PT20M", 10), video("b", 900000, "PT30M", 30),
                  video("c", 500000, "PT2M", 5), video("d", 500000, "PT20M", 5, live="live"),
                  video("e", 1000, "PT20M", 5), video("f", 500000, "PT20M", 5, lang="en")]
        session = FakeSession([("/search", {"items": [{"id": {"videoId": v["id"]}} for v in videos]}),
                               ("/videos", {"items": videos})])
        cfg = self.cfg(YOUTUBE_API_KEY="k")
        found = discovery.discover(cfg, session=session, now=NOW)
        self.assertEqual([(c.video_id, c.score) for c in found], [("b", 30000.0), ("a", 10000.0)])

    def test_allowlist_matching(self):
        c = Candidate("youtube", "x", "u", "Ma Chaîne", "UCabc", "t", 1)
        self.assertTrue(discovery.is_allowed(c, {"youtube": ["@ma chaîne"]}))
        self.assertTrue(discovery.is_allowed(c, {"youtube": ["UCabc"]}))
        self.assertFalse(discovery.is_allowed(c, {"twitch": ["UCabc"], "youtube": []}))


class EngineTest(unittest.TestCase):
    def test_twitch_clips_are_compiled_and_uploaded(self):
        session = FakeSession([("/api/uploads/up1", {"ok": True}),
                               ("/api/uploads", {"upload_id": "up1"}),
                               ("/api/process", {"job_id": "j1"})])
        eng = OpenShortsEngine({"OPENSHORTS_API_URL": "https://os", "OPENSHORTS_API_KEY": "osk"},
                               config.DEFAULTS["clipping"], session=session)
        built = []

        def fake_build(clips, workdir, max_mb=45):
            built.append([c["url"] for c in clips])
            path = os.path.join(workdir, "compilation.mp4")
            with open(path, "wb") as fh:
                fh.write(b"video")
            return path

        original = engine_mod.build_compilation
        engine_mod.build_compilation = fake_build
        self.addCleanup(setattr, engine_mod, "build_compilation", original)
        c = cand(1)
        c.clips = [{"url": "https://clip/a", "offset": 1, "duration": 30, "views": 9}]
        self.assertEqual(eng.submit(c), "j1")
        self.assertEqual(built, [["https://clip/a"]])
        (_, create), (_, put), (_, process) = session.calls
        self.assertEqual(put["data"], b"video")
        self.assertEqual(process["json"]["upload_id"], "up1")
        self.assertNotIn("url", process["json"])
        self.assertNotIn("source_start", process["json"])

    def test_url_source_keeps_its_window(self):
        session = FakeSession([("/api/process", {"job_id": "j2"})])
        eng = OpenShortsEngine({"OPENSHORTS_API_URL": "https://os"}, config.DEFAULTS["clipping"],
                               session=session)
        self.assertEqual(eng.submit(cand(2)), "j2")
        body = session.calls[0][1]["json"]
        self.assertEqual((body["url"], body["source_start"], body["source_end"]),
                         ("https://example/2", 10, 1810))

    def test_hook_is_requested_only_when_enabled(self):
        session = FakeSession([("/api/process", {"job_id": "j3"}), ("/api/process", {"job_id": "j4"})])
        clipping = {**config.DEFAULTS["clipping"], "auto_hook": True, "hook_style": "yellow"}
        OpenShortsEngine({"OPENSHORTS_API_URL": "https://os"}, clipping, session=session).submit(cand(2))
        body = session.calls[0][1]["json"]
        self.assertEqual((body["auto_hook"], body["auto_hook_style"]), (True, "yellow"))
        OpenShortsEngine({"OPENSHORTS_API_URL": "https://os"}, config.DEFAULTS["clipping"],
                         session=session).submit(cand(3))
        self.assertNotIn("auto_hook", session.calls[1][1]["json"])


    def test_transcript_is_matched_to_the_clip_by_its_length(self):
        # Le clip 0 du moteur dure 50 s : ce ne sont pas les paroles du nôtre (30 s), le 1 oui.
        session = FakeSession([
            ("/api/clip/j1/0/transcript", {"durationSec": 50.0, "captions": [{"text": "autre"}]}),
            ("/api/clip/j1/1/transcript", {"durationSec": 30.2, "captions": [{"text": "bonne"}, {"text": " prise "}]})])
        eng = OpenShortsEngine({"OPENSHORTS_API_URL": "https://os"}, config.DEFAULTS["clipping"], session=session)
        self.assertEqual(eng.transcript("j1", {"start": 10, "end": 40}, 0, span=1), "bonne prise")
        self.assertEqual(eng.transcript("j1", {"start": 10, "end": 15}, 0, span=1), "")


class PublisherTest(unittest.TestCase):
    def test_payload(self):
        session = FakeSession([("upload-post.com", lambda kw: {"request_id": "r1"})])
        pub = UploadPostPublisher({"UPLOAD_POST_API_KEY": "k", "UPLOAD_POST_USER": "vey"},
                                  config.DEFAULTS["publishing"], session=session)
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            fh.write(b"x")
            fh.flush()
            self.assertEqual(pub.publish(fh.name, "a.mp4", "Titre", "Légende"), {"request_id": "r1"})
        _, kw = session.calls[0]
        self.assertEqual(kw["headers"], {"Authorization": "Apikey k"})
        self.assertEqual([v for k, v in kw["data"] if k == "platform[]"], ["tiktok", "instagram", "youtube"])
        self.assertIn(("post_mode", "MEDIA_UPLOAD"), kw["data"])
        self.assertIn(("media_type", "REELS"), kw["data"])

    def test_instagram_reel_through_the_official_api(self):
        states = [{"status_code": "IN_PROGRESS"}, {"status_code": "FINISHED"}]
        session = FakeSession([("/media_publish", {"id": "m1"}),
                               ("/42/media", {"id": "c1"}),
                               ("/c1", lambda kw: states.pop(0))])
        naps = []
        pub = InstagramPublisher({"INSTAGRAM_ACCESS_TOKEN": "tok", "INSTAGRAM_USER_ID": "42"},
                                 session=session, sleep=naps.append)
        self.assertEqual(pub.publish("https://public/clip.mp4", "Légende #gta6"), {"instagram_media_id": "m1"})
        (url, create), _, _, (_, publish) = session.calls
        self.assertTrue(url.endswith("/42/media"))
        self.assertEqual(create["data"], {"media_type": "REELS", "video_url": "https://public/clip.mp4",
                                          "caption": "Légende #gta6", "share_to_feed": "true"})
        self.assertEqual(create["headers"], {"Authorization": "Bearer tok"})
        self.assertEqual((publish["data"], naps), ({"creation_id": "c1"}, [10]))

    def test_instagram_rejected_video_is_an_error(self):
        session = FakeSession([("/42/media", {"id": "c1"}),
                               ("/c1", {"status_code": "ERROR", "status": "Error: 2207026 format"})])
        pub = InstagramPublisher({"INSTAGRAM_ACCESS_TOKEN": "tok", "INSTAGRAM_USER_ID": "42"}, session=session)
        with self.assertRaisesRegex(RuntimeError, "ERROR.*2207026"):
            pub.publish("https://public/clip.mp4", "x")
        self.assertEqual(len(session.calls), 2)   # rien n'est publié

    def test_instagram_token_is_extended_and_kept_in_state(self):
        env = {"INSTAGRAM_ACCESS_TOKEN": "tok1", "INSTAGRAM_USER_ID": "42"}
        session = FakeSession([("refresh_access_token", {"access_token": "tok2", "expires_in": 5184000})])
        state = {}
        InstagramPublisher(env, session=session).use_state(state, NOW)
        self.assertEqual((state["instagram"]["token"], session.calls), ("tok1", []))
        # Moins d'une semaine : on garde ; au-delà : prolongé, et c'est le nouveau qui sert.
        InstagramPublisher(env, session=session).use_state(state, NOW + timedelta(days=3))
        self.assertEqual(session.calls, [])
        pub = InstagramPublisher(env, session=session)
        pub.use_state(state, NOW + timedelta(days=8))
        self.assertEqual((pub.token, state["instagram"]["token"]), ("tok2", "tok2"))
        self.assertEqual(session.calls[0][1]["params"]["access_token"], "tok1")
        # Un nouveau jeton mis dans le secret reprend la main.
        pub = InstagramPublisher({**env, "INSTAGRAM_ACCESS_TOKEN": "tok9"}, session=session)
        pub.use_state(state, NOW + timedelta(days=9))
        self.assertEqual((pub.token, state["instagram"]["token"]), ("tok9", "tok9"))

    def test_routing_instagram_direct_and_the_rest_through_upload_post(self):
        publishing = {**config.DEFAULTS["publishing"], "platforms": ["instagram"]}
        self.assertFalse(Publisher({}, publishing).configured)
        direct = Publisher({"INSTAGRAM_ACCESS_TOKEN": "t", "INSTAGRAM_USER_ID": "42"}, publishing)
        self.assertEqual((direct.configured, direct.needs_public_url), (True, True))
        # Sans jeton Instagram mais avec Upload-Post : tout passe par Upload-Post, comme avant.
        legacy = Publisher({"UPLOAD_POST_API_KEY": "k", "UPLOAD_POST_USER": "vey"}, publishing)
        self.assertEqual((legacy.configured, legacy.needs_public_url), (True, False))
        # Instagram en direct + TikTok demandé sans Upload-Post : pas prêt.
        mixed = Publisher({"INSTAGRAM_ACCESS_TOKEN": "t", "INSTAGRAM_USER_ID": "42"},
                          {**publishing, "platforms": ["instagram", "tiktok"]})
        self.assertFalse(mixed.configured)

    def buffer(self, statuses, create=None):
        def answer(kw):
            query = kw["json"]["query"]
            if "organizations" in query:
                return {"data": {"account": {"organizations": [{"id": "org1", "name": "Compte"}]}}}
            if "channels(" in query:
                return {"data": {"channels": [{"id": "ch_ig", "name": "moncompte", "service": "instagram"},
                                              {"id": "ch_tt", "name": "moncompte", "service": "tiktok"}]}}
            if "createPost" in query:
                channel = kw["json"]["variables"]["input"]["channelId"]
                return (create or (lambda c: {"data": {"createPost": {
                    "__typename": "PostActionSuccess", "post": {"id": f"post_{c}", "status": "sending"}}}}))(channel)
            return {"data": {"post": statuses.pop(0)}}
        session = FakeSession([("litterbox", "unused"), ("api.buffer.com", answer)])
        session.post = lambda url, **kw: (hosted(url, kw) if "litterbox" in url else session._answer(url, **kw))

        def hosted(url, kw):
            session.calls.append((url, {"data": kw["data"], "file": kw["files"]["fileToUpload"][0],
                                       "browser": kw["headers"]["User-Agent"].startswith("Mozilla")}))
            response = FakeResponse({})
            response.text = "https://litter.catbox.moe/abc.mp4\n"
            return response
        return session

    def test_buffer_posts_a_reel_and_a_tiktok_from_a_temporary_link(self):
        session = self.buffer([{"status": "sending"}, {"status": "sent", "externalLink": "https://instagram.com/reel/x"},
                               {"status": "sent"}])
        naps, state = [], {}
        pub = BufferPublisher({"BUFFER_API_KEY": "key"}, session=session, sleep=naps.append)
        pub.use_state(state, NOW)
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            fh.write(b"x")
            fh.flush()
            result = pub.publish(fh.name, "clip.mp4", "Titre", "Légende #gta6", ["instagram", "tiktok"])
            self.assertEqual(result, {"buffer_posts": {"instagram": "post_ch_ig", "tiktok": "post_ch_tt"}})
            # Un second appel (passage suivant, reprise après incident) ne reposte rien.
            before = len(session.calls)
            pub.publish(fh.name, "clip.mp4", "Titre", "Légende #gta6", ["instagram", "tiktok"])
            self.assertEqual(len(session.calls), before)
        self.assertEqual(state["buffer_sent"], {"clip.mp4": result["buffer_posts"]})
        host = [kw for url, kw in session.calls if "litterbox" in url]
        self.assertEqual(host, [{"data": {"reqtype": "fileupload", "time": "12h"}, "file": "clip.mp4", "browser": True}])
        created = [kw["json"]["variables"]["input"] for _, kw in session.calls
                   if "api.buffer" in _ and "createPost" in kw["json"]["query"]]
        self.assertEqual(created[0], {"text": "Légende #gta6", "channelId": "ch_ig", "schedulingType": "automatic",
                                      "mode": "shareNow", "needsApproval": False,
                                      "assets": [{"video": {"url": "https://litter.catbox.moe/abc.mp4"}}],
                                      "metadata": {"instagram": {"type": "reel", "shouldShareToFeed": True}}})
        self.assertEqual((created[1]["channelId"], "metadata" in created[1]), ("ch_tt", False))
        self.assertEqual(naps, [30, 30, 30])

    def test_buffer_failure_on_one_network_does_not_repost_the_other(self):
        def create(channel):
            if channel == "ch_tt":
                return {"data": {"createPost": {"__typename": "InvalidInputError", "message": "vidéo trop courte"}}}
            return {"data": {"createPost": {"__typename": "PostActionSuccess",
                                            "post": {"id": "post_ig", "status": "sending"}}}}
        session = self.buffer([{"status": "sent"}], create)
        pub = BufferPublisher({"BUFFER_API_KEY": "key"}, session=session, sleep=lambda s: None)
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            with self.assertRaisesRegex(RuntimeError, "tiktok : Buffer : vidéo trop courte"):
                pub.publish(fh.name, "clip.mp4", "T", "L", ["instagram", "tiktok"])
            self.assertEqual(pub.sent, {"clip.mp4": {"instagram": "post_ig"}})
            with self.assertRaisesRegex(RuntimeError, "aucun canal connecté pour youtube"):
                pub.publish(fh.name, "autre.mp4", "T", "L", ["youtube"])

    def test_buffer_posts_a_public_gaming_short_with_its_title(self):
        session = self.buffer([{"status": "sent"}])
        inner = session.routes[1][1]
        session.routes[1] = ("api.buffer.com", lambda kw: (
            {"data": {"channels": [{"id": "ch_yt", "service": "youtube"}]}}
            if "channels(" in kw["json"]["query"] else inner(kw)))
        pub = BufferPublisher({"BUFFER_API_KEY": "key"}, session=session, sleep=lambda s: None)
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            pub.publish(fh.name, "clip.mp4", "T" * 120, "Légende", ["youtube"], video_url="https://drive/x")
        created = [kw["json"]["variables"]["input"] for u, kw in session.calls
                   if "createPost" in kw["json"]["query"]]
        self.assertEqual(created[0]["metadata"], {"youtube": {"title": "T" * 100, "categoryId": "20",
                                                              "privacy": "public", "madeForKids": False}})

    def test_a_network_at_its_daily_limit_does_not_block_the_clip(self):
        def create(channel):
            if channel == "ch_tt":
                return {"data": {"createPost": {"__typename": "LimitReachedError", "message":
                        "You have already scheduled the maximum number of posts YouTube allows in a day (2)."}}}
            return {"data": {"createPost": {"__typename": "PostActionSuccess",
                                            "post": {"id": "post_ig", "status": "sending"}}}}
        session = self.buffer([{"status": "sent"}], create)
        pub = BufferPublisher({"BUFFER_API_KEY": "key"}, session=session, sleep=lambda s: None)
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            result = pub.publish(fh.name, "clip.mp4", "T", "L", ["instagram", "tiktok"], video_url="https://d/x")
            self.assertEqual(result, {"buffer_posts": {"instagram": "post_ig"}})
            before = len(session.calls)   # le réseau plafonné n'est pas retenté pour ce clip
            self.assertEqual(pub.publish(fh.name, "clip.mp4", "T", "L", ["instagram", "tiktok"]), result)
            self.assertEqual(len(session.calls), before)
        state = {}
        stats.register(state, "clip.mp4", {"instagram": "post_ig", "tiktok": None}, NOW.isoformat())
        self.assertEqual(list(state["stats"]["clip.mp4"]["posts"]), ["instagram"])

    def test_buffer_metrics_are_read_in_one_request(self):
        def answer(kw):
            query = kw["json"]["query"]
            self.assertIn('p0: post(input: { id: "ig1" })', query)
            self.assertIn('p1: post(input: { id: "tt1" })', query)
            return {"data": {"p0": {"id": "ig1", "metricsUpdatedAt": "2026-11-21T06:00:00Z",
                                    "metrics": [{"type": "views", "value": 380}, {"type": "follows", "value": 2}]},
                             "p1": {"id": "tt1", "metrics": [], "metricsUpdatedAt": None}}}
        session = FakeSession([("api.buffer.com", answer)])
        pub = BufferPublisher({"BUFFER_API_KEY": "key"}, session=session)
        # Un identifiant qui n'en est pas un n'entre jamais dans la requête.
        result = pub.metrics(["ig1", "tt1", "ig1", 'x" } evil'])
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(result, {"ig1": {"metrics": {"views": 380.0, "follows": 2.0},
                                          "updated_at": "2026-11-21T06:00:00Z"},
                                  "tt1": {"metrics": {}, "updated_at": None}})

    def test_buffer_takes_over_every_platform_when_its_key_is_set(self):
        publishing = {**config.DEFAULTS["publishing"], "platforms": ["instagram", "tiktok"]}
        pub = Publisher({"BUFFER_API_KEY": "key", "INSTAGRAM_ACCESS_TOKEN": "t", "INSTAGRAM_USER_ID": "42"},
                        publishing)
        self.assertEqual((pub.configured, pub.needs_public_url), (True, True))

    def test_buffer_uses_the_drive_link_without_temporary_host(self):
        session = self.buffer([{"status": "sent"}])
        pub = BufferPublisher({"BUFFER_API_KEY": "key"}, session=session, sleep=lambda s: None)
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            pub.publish(fh.name, "clip.mp4", "T", "L", ["instagram"],
                        video_url="https://drive.usercontent.google.com/download?id=x")
        self.assertFalse([u for u, _ in session.calls if "litterbox" in u])
        created = [kw["json"]["variables"]["input"] for u, kw in session.calls
                   if "api.buffer" in u and "createPost" in kw["json"]["query"]]
        self.assertEqual(created[0]["assets"], [{"video": {"url": "https://drive.usercontent.google.com/download?id=x"}}])


class PublicLogsTest(unittest.TestCase):
    """Dépôt public : le journal des passages ne montre ni lien, ni secret, ni nom du compte."""

    def tearDown(self):
        redact.uninstall()

    def test_derived_tokens_are_declared_to_github(self):
        # Sur GitHub Actions, la valeur est aussi confiée au runner (commande add-mask, que
        # GitHub retire lui-même du journal) : il la masque dans ce qui ne passe pas par Python.
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), contextlib.redirect_stdout(out):
            redact.install({}, force=True)
            redact.mask("jeton-renouvele-456")
            print("jeton-renouvele-456")
            redact.uninstall()
        self.assertEqual(out.getvalue(), "::add-mask::jeton-renouvele-456\n***\n")

    def test_links_secrets_and_account_name_never_reach_the_log(self):
        out = io.StringIO()
        # Hors GitHub Actions : sur le runner, add-mask écrirait la valeur pour la confier à GitHub.
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}), contextlib.redirect_stdout(out):
            self.assertTrue(redact.install({"BUFFER_API_KEY": "cle-buffer-123", "ACCOUNT_HANDLE": "@MonCompte",
                                            "OPENSHORTS_API_URL": "http://127.0.0.1:8000", "YOUTUBE_API_KEY": ""},
                                           force=True))
            redact.mask("jeton-renouvele-456")
            print("publié https://www.instagram.com/reel/abc/ sur moncompte")
            print("Drive GET https://www.googleapis.com/drive/v3/files/1AbC?alt=media → 403")
            print("échec : Max retries exceeded with url: /refresh_access_token?grant_type=x&access_token=jeton-renouvele-456")
            print("Bearer cle-buffer-123 refusé ; montage=true ; 3 clip(s)")
            print("[publication] 2026-11-20_note85_streamer_le-titre-du-clip_8910-1.mp4 → tiktok")
            print("avant : " + redact.quiet("Une accroche publiée"))
            redact.uninstall()
        text = out.getvalue()
        for secret in ("instagram.com", "googleapis", "1AbC", "moncompte", "cle-buffer-123", "jeton-renouvele-456"):
            self.assertNotIn(secret, text.lower() if secret.islower() else text)
        self.assertEqual(text.count(redact.LINK_MASK), 2)
        self.assertNotIn("le-titre-du-clip", text)             # le titre d'un clip mène au compte
        self.assertRegex(text, r"\[publication\] clip-[0-9a-f]{8} → tiktok")
        self.assertNotIn("Une accroche", text)
        self.assertIn("montage=true ; 3 clip(s)", text)      # le reste du journal reste lisible

    def test_filter_is_off_on_a_private_machine(self):
        self.assertFalse(redact.public_logs({}))
        self.assertTrue(redact.public_logs({"GITHUB_ACTIONS": "true"}))
        self.assertTrue(redact.public_logs({"CLIPPER_PUBLIC_LOGS": "1"}))

    def test_account_name_comes_from_the_environment_not_the_repository(self):
        cfg = config.load(None, env={"ACCOUNT_HANDLE": "@moncompte"})
        cfg["theme"]["caption_template"] = "{hook}\n\n⭐ Abonne-toi à @{account}\n{credit}"
        source = {"platform": "twitch", "channel": "Chowh1", "channel_key": "chowh1"}
        named = build_description(cfg, {"title": "Il rate tout"}, source)
        self.assertIn("⭐ Abonne-toi à @moncompte\n", named)
        cfg["env"]["ACCOUNT_HANDLE"] = ""
        bare = build_description(cfg, {"title": "Il rate tout"}, source)
        self.assertNotIn("Abonne-toi", bare)                  # pas de « @ » orphelin dans la légende
        self.assertIn("🎥 Crédit : Chowh1 (Twitch)", bare)

    def test_repository_names_no_account(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config.yaml"), encoding="utf-8") as fh:
            template = config.yaml.safe_load(fh)["theme"]["caption_template"]
        self.assertNotRegex(template, r"@[A-Za-z0-9_.]")        # seul « @{account} » est admis
        self.assertFalse(os.path.exists(os.path.join(root, "branding")))


if __name__ == "__main__":
    unittest.main()
