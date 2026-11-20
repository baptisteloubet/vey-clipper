"""Mesure : ce que chaque clip publié a rapporté en vues et en abonnés.

Buffer relève les chiffres des réseaux une fois par jour. L'agent les lit au
plus toutes les `stats.refresh_hours`, en une requête pour tous les clips
suivis, et les range par clip avec ce qui le caractérise (streamer, durée,
note du montage, accroche). Le relevé complet est écrit dans
`_agent/stats-clips.csv` ; `python -m clipper stats` en affiche la synthèse.

Les chiffres sont gardés tels que Buffer les donne. Un chiffre absent ne veut
pas dire zéro : il reste vide dans le relevé.
"""
import csv
import io
from datetime import datetime, timedelta

REPORT_NAME = "stats-clips.csv"
PLATFORMS = ("instagram", "tiktok", "youtube")
LABELS = {"instagram": "Insta", "tiktok": "TikTok", "youtube": "Shorts"}
# Colonnes du relevé : (titre, type de mesure chez Buffer).
COLUMNS = (("vues", "views"), ("réactions", "reactions"), ("commentaires", "comments"),
           ("partages", "shares"), ("enregistrements", "saves"), ("abonnés gagnés", "follows"),
           ("durée moyenne vue (s)", "averageTimeWatched"))
CHECKPOINTS = (("h24", 24), ("h72", 72))


def _age_hours(iso, now):
    return (now - datetime.fromisoformat(iso)).total_seconds() / 3600


def register(state, name, posts, published_at, features=None):
    """Ouvre le suivi d'un clip publié. `posts` : {réseau: id du post Buffer}."""
    posts = {p: i for p, i in (posts or {}).items() if i}
    if not posts:
        return
    entry = state.setdefault("stats", {}).setdefault(name, {"published_at": published_at, "posts": {}})
    for key, value in (features or {}).items():
        if value not in (None, ""):
            entry.setdefault(key, value)
    for platform, post_id in posts.items():
        if post_id:
            entry["posts"].setdefault(platform, {"post_id": post_id})


def backfill(state):
    """Clips publiés avant la mise en place de la mesure : suivis eux aussi."""
    for record in state.get("published") or []:
        if record.get("buffer_posts") and record["name"] not in state.get("stats", {}):
            info = (state.get("clips") or {}).get(record["name"]) or {}
            register(state, record["name"], record["buffer_posts"], record["at"],
                     {"channel": info.get("channel"), "seconds": info.get("seconds"),
                      "score": info.get("score"), "hook": info.get("hook")})


def collect(state, publisher, now, cfg, log=print):
    """Relève les chiffres des clips suivis. Renvoie le nombre de posts relevés."""
    settings = cfg.get("stats") or {}
    if not settings.get("enabled") or not getattr(publisher, "has_metrics", False):
        return 0
    backfill(state)
    last = state.get("stats_read_at")
    if last and _age_hours(last, now) < settings["refresh_hours"]:
        return 0
    tracked = {}
    for name, entry in (state.get("stats") or {}).items():
        if _age_hours(entry["published_at"], now) > settings["track_days"] * 24:
            continue
        for platform, post in entry["posts"].items():
            tracked[post["post_id"]] = (entry, post)
    if not tracked:
        return 0
    state["stats_read_at"] = now.isoformat()   # même en échec : pas de requête à chaque passage
    readings = publisher.metrics(list(tracked))
    read = 0
    for post_id, (entry, post) in tracked.items():
        reading = readings.get(post_id) or {}
        if not reading.get("metrics"):
            continue   # Buffer n'a pas encore relevé ce post
        post.update(metrics=reading["metrics"], updated_at=reading.get("updated_at"),
                    read_at=now.isoformat())
        age = _age_hours(entry["published_at"], now)
        for key, hours in CHECKPOINTS:
            if age >= hours and key not in post:
                post[key] = dict(reading["metrics"])   # première lecture après ce délai
        read += 1
    log(f"[mesure] {read} post(s) relevé(s) sur {len(tracked)} suivi(s)")
    return read


