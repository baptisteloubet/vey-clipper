"""Configuration : un fichier YAML pour les réglages, l'environnement pour les secrets."""
import copy
import json
import os

import yaml

DEFAULTS = {
    "theme": {
        "name": "GTA VI",
        "hashtags": ["#gta6", "#gtavi", "#gaming", "#clips"],
        "credit_line": "🎥 Crédit : {channel} ({platform})",
        # Légende identique pour tous les clips. Champs : {title} {credit} {channel}
        # {login} {hashtags} {account}. La 1re ligne de la description Drive reste le titre.
        # {account} = ACCOUNT_HANDLE (secret) ; sans lui, la ligne qui le cite est retirée.
        "caption_template": "{title}\n\n{credit}\n{hashtags}",
    },
    "sources": {
        # Par défaut l'agent ne clippe QUE les chaînes listées ici (celles dont
        # vous avez l'accord). Les autres sont seulement remontées comme pistes.
        "only_allowed_channels": True,
        "allowed_channels": {"twitch": [], "youtube": []},
        "languages": ["fr"],
        "lookback_hours": 48,
        "interval_hours": 2,             # délai minimal entre deux recherches (quota des API)
        "twitch": {
            "enabled": True,
            "game_names": ["Grand Theft Auto VI"],
            "min_clip_views": 200,       # vues cumulées des clips spectateurs d'une VOD
            # Un live dure des heures : on remonte la même VOD avec les clips apparus
            # depuis, au plus toutes les `resubmit_minutes`, s'ils font assez de vues.
            "resubmit_minutes": 60,
            "min_resubmit_seconds": 180,  # durée minimale des nouveaux clips recollés
            # Fraîcheur : les vues d'une VOD comptent moitié moins toutes les
            # `half_life_hours` depuis son dernier clip (0 = pas de bonus fraîcheur).
            "half_life_hours": 0,
            # Live dont un clip a moins de `fresh_hours` : monté même si le stock est plein
            # (dans la limite de `max_sources_per_day`). 0 = jamais.
            "fresh_hours": 0,
        },
        "youtube": {
            "enabled": True,
            "queries": ["GTA 6", "GTA VI"],
            "min_views": 20000,
            "min_minutes": 8,
            "max_minutes": 90,
        },
    },
    "clipping": {
        "max_sources_per_day": 4,
        "max_jobs_in_flight": 2,
        "max_minutes_per_source": 30,    # Twitch : durée max des clips recollés envoyés au montage
        "max_upload_mb": 45,             # taille max du fichier envoyé (OpenShorts cloud : ~50 Mo)
        "target_clips": 4,
        "clip_min_seconds": 20,
        "clip_max_seconds": 60,
        "layouts": ["auto"],
        # Tri des clips rendus par le montage, d'après la note que l'IA leur donne (0-100).
        "keep_best": 0,                  # ne garder que les N mieux notés par source (0 = tous)
        "min_score": 0,                  # écarter les clips notés en dessous (0 = aucun seuil)
        # Accroche incrustée en ouverture du clip, écrite par l'IA d'après ce qui est dit.
        "auto_hook": False,
        "hook_style": "pill",            # pill | classic | dark | yellow | red
        # Légende réécrite pour le compte de clips : 3e personne, streamer et jeu nommés,
        # contexte tiré des paroles du clip. Demande GEMINI_API_KEY ; sans réponse
        # exploitable, la légende du montage est gardée telle quelle.
        "rewrite_captions": False,
        "caption_model": "",             # vide = GEMINI_MODEL, sinon le modèle du montage
    },
    "stats": {
        # Relevé des vues, réactions et abonnés gagnés de chaque clip publié par Buffer.
        "enabled": True,
        "refresh_hours": 12,             # Buffer rafraîchit ses chiffres une fois par jour
        "track_days": 7,                 # un clip est suivi 7 jours après sa publication
    },
    "publishing": {
        "platforms": ["tiktok", "instagram", "youtube"],
        "min_per_day": 3,                # objectif : au moins 3 publications par jour
        "stock_days": 2,                 # jours de clips gardés d'avance pour le tenir
        "max_per_day": 6,
        "max_per_run": 1,
        "tiktok_post_mode": "MEDIA_UPLOAD",  # brouillon dans l'app ; DIRECT_POST = publication directe
        # Plateformes postées à la main : chaque clip publié est aussi copié dans
        # le dossier « manual » (avec sa légende) pour être posté depuis le téléphone.
        "manual_platforms": [],
        # true = chaque clip monté est validé d'office (« À valider » → « Validé ») :
        # plus aucune relecture humaine avant publication. À n'activer que ponctuellement.
        "auto_approve": False,
    },
    "storage": {
        "kind": "drive",                 # drive | local
        "local_dir": "data/drive",
        "folders": {
            "review": "1 - À valider",
            "approved": "2 - Validé",
            "published": "3 - Publié",
            "rejected": "4 - Refusé",
            "manual": "5 - À poster (Insta + TikTok)",
        },
    },
}

ENV_KEYS = [
    "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "YOUTUBE_API_KEY",
    "OPENSHORTS_API_URL", "OPENSHORTS_API_KEY", "GEMINI_API_KEY", "GEMINI_MODEL",
    "UPLOAD_POST_API_KEY", "UPLOAD_POST_USER",
    "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID", "BUFFER_API_KEY",
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REFRESH_TOKEN", "GOOGLE_DRIVE_FOLDER_ID",
    "ACCOUNT_HANDLE",   # nom du compte de clips, sans @ : jamais écrit dans le dépôt (public)
]


def _merge(base, extra):
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_dotenv(path=".env"):
    """Charge un .env simple (CLE=valeur) sans écraser l'environnement existant."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def update_dotenv(values, path=".env"):
    """Écrit CLE=valeur dans .env : remplace la ligne existante, sinon l'ajoute à la fin."""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    pending = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    lines += [f"{key}={value}" for key, value in pending.items()]
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(path + ".tmp", path)


def load_oauth_client(cfg, path="credentials.json"):
    """Complète l'ID et le secret client OAuth depuis le JSON téléchargé dans Google Cloud.

    Les valeurs déjà présentes dans l'environnement (.env, secrets GitHub) gardent la priorité.
    """
    env = cfg["env"]
    if (env["GOOGLE_OAUTH_CLIENT_ID"] and env["GOOGLE_OAUTH_CLIENT_SECRET"]) or not os.path.exists(path):
        return cfg
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    client = data.get("installed") or data.get("web") or {}
    env["GOOGLE_OAUTH_CLIENT_ID"] = env["GOOGLE_OAUTH_CLIENT_ID"] or client.get("client_id", "")
    env["GOOGLE_OAUTH_CLIENT_SECRET"] = env["GOOGLE_OAUTH_CLIENT_SECRET"] or client.get("client_secret", "")
    return cfg


def load(path=None, env=None):
    cfg = copy.deepcopy(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            _merge(cfg, yaml.safe_load(fh) or {})
    env = os.environ if env is None else env
    cfg["env"] = {key: (env.get(key) or "").strip() for key in ENV_KEYS}
    return cfg
