"""Journal public : rien de sensible ne doit s'y lire.

Le dépôt est public, donc les journaux des passages GitHub Actions aussi. GitHub
masque la valeur exacte des secrets, mais pas ce qui en dérive (jeton renouvelé,
jeton d'accès d'une heure) ni les adresses : lien de partage d'une vidéo, lien du
post publié, adresse d'API avec sa clé en paramètre.

`install()` filtre tout ce qui sort sur stdout et stderr (traces d'erreur comprises) :
les adresses deviennent « [lien masqué] », les secrets et le nom du compte « *** ».
Le filtre est posé sur GitHub Actions, ou partout avec CLIPPER_PUBLIC_LOGS=1 ; en
local le journal reste lisible (drive-auth affiche une adresse à ouvrir).
"""
import os
import re
import sys

URL = re.compile(r"https?://[^\s\"'<>\\)\]}]+", re.IGNORECASE)
# Paramètre sensible resté hors d'une adresse (corps de réponse, message d'erreur).
PARAM = re.compile(r"((?:access_token|refresh_token|client_secret|api_?key|token|key)\s*[=:]\s*[\"']?)"
                   r"[A-Za-z0-9._~+/|-]{8,}", re.IGNORECASE)
LINK_MASK = "[lien masqué]"
MASK = "***"
MIN_LENGTH = 4          # en dessous, masquer une valeur abîmerait tout le journal

_values = set()         # valeurs à masquer, telles quelles
_names = set()          # noms à masquer sans tenir compte de la casse (compte de clips)
_raw = {}               # flux d'origine, pour parler à GitHub sans passer par le filtre


def public_logs(env=None):
    env = os.environ if env is None else env
    return env.get("GITHUB_ACTIONS") == "true" or env.get("CLIPPER_PUBLIC_LOGS") == "1"


def mask(value):
    """Ajoute une valeur apprise en cours de passage (jeton renouvelé, jeton d'accès)."""
    value = str(value or "").strip()
    if len(value) < MIN_LENGTH or value in _values:
        return
    _values.add(value)
    stream = _raw.get("stdout")
    if stream is not None and os.environ.get("GITHUB_ACTIONS") == "true" and "\n" not in value:
        # GitHub masque alors aussi la valeur dans ce qui ne passe pas par Python (ffmpeg, curl).
        stream.write(f"::add-mask::{value}\n")
        stream.flush()


def mask_name(value):
    value = str(value or "").strip().lstrip("@")
    if len(value) >= MIN_LENGTH:
        _names.add(value.lower())


def scrub(text):
    """Le texte tel qu'il peut être lu par tout le monde."""
    text = URL.sub(LINK_MASK, text)
    text = PARAM.sub(lambda m: m.group(1) + MASK, text)
    for value in sorted(_values, key=len, reverse=True):
        text = text.replace(value, MASK)
    for name in sorted(_names, key=len, reverse=True):
        text = re.sub(re.escape(name), MASK, text, flags=re.IGNORECASE)
    return text


class _Filtered:
    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        return self._stream.write(scrub(text) if isinstance(text, str) else text)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install(env, force=None):
    """Pose le filtre et lui apprend les secrets de l'environnement. Sans effet en local."""
    for key, value in (env or {}).items():
        if key == "ACCOUNT_HANDLE":
            mask_name(value)
        elif key != "OPENSHORTS_API_URL" and len(str(value or "")) >= MIN_LENGTH:
            _values.add(str(value))
    active = public_logs() if force is None else force
    if active and "stdout" not in _raw:
        _raw["stdout"], _raw["stderr"] = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = _Filtered(sys.stdout), _Filtered(sys.stderr)
    return active


def uninstall():
    """Retire le filtre et oublie ce qu'il masquait (tests)."""
    if "stdout" in _raw:
        sys.stdout, sys.stderr = _raw.pop("stdout"), _raw.pop("stderr")
    _values.clear()
    _names.clear()