# ------------------------------------------------------------------ relevé
def _num(value):
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def to_csv(state):
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")   # « ; » : s'ouvre tel quel dans un tableur en français
    head = ["clip", "publié le", "streamer", "durée (s)", "note", "accroche"]
    for platform in PLATFORMS:
        head += [f"{platform} {title}" for title, _ in COLUMNS]
        head += [f"{platform} vues à 24 h", f"{platform} vues à 72 h"]
    writer.writerow(head)
    for name, entry in sorted((state.get("stats") or {}).items(), key=lambda kv: kv[1]["published_at"]):
        row = [name, entry["published_at"][:16].replace("T", " "), entry.get("channel", ""),
               _num(entry.get("seconds")), _num(entry.get("score")), entry.get("hook", "")]
        for platform in PLATFORMS:
            post = entry["posts"].get(platform) or {}
            metrics = post.get("metrics") or {}
            row += [_num(metrics.get(kind)) for _, kind in COLUMNS]
            row += [_num((post.get(key) or {}).get("views")) for key, _ in CHECKPOINTS]
        writer.writerow(row)
    return out.getvalue()


def _bucket_seconds(seconds):
    if seconds is None:
        return "durée inconnue"
    for limit, label in ((30, "moins de 30 s"), (45, "30 à 45 s"), (60, "46 à 60 s")):
        if seconds <= limit:
            return label
    return "plus de 60 s"


def _bucket_score(score):
    if score is None:
        return "sans note"
    for floor in (90, 80, 70):
        if score >= floor:
            return f"note {floor}+"
    return "note < 70"


def _group(entries, key):
    """{groupe: {clips, vues moyennes par réseau, abonnés gagnés}}, les clips sans chiffres à part."""
    groups = {}
    for entry in entries:
        group = groups.setdefault(key(entry), {"clips": 0, "views": {p: [] for p in PLATFORMS}, "follows": 0})
        group["clips"] += 1
        for platform in PLATFORMS:
            metrics = (entry["posts"].get(platform) or {}).get("metrics") or {}
            if "views" in metrics:
                group["views"][platform].append(metrics["views"])
            group["follows"] += metrics.get("follows", 0)
    return groups


def report(state):
    """Synthèse lisible : vues moyennes et abonnés gagnés par streamer, durée et note."""
    entries = list((state.get("stats") or {}).values())
    measured = [e for e in entries if any((p.get("metrics") or {}) for p in e["posts"].values())]
    lines = [f"Clips suivis : {len(entries)}, dont {len(measured)} avec des chiffres "
             f"(dernier relevé : {state.get('stats_read_at', 'jamais')[:16].replace('T', ' ')})"]
    if not measured:
        lines.append("Buffer relève les réseaux une fois par jour : les premiers chiffres "
                     "arrivent dans les 24 h qui suivent une publication.")
        return "\n".join(lines)
    for title, key in (("Par streamer", lambda e: e.get("channel") or "?"),
                       ("Par durée", lambda e: _bucket_seconds(e.get("seconds"))),
                       ("Par note du montage", lambda e: _bucket_score(e.get("score")))):
        lines.append(f"\n{title}")
        heads = "".join(f"{'vues ' + LABELS[p]:>13}" for p in PLATFORMS)
        lines.append(f"  {'':<18}{'clips':>6}{heads}{'abonnés':>9}")
        groups = _group(measured, key)
        for name, group in sorted(groups.items(), key=lambda kv: -sum(sum(v) for v in kv[1]["views"].values())):
            means = [f"{sum(v) / len(v):.0f}" if v else "-" for v in (group["views"][p] for p in PLATFORMS)]
            cells = "".join(f"{m:>13}" for m in means)
            lines.append(f"  {name[:18]:<18}{group['clips']:>6}{cells}{group['follows']:>9.0f}")
    lines.append("\nVues : moyenne par clip. Abonnés : gagnés depuis les réels Instagram "
                 "(Buffer ne donne ce chiffre ni pour TikTok ni pour YouTube).")
    return "\n".join(lines)
