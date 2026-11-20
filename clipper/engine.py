"""Client du moteur de montage OpenShorts (auto-hébergé ou cloud, même API).

OpenShorts fait le montage : choix des moments, recadrage 9:16, sous-titres
incrustés, titres et légendes. https://github.com/mutonby/openshorts
"""
import os
import shutil
import subprocess
import tempfile

import requests


class QuotaExceeded(Exception):
    pass


class EngineBusy(Exception):
    pass


class OpenShortsEngine:
    def __init__(self, env, clipping, session=None):
        self.base = (env.get("OPENSHORTS_API_URL") or "http://localhost:8000").rstrip("/")
        self.clipping = clipping
        self.http = session or requests.Session()
        self.headers = {}
        if env.get("OPENSHORTS_API_KEY"):
            self.headers["Authorization"] = f"Bearer {env['OPENSHORTS_API_KEY']}"
        if env.get("GEMINI_API_KEY"):
            self.headers["X-Gemini-Key"] = env["GEMINI_API_KEY"]

    def submit(self, candidate):
        """Envoie une vidéo au montage. Renvoie un job_id, ou None si la source est refusée.

        Twitch : OpenShorts ne lit pas les VOD Twitch (souvent réservées aux abonnés).
        On télécharge donc les clips retenus, on les recolle et on envoie le fichier.
        """
        c = self.clipping
        source = {"url": candidate.url}
        if candidate.clips:
            with tempfile.TemporaryDirectory() as tmp:
                print(f"[montage] {candidate.key} : {len(candidate.clips)} clip(s) Twitch à recoller", flush=True)
                path = build_compilation(candidate.clips, tmp, c.get("max_upload_mb") or 45)
                print(f"[montage] {candidate.key} : envoi de {os.path.getsize(path) / 1e6:.0f} Mo au montage",
                      flush=True)
                source = {"upload_id": self.upload(path)}
        elif candidate.source_start is not None and candidate.source_end is not None:
            source["source_start"] = int(candidate.source_start)
            source["source_end"] = int(candidate.source_end)
        body = {
            **source,
            # Attestation de droits exigée par OpenShorts : elle engage l'utilisateur,
            # qui choisit les chaînes autorisées dans la configuration.
            "acknowledged": True,
            "layouts": c["layouts"],
            "target_clips": c["target_clips"],
            "clip_min_seconds": c["clip_min_seconds"],
            "clip_max_seconds": c["clip_max_seconds"],
        }
        if c.get("auto_hook"):
            body["auto_hook"] = True
            body["auto_hook_style"] = c.get("hook_style") or "pill"
        resp = self.http.post(f"{self.base}/api/process", json=body, headers=self.headers, timeout=600)
        if resp.status_code == 402:
            raise QuotaExceeded(resp.text[:300])
        if resp.status_code == 429:
            raise EngineBusy(resp.text[:300])
        resp.raise_for_status()
        data = resp.json()
        if data.get("needs_confirmation"):
            return None  # qualité source trop basse : on ne force pas
        return data["job_id"]

    def upload(self, path):
        """Dépose un fichier sur OpenShorts et renvoie son upload_id."""
        resp = self.http.post(f"{self.base}/api/uploads", json={"filename": os.path.basename(path)},
                              headers=self.headers, timeout=60)
        resp.raise_for_status()
        upload_id = resp.json()["upload_id"]
        with open(path, "rb") as fh:
            resp = self.http.put(f"{self.base}/api/uploads/{upload_id}", data=fh,
                                 headers={**self.headers, "Content-Type": "video/mp4"}, timeout=900)
        resp.raise_for_status()
        return upload_id

    def status(self, job_id):
        resp = self.http.get(f"{self.base}/api/status/{job_id}", headers=self.headers, timeout=60)
        if resp.status_code == 404:
            return {"status": "failed", "error": "job introuvable sur le moteur"}
        resp.raise_for_status()
        return resp.json()

    def transcript(self, job_id, clip, index, span=4):
        """Paroles d'un clip (texte brut), "" si le moteur ne les donne pas.

        Le moteur range les paroles par numéro de clip dans son propre classement, qui
        peut différer du nôtre quand un clip n'a pas été rendu : on vérifie donc la
        durée, et on cherche chez les voisins si elle ne correspond pas.
        """
        try:
            wanted = float(clip["end"]) - float(clip["start"])
        except (KeyError, TypeError, ValueError):
            wanted = None
        for candidate in [index] + [i for i in range(index + span + 1) if i != index]:
            try:
                resp = self.http.get(f"{self.base}/api/clip/{job_id}/{candidate}/transcript",
                                     headers=self.headers, timeout=60)
                if resp.status_code >= 400:
                    continue
                data = resp.json()
            except Exception:
                continue
            if wanted is not None and abs(float(data.get("durationSec") or 0) - wanted) > 1.0:
                continue
            return " ".join(str(w.get("text") or "").strip() for w in data.get("captions") or []).strip()
        return ""

    def download(self, clip, dest_path):
        url = clip.get("download_url")
        headers = {}
        if not url:
            url = clip["video_url"]
            if url.startswith("/"):
                url = self.base + url
            headers = self.headers
        with self.http.get(url, headers=headers, stream=True, timeout=600) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as fh:
                for chunk in resp.iter_content(1 << 20):
                    fh.write(chunk)


