"""Légende réécrite pour le compte de clips.

Le montage écrit l'accroche et la légende comme si le streamer parlait (« J'ai
ruiné ma partie »). Publiées par un compte de clips, elles doivent parler DU
streamer : 3e personne, streamer et jeu nommés, contexte tiré des paroles du
clip et de rien d'autre. Un appel Gemini par clip, avec la clé déjà utilisée
par le montage.
"""
import json
import re

import requests

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.1-flash-lite"   # celui qu'OpenShorts utilise pour choisir les moments

PROMPT = """Tu écris la légende d'un clip vidéo publié par un compte de clips. Ce compte n'est PAS le streamer.

Streamer : {channel}
Jeu : {game}
Titre proposé par le montage : {title}
Accroche proposée par le montage : {hook}
{words}

Rends UNIQUEMENT un objet JSON : {{"hook": "...", "context": "..."}}

Règles :
- En français.
- À la 3e personne : le streamer s'appelle {channel}. Jamais « je », « j' », « mon », « ma », « mes », « moi ».
- "hook" : une seule phrase de 12 mots au plus, qui nomme {channel} et {game}.
- "context" : une seule phrase qui dit ce qui se passe dans le clip. {basis}
- N'invente rien. Ne parle jamais du clip, des paroles ou de ce que tu ne sais pas : en cas de doute, reste général.
- Ne prête aucune citation au streamer : pas de guillemets.
- Pas de hashtag. Pas d'emoji dans "context".
"""

WITH_WORDS = ('Paroles du clip (transcription automatique, elle peut contenir des erreurs) :\n"""{transcript}"""',
              "Appuie-toi sur les paroles uniquement.")
WITHOUT_WORDS = ("", "Tu n'as pas les paroles : redis le titre et l'accroche autrement, sans rien y ajouter.")
# Une phrase qui commente la tâche (« le contenu du clip n'est pas disponible ») n'est pas une légende.
META = re.compile(r"disponible|transcription|paroles|contenu du clip|impossible de", re.IGNORECASE)
FIRST_PERSON = re.compile(r"(?<![\w’'-])(?:j['’]|(?:je|mon|ma|mes|moi)(?![\w-]))", re.IGNORECASE)


def _clean(text):
    text = re.sub(r"\s*#\w+", "", str(text or ""))
    return re.sub(r"\s+", " ", text).strip().strip('"«»“” ').strip()


def check(texts, channel):
    """Raison du refus d'une réécriture, None si elle est publiable."""
    hook, context = texts.get("hook", ""), texts.get("context", "")
    if not hook or not context:
        return "accroche ou contexte vide"
    if channel.lower() not in hook.lower():
        return "le streamer n'est pas nommé dans l'accroche"
    if FIRST_PERSON.search(hook) or FIRST_PERSON.search(context):
        return "encore à la 1re personne"
    if META.search(hook) or META.search(context):
        return "parle de la tâche, pas du clip"
    if len(hook.split()) > 16 or len(context) > 300:
        return "trop long"
    return None


class CaptionWriter:
    def __init__(self, env, clipping, session=None):
        self.key = env.get("GEMINI_API_KEY") or ""
        self.model = clipping.get("caption_model") or env.get("GEMINI_MODEL") or DEFAULT_MODEL
        self.enabled = bool(clipping.get("rewrite_captions"))
        self.http = session or requests.Session()

    @property
    def configured(self):
        return self.enabled and bool(self.key)

    def rewrite(self, clip, source, transcript, game):
        """{"hook", "context"} réécrits. Lève une erreur si la réponse n'est pas publiable."""
        channel = source.get("channel") or "le streamer"
        words, basis = WITH_WORDS if (transcript or "").strip() else WITHOUT_WORDS
        prompt = PROMPT.format(
            channel=channel, game=game, basis=basis,
            title=_clean(clip.get("video_title_for_youtube_short") or clip.get("title")),
            hook=_clean(clip.get("viral_hook_text")),
            words=words.format(transcript=(transcript or "").strip()[:2500]))
        resp = self.http.post(
            GEMINI_API.format(model=self.model), timeout=60,
            headers={"x-goog-api-key": self.key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}})
        if resp.status_code >= 400:
            raise RuntimeError(f"Gemini {resp.status_code} : {resp.text[:200]}")
        try:
            raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip())
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError(f"réponse Gemini illisible ({exc})")
        if not isinstance(data, dict):
            raise RuntimeError("réponse Gemini illisible (pas un objet)")
        texts = {"hook": _clean(data.get("hook")), "context": _clean(data.get("context"))}
        problem = check(texts, channel)
        if problem:
            raise RuntimeError(f"réécriture refusée : {problem}")
        return texts
