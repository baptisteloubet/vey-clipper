"""Ligne de commande : python -m clipper <commande> [--config config.yaml]"""
import argparse
import fcntl
import os
import sys

from . import config as config_mod
from . import redact
from . import stats
from .engine import OpenShortsEngine
from .pipeline import Agent
from .publisher import Publisher
from .storage import make_storage
from .writer import CaptionWriter


def build_agent(cfg):
    return Agent(cfg, make_storage(cfg), OpenShortsEngine(cfg["env"], cfg["clipping"]),
                 Publisher(cfg["env"], cfg["publishing"]),
                 writer=CaptionWriter(cfg["env"], cfg["clipping"]))


def trial(agent, limit=3):
    """Essai en vrai, sans rien publier ni enregistrer : chiffres Buffer et réécriture d'une légende."""
    from .storage import split_description
    ok = True
    stats.backfill(agent.state)
    posts = {post["post_id"]: (name, platform)
             for name, entry in (agent.state.get("stats") or {}).items()
             for platform, post in entry["posts"].items()}
    if getattr(agent.publisher, "has_metrics", False):
        print("== Branchement Buffer")
        try:
            print("   " + agent.publisher.buffer.info().replace("\n", "\n   "))
        except Exception as exc:
            ok = False
            print(f"   ÉCHEC : {exc}")
    print(f"== Chiffres Buffer : {len(posts)} post(s) connu(s)")
    if not getattr(agent.publisher, "has_metrics", False):
        print("   clé Buffer absente : rien à lire")
    elif posts:
        try:
            for post_id, reading in agent.publisher.metrics(list(posts)).items():
                name, platform = posts[post_id]
                numbers = ", ".join(f"{k}={v:g}" for k, v in sorted(reading["metrics"].items()))
                print(f"   {platform:<10} {name[:60]}")
                print(f"   {'':<10} {numbers or 'pas encore de chiffres'} (relevé Buffer : {reading['updated_at']})")
        except Exception as exc:
            ok = False
            print(f"   ÉCHEC : {exc}")
    print("== Légendes réécrites (rien n'est modifié)")
    if not agent.writer.configured:
        print("   réécriture coupée (rewrite_captions) ou clé Gemini absente")
    seen = 0
    for folder in ("review", "approved", "published"):
        for clip in agent.storage.list_clips(folder):
            if seen >= limit or not agent.writer.configured:
                break
            seen += 1
            title, caption = split_description(clip.description)
            hook = (caption.splitlines() or [""])[0]
            info = agent.state["clips"].get(clip.name) or {}
            source = {"channel": info.get("channel") or "le streamer"}
            print(f"   {clip.name[:70]}")
            print(f"     avant : {hook}")
            try:
                texts = agent.writer.rewrite({"title": title, "viral_hook_text": hook}, source, "",
                                             agent.cfg["theme"]["name"])
                print(f"     après : {texts['hook']}")
                print(f"             {texts['context']}")
            except Exception as exc:
                ok = False
                print(f"     ÉCHEC : {exc}")
    if agent.writer.configured and not seen:
        print("   aucun clip dans Drive pour essayer")
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="clipper", description="Agent de clipping")
    parser.add_argument("command", choices=["run", "plan", "collect", "publish", "discover", "status",
                                            "stats", "essai", "reset", "buffer-info", "drive-auth"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="discover : liste sans envoyer au montage")
    parser.add_argument("--wait", type=float, default=0, metavar="MINUTES",
                        help="run : attendre la fin des montages lancés (passage unique, GitHub Actions)")
    args = parser.parse_args(argv)

    config_mod.load_dotenv()
    cfg = config_mod.load_oauth_client(config_mod.load(args.config))
    redact.install(cfg["env"])   # journal public (GitHub Actions) : ni lien, ni secret, ni nom du compte

    if args.command == "drive-auth":
        from .drive_auth import run as drive_auth
        return drive_auth(cfg)

    if args.command == "buffer-info":
        # Lecture seule : canaux connectés et forme des posts attendue par Buffer.
        print(Publisher(cfg["env"], cfg["publishing"]).buffer.info())
        return 0

    if args.command == "plan":
        # Rien n'est lancé ni enregistré : dit seulement si ce passage a besoin du moteur.
        needed = "true" if build_agent(cfg).needs_engine() else "false"
        print(f"montage={needed}")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
                fh.write(f"montage={needed}\n")
        return 0

    if args.command not in ("status", "stats", "essai") and not args.dry_run:
        # Un seul passage à la fois (launchd + lancement à la main) : sinon le second
        # écrase l'état enregistré par le premier.
        lock = open(os.path.join(os.path.dirname(os.path.abspath(args.config)), ".clipper.lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Un autre passage est en cours : on saute celui-ci.")
            return 0

    agent = build_agent(cfg)
    if args.command == "essai":
        return trial(agent)   # lecture seule : l'état n'est pas enregistré
    if args.command == "reset":
        backup = agent.reset()
        print(f"État remis à zéro (ancien état gardé : {backup or 'aucun'})")
        return 0
    if args.command == "run":
        print(agent.run(wait_minutes=args.wait))
    elif args.command == "collect":
        print(f"{agent.collect()} clip(s) déposé(s)")
    elif args.command == "publish":
        print(f"{agent.publish()} clip(s) publié(s)")
    elif args.command == "discover":
        result = agent.discover_and_submit(dry_run=args.dry_run)
        if args.dry_run:
            for c in result:
                print(f"{c.score:>10.0f}  {c.platform:<8} {c.channel:<24} {c.url}  {c.title[:60]}")
            print(f"{len(result)} vidéo(s) éligible(s), {len(agent.state['leads'])} piste(s) hors liste")
            return 0
        print(f"{result} vidéo(s) envoyée(s) au montage")
    elif args.command == "status":
        print(agent.summary())
        return 0
    elif args.command == "stats":
        print(stats.report(agent.state))   # lecture seule : le relevé se fait pendant les passages
        return 0
    agent.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