def build_compilation(clips, workdir, max_mb=45):
    """Télécharge les clips Twitch (yt-dlp) et les recolle en un seul MP4 (ffmpeg)."""
    import yt_dlp  # import tardif : seulement utile pour Twitch

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg introuvable : il faut l'installer pour recoller les clips Twitch")
    # OpenShorts cloud refuse les envois au-delà d'environ 50 Mo (500 côté serveur) ;
    # auto-hébergé, la limite est 2 Go (clipping.max_upload_mb).
    # Les clips Twitch sont souvent en HEVC 1080p60 (~45 Mo/min) : on réencode en
    # H.264 1080p30 avec un débit calculé pour tenir sous max_mb.
    seconds = sum(float(c.get("duration") or 30) for c in clips) or 1
    video_kbps = min(4500, int(max_mb * 8 * 1000 / seconds) - 160)
    if video_kbps < 1500:
        raise RuntimeError(f"{seconds:.0f} s de clips : trop long pour {max_mb} Mo, "
                           "baissez clipping.max_minutes_per_source")

    # Chaque clip est réencodé SEUL au même format avant le recollage. Recoller des
    # clips de codecs différents (HEVC et H.264 mélangés) puis réencoder le tout
    # donnait des « Invalid data found » et des images perdues aux jointures.
    parts = []
    opts = {"format": "best[height<=1080]/best", "quiet": True, "no_warnings": True,
            "noprogress": True, "retries": 5, "fragment_retries": 5}
    for i, clip in enumerate(clips):
        raw = os.path.join(workdir, f"raw{i:02d}.mp4")
        part = os.path.join(workdir, f"part{i:02d}.mp4")
        try:
            with yt_dlp.YoutubeDL({**opts, "outtmpl": raw}) as ydl:
                ydl.download([clip["url"]])
            subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", raw,
                            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
                                   "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p",
                            "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{video_kbps}k",
                            "-maxrate", f"{video_kbps}k", "-bufsize", f"{2 * video_kbps}k",
                            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", part],
                           check=True)
        except Exception as exc:  # un clip illisible ne fait pas échouer toute la source
            print(f"[montage] clip Twitch ignoré ({clip['url']}) : {exc}", flush=True)
            continue
        finally:
            if os.path.exists(raw):
                os.remove(raw)
        parts.append(part)
        print(f"[montage] clip Twitch {i + 1}/{len(clips)} téléchargé et réencodé", flush=True)
    if not parts:
        raise RuntimeError("aucun clip Twitch n'a pu être téléchargé")

    listing = os.path.join(workdir, "parts.txt")
    with open(listing, "w") as fh:
        fh.writelines(f"file '{p}'\n" for p in parts)
    dest = os.path.join(workdir, "compilation.mp4")
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listing,
                    "-c", "copy", "-movflags", "+faststart", dest], check=True)
    size_mb = os.path.getsize(dest) / 1e6
    if size_mb > max_mb * 1.1:
        raise RuntimeError(f"compilation de {size_mb:.0f} Mo : trop lourde pour OpenShorts")
    return dest
