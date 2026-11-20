# Vey Clipper

Agent de clipping : il trouve les vidéos Twitch / YouTube qui marchent sur un thème (GTA VI au lancement), en fait des shorts verticaux montés, les dépose dans Google Drive pour validation, puis publie ceux que vous validez sur TikTok, Instagram Reels et YouTube Shorts.

Vous ne faites qu'une chose : regarder les clips dans Drive et déplacer ceux qui vous plaisent dans « Validé ».

## Comment ça marche

```
Découverte ──► Montage (OpenShorts) ──► Drive « 1 - À valider »
 Twitch / YouTube                              │  vous déplacez le fichier
                                               ▼
        TikTok / Reels / Shorts ◄── Drive « 2 - Validé » ──► « 3 - Publié »
              (Upload-Post)
```

| Étape | Qui | Détail |
|---|---|---|
| Trouver | Agent | Twitch : les streams du jeu que les spectateurs ont le plus clippés ; l'agent télécharge les clips les plus vus et les recolle (8 min au plus). Les clips sont publics même quand la VOD est réservée aux abonnés, et OpenShorts ne lit pas les liens Twitch. YouTube : les vidéos récentes qui prennent le plus de vues à l'heure. |
| Monter | [OpenShorts](https://github.com/mutonby/openshorts) | Choix des moments, recadrage 9:16, sous-titres incrustés, titre et légende. |
| Déposer | Agent | Chaque clip arrive dans `1 - À valider`. Sa description Drive contient le titre (1re ligne) puis la légende, le crédit du créateur et les hashtags. |
| **Valider** | **Vous** | Déplacer le fichier dans `2 - Validé` (ou `4 - Refusé`). Pour changer le texte publié, modifiez la description du fichier avant de le déplacer. |
| Publier | Agent | Un clip validé par passage (toutes les heures, le plus frais d'abord), 5 par jour au plus, puis le fichier passe dans `3 - Publié`. Un montage fini pendant le passage est publié dans la foulée. |

**Objectif : 3 à 5 publications par jour, relues.** L'agent garde un jour de clips d'avance (3 clips entre « À valider », « Validé » et le montage en cours) et ne lance un nouveau montage que pour refaire ce stock.

**Peu de clips, mais travaillés.** Chaque montage rend 4 clips notés de 0 à 100 par l'IA ; seuls les 3 mieux notés arrivent dans « À valider » (`clipping.keep_best`, seuil possible avec `min_score`), avec leur note en tête du nom de fichier (`2026-11-20_note92_…`). Chaque clip porte une accroche incrustée à l'ouverture (`auto_hook`), une mise en page qui peut empiler le jeu et la facecam (`layouts`), et une légende faite de l'accroche et d'une phrase de contexte écrites pour ce clip. Comme rien ne sort sans votre validation, l'objectif tient si vous validez au moins 3 clips par jour : chaque passage écrit où en est l'objectif et ce qui le bloque (clips à valider, aucune chaîne autorisée, pas assez de sources).

Rien n'est jamais publié sans être passé par le dossier `2 - Validé`.

**Un seuil à la place de la relecture.** Depuis que les clips sortent sans validation, `clipping.min_score` (80) écarte ceux que le montage note en dessous : ils ne sont ni déposés ni publiés. Un montage qui ne rend aucun clip au-dessus du seuil est remplacé par le suivant, dans la limite de `max_sources_per_day`.

**Clips de 20 à 45 secondes.** Tant que le compte grossit, on vise la rétention. La monétisation TikTok demande des vidéos d'une minute au moins, mais d'abord 10 000 abonnés : repasser `clip_min_seconds` / `clip_max_seconds` à 61-90 une fois ce seuil atteint.

**Légende écrite pour le compte, pas pour le streamer.** Le montage écrit l'accroche à la première personne (« J'ai ruiné ma partie »), comme si le compte était le streamer. Avec `clipping.rewrite_captions`, l'agent demande à Gemini (même clé que le montage) une accroche et une phrase de contexte à la 3e personne, qui nomment le streamer et GTA VI et ne s'appuient que sur les paroles du clip. Une réponse à la 1re personne, sans le nom du streamer ou illisible est refusée : la légende du montage est alors gardée et la raison écrite dans le journal (`[légende] …`). L'accroche incrustée dans la vidéo reste celle du montage. La légende se termine par l'appel à s'abonner, puis le crédit.

**Mesure.** Deux fois par jour, l'agent lit dans Buffer les chiffres de chaque clip publié depuis moins de 7 jours : vues, réactions, commentaires, partages, enregistrements, abonnés gagnés (Instagram seulement) et durée moyenne de visionnage. Le relevé complet est le fichier `_agent/stats-clips.csv` du dossier Drive (un clip par ligne, avec son streamer, sa durée, sa note et son accroche ; les vues à 24 h et à 72 h sont gardées à part). `python -m clipper stats` affiche la synthèse : vues moyennes et abonnés gagnés par streamer, par durée et par note. Buffer ne relève les réseaux qu'une fois par jour : les premiers chiffres d'un clip arrivent dans les 24 h, et une case vide veut dire « pas encore de chiffre », pas zéro. Le suivi survit à une remise à zéro. Réglages : bloc `stats` de `config.yaml`.

**Instagram, TikTok et YouTube Shorts en automatique, par Buffer.** Le Short reprend le titre du clip (100 caractères au plus), en catégorie « Jeux vidéo », public. Avec le secret `BUFFER_API_KEY` (offre gratuite de Buffer : 3 canaux, clé créée sur publish.buffer.com/settings/api), chaque clip validé est publié tout de suite sur les comptes Instagram et TikTok connectés à Buffer. Buffer vient chercher la vidéo à une adresse publique : le clip est déposé sur un hébergeur temporaire (litterbox.catbox.moe), qui l'efface au bout de 12 heures. `python -m clipper buffer-info` (ou la case du lancement manuel) liste les canaux connectés sans rien publier. Limites de l'offre gratuite : 1 000 requêtes par canal et par mois.

**Instagram en direct (secours, sans Buffer).** Sans clé Buffer, les clips validés sont publiés en Reels par l'API officielle de Meta, gratuite et sans quota mensuel. Il faut un compte Instagram professionnel, une app Meta (developers.facebook.com → Instagram → « API setup with Instagram login » → Generate token) et deux secrets : `INSTAGRAM_ACCESS_TOKEN` et `INSTAGRAM_USER_ID`. Instagram vient chercher la vidéo à une adresse publique : le clip est ouvert « à toute personne disposant du lien » dans Drive le temps de l'envoi, puis le partage est retiré. Le jeton vit 60 jours ; l'agent le prolonge chaque semaine et le garde dans son état (`_agent/etat-agent.json`).

**Repartir de zéro.** `python -m clipper reset` (ou la case « Repartir de zéro » du lancement manuel sur GitHub) oublie montages et publications passés ; l'ancien état est gardé en copie dans `_agent`.

**Publication à la main.** TikTok ne publie qu'en privé par son API tant que l'app n'est pas auditée. Les plateformes listées dans `manual_platforms` (actuellement TikTok) sont postées à la main : chaque clip publié est aussi copié dans `5 - À poster (Insta + TikTok)`, légende comprise (dans la description du fichier) : on l'enregistre depuis l'app Drive, on le poste, puis on supprime la copie.

**Validation automatique (active depuis le 20/11).** `auto_approve: true` fait passer chaque clip monté directement dans `2 - Validé` : il est publié sans relecture. Repasser à `false` pour revenir à la validation à la main.

**Lives du moment d'abord.** Les vues d'un live comptent moitié moins toutes les `half_life_hours` (6 h) depuis son dernier clip, et un live clippé il y a moins de `fresh_hours` (3 h) est monté même si le stock est plein, dans la limite de `max_sources_per_day`.

## Dossiers Google Drive

Un seul dossier racine, « Vey Clipper » (son identifiant est `GOOGLE_DRIVE_FOLDER_ID` dans `.env`). L'agent crée lui-même les sous-dossiers manquants, d'après `storage.folders` (`clipper/config.py`) :

```
Vey Clipper/
├── 1 - À valider                    clips montés, en attente de relecture
├── 2 - Validé                       à publier (déplacés ici à la main, ou d'office avec auto_approve)
├── 3 - Publié                       publiés par l'agent
├── 4 - Refusé                       écartés à la relecture
├── 5 - À poster (Insta + TikTok)    copies à poster à la main (manual_platforms)
└── _agent                           état de l'agent (etat-agent.json), ne pas toucher
```

Renommer un dossier dans Drive sans changer `storage.folders` fait recréer l'ancien nom au passage suivant : changer les deux en même temps.

## Droits : à lire avant de lancer

Clipper la vidéo d'un créateur sans son accord est une contrefaçon : retraits, avertissements (trois « strikes » ferment une chaîne YouTube) et suppression des comptes. Créditer ne suffit pas. Les plateformes démonétisent aussi le contenu simplement repris.

L'agent est donc réglé par défaut sur `only_allowed_channels: true` : il ne clippe que les chaînes listées dans `config.yaml`, c'est-à-dire celles qui vous ont donné leur accord (beaucoup de streamers cherchent des clippers et l'annoncent). Les autres chaînes qui performent sont listées comme **pistes** (`python -m clipper status`) : ce sont celles à contacter. Passer le réglage à `false` est votre décision et votre responsabilité ; c'est aussi vous qu'engage l'attestation de droits envoyée à OpenShorts à chaque montage.

## Mise en place

Cinq comptes à créer, une seule fois. Comptez une heure.

1. **OpenShorts** (montage). Le plus simple : un compte sur [openshorts.app](https://www.openshorts.app), puis une clé d'API `osk_...` dans la page du compte. Alternative gratuite : l'auto-héberger avec Docker sur une machine allumée en permanence (voir son README), avec une clé Gemini.
2. **Twitch** : une application sur [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps) → identifiant et secret client.
3. **YouTube** : dans Google Cloud Console, activer « YouTube Data API v3 » et créer une clé d'API.
4. **Google Drive** : dans le même projet Google Cloud, activer « Google Drive API », créer un ID client OAuth de type « Application de bureau », télécharger son JSON et le placer à la racine sous le nom `credentials.json` (ignoré par git ; on peut aussi renseigner `GOOGLE_OAUTH_CLIENT_ID` et `GOOGLE_OAUTH_CLIENT_SECRET` dans `.env`, qui restent prioritaires), puis lancer `python -m clipper drive-auth`. La commande crée le dossier « Vey Clipper » et écrit elle-même les deux dernières valeurs dans `.env`. L'agent n'a accès qu'aux fichiers qu'il crée, pas au reste du Drive.
5. **Upload-Post** (publication) : un compte sur [upload-post.com](https://www.upload-post.com), y connecter les comptes TikTok, Instagram et YouTube, puis créer une clé d'API. Le nom du profil va dans `UPLOAD_POST_USER`.

Les comptes TikTok / Instagram / YouTube eux-mêmes se créent à la main, l'agent ne le fait pas.

```bash
git clone <ce dépôt> ~/vey-clipper && cd ~/vey-clipper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # + ffmpeg (brew install ffmpeg) pour recoller les clips Twitch
cp .env.example .env        # puis remplir
python -m clipper drive-auth
python -m clipper discover --dry-run   # voir ce que l'agent trouverait, sans rien lancer
python -m clipper run                  # un passage complet
python -m clipper status
python -m clipper stats                # vues et abonnés gagnés par streamer, durée et note
```

Ajoutez ensuite vos chaînes autorisées dans `config.yaml`.

## Lancement automatique

### Sur GitHub Actions, sans machine à laisser allumée (configuration cible)

Le fichier `.github/workflows/vey-clipper.yml` lance un passage toutes les heures, nuit comprise. La plupart durent une minute (état, clips validés à sortir). Quand un montage est à faire (`python -m clipper plan` répond `montage=true`), le passage installe OpenShorts dans le runner, le lance le temps du montage, attend sa fin (`python -m clipper run --wait 40`) et dépose les clips dans Drive. Aucun service payant : OpenShorts tourne avec votre clé Gemini, comme sur le Mac.

Mise en route, dans cet ordre :

1. Dans GitHub → Settings → Secrets and variables → Actions, créer les secrets avec les valeurs de `.env` : `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `GEMINI_API_KEY`, `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` (dans `credentials.json` s'ils ne sont pas dans `.env`), `GOOGLE_OAUTH_REFRESH_TOKEN`, `GOOGLE_DRIVE_FOLDER_ID`, `ACCOUNT_HANDLE` (nom du compte de clips, sans @). Facultatifs : `YOUTUBE_API_KEY`, `UPLOAD_POST_API_KEY`, `UPLOAD_POST_USER`.
2. Couper l'agent du Mac, pour que deux agents n'écrivent pas le même état : `launchctl bootout gui/$(id -u)/com.vey-events.clipper`.
3. Pousser la branche `main`, puis lancer un premier passage à la main : onglet Actions → Vey Clipper → Run workflow, case « Démarrer OpenShorts » cochée. Le premier montage est plus long (installation des dépendances, mises en cache ensuite).

**Dépôt public : ce qui ne doit pas s'y lire.** Le code et les journaux des passages sont visibles par tout le monde. Trois règles en découlent :

- Le compte de clips n'est nommé nulle part dans le dépôt. Son nom vient du secret `ACCOUNT_HANDLE` et remplace `{account}` dans `caption_template` ; sans ce secret, la ligne de la légende qui le cite est retirée. Photo de profil et bios du compte se rangent dans Drive, pas ici.
- Le journal ne montre ni lien, ni clé, ni nom du compte : `clipper/redact.py` remplace toute adresse par « [lien masqué] » (lien de partage de la vidéo, lien du post publié, adresse d'API) et masque les secrets ainsi que les jetons obtenus en cours de passage. Le lien d'un post se lit dans Buffer ou dans l'état de l'agent (`_agent/etat-agent.json`, privé).
- Rien de ce que le passage télécharge n'est pris « au dernier numéro » : les actions GitHub et OpenShorts sont figés par empreinte de commit, ffmpeg par une archive datée dont l'empreinte SHA-256 est vérifiée (`FFMPEG_TAG`, `FFMPEG_FILE`, `FFMPEG_SHA256` dans le workflow, à changer ensemble).

Tant que les secrets manquent, le passage est sauté. Un montage qui n'a pas fini dans le temps imparti (`WAIT_MINUTES` dans le workflow) est abandonné et noté en échec.

**Minutes GitHub.** Dépôt privé : 2 000 minutes par mois offertes, sur une machine à 2 cœurs. Les passages légers en consomment environ 270 ; chaque montage, de l'ordre de 20 à 30 (à mesurer sur les premiers passages). Avec un montage par jour, cela tient ; avec deux, c'est juste. Dépôt public : minutes illimitées et machine à 4 cœurs (les clés restent dans les secrets, jamais dans le code).

### Sur le Mac, avec OpenShorts installé en local (configuration d'origine)

OpenShorts tourne dans Docker (Colima) sur le Mac : pas de file d'attente, pas de filigrane, pas de limite de minutes. Il faut une clé Gemini dans `.env` (`GEMINI_API_KEY`) et `OPENSHORTS_API_URL=http://localhost:8000`.

```bash
brew install colima docker docker-compose ffmpeg
brew services start colima                      # Docker démarre à l'ouverture de session
git clone https://github.com/mutonby/openshorts.git ~/openshorts
cd ~/openshorts && docker compose up -d --build backend
sed "s/VOTRE_NOM/$(whoami)/g" launchd/com.vey-events.clipper.plist > ~/Library/LaunchAgents/com.vey-events.clipper.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vey-events.clipper.plist
```

L'agent passe alors toutes les 30 minutes ; son journal est dans `logs/agent.log`. Le Mac doit rester allumé et ne pas se mettre en veille (Réglages Système → Batterie / Économiseur d'énergie). Pour l'arrêter : `launchctl bootout gui/$(id -u)/com.vey-events.clipper`.

Sur un serveur, une ligne de cron fait la même chose : `*/30 * * * * cd /chemin/vers/vey-clipper && python -m clipper run`.

L'état de l'agent (vidéos déjà traitées, montages en cours, publications) est le fichier `_agent/etat-agent.json` du dossier Drive : l'agent peut donc tourner depuis n'importe quelle machine sans rien perdre.

## Réglages utiles (`config.yaml`)

- `theme` : nom, hashtags, ligne de crédit. Changer de jeu = changer `theme`, `twitch.game_names` et `youtube.queries`.
- `sources.languages`, `lookback_hours`, seuils de vues.
- `clipping` : vidéos sources par jour, minutes de clips envoyées au montage, nombre et durée des clips, tri par note (`keep_best`, `min_score`), accroche (`auto_hook`, `hook_style`), mise en page (`layouts`) et légende réécrite (`rewrite_captions`, `caption_model`).
- `stats` : relevé des chiffres (`enabled`, `refresh_hours`, `track_days`).
- `publishing` : plateformes, plafond par jour, `tiktok_post_mode` (`MEDIA_UPLOAD` = le clip arrive en brouillon dans l'app TikTok, à publier d'un geste ; `DIRECT_POST` = publication directe).

## Tests

```bash
python -m unittest discover -s tests
```

Les tests couvrent le cycle complet avec un moteur et un stockage simulés. Le lancement sur GitHub Actions (OpenShorts installé dans le runner) n'a pas encore tourné : le premier passage manuel sert d'essai.
