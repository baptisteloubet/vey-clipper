"""Stockage des clips et de l'état de l'agent.

Deux implémentations de la même interface : Google Drive (production) et un
dossier local (tests, essais). La validation se fait en déplaçant un fichier
d'un dossier à l'autre ; la légende publiée est la description du fichier
(1re ligne = titre, suite = légende), modifiable avant validation.
"""
import json
import os
import shutil
from dataclasses import dataclass

import requests

from . import redact

STATE_NAME = "etat-agent.json"
AGENT_FOLDER = "_agent"


@dataclass
class StoredClip:
    id: str
    name: str
    description: str = ""


def split_description(description):
    """(titre, légende) depuis la description d'un fichier."""
    lines = [l.rstrip() for l in (description or "").strip().splitlines()]
    if not lines:
        return "", ""
    return lines[0].strip(), "\n".join(lines[1:]).strip()


class LocalStorage:
    def __init__(self, root, folders):
        self.root = root
        self.folders = folders
        for name in list(folders.values()) + [AGENT_FOLDER]:
            os.makedirs(os.path.join(root, name), exist_ok=True)

    def _dir(self, key):
        return os.path.join(self.root, self.folders[key])

    def load_state(self):
        path = os.path.join(self.root, AGENT_FOLDER, STATE_NAME)
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def save_state(self, state):
        path = os.path.join(self.root, AGENT_FOLDER, STATE_NAME)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)

    def put_clip(self, local_path, name, description, folder="review"):
        shutil.copyfile(local_path, os.path.join(self._dir(folder), name))
        with open(os.path.join(self._dir(folder), name + ".txt"), "w", encoding="utf-8") as fh:
            fh.write(description)
        return name

    def list_clips(self, folder):
        out = []
        for name in sorted(os.listdir(self._dir(folder))):
            if name.endswith(".txt"):
                continue
            sidecar = os.path.join(self._dir(folder), name + ".txt")
            description = ""
            if os.path.exists(sidecar):
                with open(sidecar, encoding="utf-8") as fh:
                    description = fh.read()
            out.append(StoredClip(id=name, name=name, description=description))
        return out

    def fetch(self, clip, folder, dest_path):
        shutil.copyfile(os.path.join(self._dir(folder), clip.id), dest_path)

    def move(self, clip, src, dst):
        for name in (clip.id, clip.id + ".txt"):
            path = os.path.join(self._dir(src), name)
            if os.path.exists(path):
                shutil.move(path, os.path.join(self._dir(dst), name))

    def backup_state(self, label):
        path = os.path.join(self.root, AGENT_FOLDER, STATE_NAME)
        if not os.path.exists(path):
            return None
        name = f"etat-agent-{label}.json"
        shutil.copy(path, os.path.join(self.root, AGENT_FOLDER, name))
        return name

    def save_report(self, name, text):
        with open(os.path.join(self.root, AGENT_FOLDER, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def share(self, clip):
        raise RuntimeError("le stockage local n'a pas d'adresse publique : Instagram demande Google Drive")

    def unshare(self, clip):
        pass

    def copy(self, clip, src, dst):
        shutil.copyfile(os.path.join(self._dir(src), clip.id), os.path.join(self._dir(dst), clip.id))
        with open(os.path.join(self._dir(dst), clip.id + ".txt"), "w", encoding="utf-8") as fh:
            fh.write(clip.description)


class DriveStorage:
    """Google Drive via l'API REST, authentifié par un refresh token OAuth."""

    API = "https://www.googleapis.com/drive/v3"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    FOLDER_MIME = "application/vnd.google-apps.folder"

    def __init__(self, env, folders, session=None):
        self.env = env
        self.folders = folders
        self.http = session or requests.Session()
        self._token = None
        self._ids = {}
        self.root_id = env.get("GOOGLE_DRIVE_FOLDER_ID")
        if not self.root_id:
            raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID manquant (lancez : python -m clipper drive-auth)")

    # -- HTTP ---------------------------------------------------------------
    def _headers(self):
        if not self._token:
            resp = self.http.post(self.TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": self.env["GOOGLE_OAUTH_CLIENT_ID"],
                "client_secret": self.env["GOOGLE_OAUTH_CLIENT_SECRET"],
                "refresh_token": self.env["GOOGLE_OAUTH_REFRESH_TOKEN"],
            }, timeout=30)
            resp.raise_for_status()
            self._token = resp.json()["access_token"]
            redact.mask(self._token)
        return {"Authorization": f"Bearer {self._token}"}

    def _call(self, method, url, **kw):
        kw.setdefault("timeout", 120)
        params = dict(kw.pop("params", {}) or {})
        params.setdefault("supportsAllDrives", "true")
        extra = kw.pop("headers", {})
        resp = self.http.request(method, url, headers={**self._headers(), **extra}, params=params, **kw)
        if resp.status_code == 401:
            # Le jeton d'accès Google expire au bout d'une heure : on le renouvelle une fois.
            self._token = None
            resp = self.http.request(method, url, headers={**self._headers(), **extra},
                                     params=params, **kw)
        if resp.status_code >= 400:
            raise RuntimeError(f"Drive {method} {url} → {resp.status_code} : {resp.text[:300]}")
        return resp

    # -- dossiers -----------------------------------------------------------
    def _query(self, q, fields="files(id,name,description)"):
        files, token = [], None
        while True:
            params = {"q": q, "fields": f"nextPageToken,{fields}", "pageSize": 200,
                      "includeItemsFromAllDrives": "true", "orderBy": "createdTime"}
            if token:
                params["pageToken"] = token
            data = self._call("GET", f"{self.API}/files", params=params).json()
            files += data.get("files", [])
            token = data.get("nextPageToken")
            if not token:
                return files

    def _folder(self, name):
        if name not in self._ids:
            safe = name.replace("\\", "\\\\").replace("'", "\\'")
            found = self._query(f"'{self.root_id}' in parents and name = '{safe}' and "
                                f"mimeType = '{self.FOLDER_MIME}' and trashed = false")
            if found:
                self._ids[name] = found[0]["id"]
            else:
                created = self._call("POST", f"{self.API}/files", json={
                    "name": name, "mimeType": self.FOLDER_MIME, "parents": [self.root_id]}).json()
                self._ids[name] = created["id"]
        return self._ids[name]

    def ensure_folders(self):
        return {key: self._folder(name) for key, name in self.folders.items()}

    # -- état ---------------------------------------------------------------
    def _state_file(self):
        folder = self._folder(AGENT_FOLDER)
        found = self._query(f"'{folder}' in parents and name = '{STATE_NAME}' and trashed = false")
        return folder, (found[0]["id"] if found else None)

    def load_state(self):
        _, file_id = self._state_file()
        if not file_id:
            return {}
        return self._call("GET", f"{self.API}/files/{file_id}", params={"alt": "media"}).json()

    def save_state(self, state):
        folder, file_id = self._state_file()
        body = json.dumps(state, ensure_ascii=False, indent=1).encode("utf-8")
        if file_id:
            self._call("PATCH", f"{self.UPLOAD}/{file_id}", params={"uploadType": "media"},
                       data=body, headers={"Content-Type": "application/json"})
        else:
            self._upload(body, {"name": STATE_NAME, "parents": [folder]}, "application/json")

    # -- clips --------------------------------------------------------------
    def _upload(self, data, metadata, mime):
        start = self._call("POST", self.UPLOAD, params={"uploadType": "resumable"}, json=metadata,
                           headers={"X-Upload-Content-Type": mime})
        resp = self.http.put(start.headers["Location"], data=data,
                             headers={"Content-Type": mime}, timeout=900)
        if resp.status_code >= 400:
            raise RuntimeError(f"Drive upload → {resp.status_code} : {resp.text[:300]}")
        return resp.json()["id"]

    def put_clip(self, local_path, name, description, folder="review"):
        with open(local_path, "rb") as fh:
            return self._upload(fh, {"name": name, "description": description,
                                     "parents": [self._folder(self.folders[folder])]}, "video/mp4")

    def list_clips(self, folder):
        fid = self._folder(self.folders[folder])
        files = self._query(f"'{fid}' in parents and trashed = false and "
                            f"mimeType != '{self.FOLDER_MIME}'")
        return [StoredClip(f["id"], f["name"], f.get("description", "")) for f in files]

    def fetch(self, clip, folder, dest_path):
        resp = self._call("GET", f"{self.API}/files/{clip.id}", params={"alt": "media"},
                          stream=True, timeout=900)
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(1 << 20):
                fh.write(chunk)

    def move(self, clip, src, dst):
        self._call("PATCH", f"{self.API}/files/{clip.id}", json={}, params={
            "addParents": self._folder(self.folders[dst]),
            "removeParents": self._folder(self.folders[src])})

    def copy(self, clip, src, dst):
        # Copie côté serveur : rien n'est retéléchargé. La description (légende) suit.
        self._call("POST", f"{self.API}/files/{clip.id}/copy", json={
            "name": clip.name, "description": clip.description,
            "parents": [self._folder(self.folders[dst])]})

    def backup_state(self, label):
        """Garde une copie de l'état sous un autre nom, dans le même dossier. None s'il n'y en a pas."""
        folder, file_id = self._state_file()
        if not file_id:
            return None
        name = f"etat-agent-{label}.json"
        self._call("POST", f"{self.API}/files/{file_id}/copy", json={"name": name, "parents": [folder]})
        return name

    def save_report(self, name, text, mime="text/csv"):
        """Écrit (ou remplace) un fichier texte du dossier de l'agent : le relevé des stats."""
        folder = self._folder(AGENT_FOLDER)
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        found = self._query(f"'{folder}' in parents and name = '{safe}' and trashed = false")
        body = text.encode("utf-8")
        if found:
            self._call("PATCH", f"{self.UPLOAD}/{found[0]['id']}", params={"uploadType": "media"},
                       data=body, headers={"Content-Type": mime})
        else:
            self._upload(body, {"name": name, "parents": [folder]}, mime)

    # -- lien public temporaire --------------------------------------------
    # Instagram ne reçoit pas de fichier : il vient chercher la vidéo à une adresse
    # publique. Le clip est donc ouvert « à toute personne disposant du lien » le
    # temps de la publication, puis le partage est retiré.
    def share(self, clip):
        self._call("POST", f"{self.API}/files/{clip.id}/permissions",
                   json={"role": "reader", "type": "anyone"})
        return f"https://drive.usercontent.google.com/download?id={clip.id}&export=download&confirm=t"

    def unshare(self, clip):
        self._call("DELETE", f"{self.API}/files/{clip.id}/permissions/anyoneWithLink")


def make_storage(cfg):
    st = cfg["storage"]
    if st["kind"] == "local":
        return LocalStorage(st["local_dir"], st["folders"])
    return DriveStorage(cfg["env"], st["folders"])
