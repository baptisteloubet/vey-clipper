"""Connexion à Google Drive, à faire une seule fois sur votre ordinateur.

Ouvre la page de consentement Google, récupère un refresh token, crée le
dossier « Vey Clipper » et ses sous-dossiers, puis écrit les deux valeurs
obtenues dans .env. Portée demandée : drive.file (l'agent ne voit que les
fichiers qu'il a créés, pas le reste de votre Drive).
"""
import http.server
import secrets
import urllib.parse
import webbrowser

import requests

from .config import update_dotenv
from .storage import AGENT_FOLDER, DriveStorage

PORT = 8765
SCOPE = "https://www.googleapis.com/auth/drive.file"


def run(cfg):
    env = cfg["env"]
    client_id, client_secret = env["GOOGLE_OAUTH_CLIENT_ID"], env["GOOGLE_OAUTH_CLIENT_SECRET"]
    if not (client_id and client_secret):
        print("Placez le credentials.json téléchargé dans Google Cloud à la racine du projet, "
              "ou renseignez GOOGLE_OAUTH_CLIENT_ID et GOOGLE_OAUTH_CLIENT_SECRET dans .env")
        return 1
    redirect = f"http://localhost:{PORT}"
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state})
    received = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] == state and "code" in query:
                received["code"] = query["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("Connexion réussie, vous pouvez fermer cet onglet.".encode())

        def log_message(self, *args):
            pass

    print(f"Ouvrez cette adresse si le navigateur ne s'ouvre pas :\n{url}\n")
    webbrowser.open(url)
    server = http.server.HTTPServer(("localhost", PORT), Handler)
    while "code" not in received:
        server.handle_request()
    server.server_close()

    resp = requests.post(DriveStorage.TOKEN_URL, data={
        "grant_type": "authorization_code", "code": received["code"], "client_id": client_id,
        "client_secret": client_secret, "redirect_uri": redirect}, timeout=30)
    resp.raise_for_status()
    refresh = resp.json()["refresh_token"]
    access = resp.json()["access_token"]

    folder_id = env["GOOGLE_DRIVE_FOLDER_ID"]
    if not folder_id:
        created = requests.post(f"{DriveStorage.API}/files", timeout=30,
                                headers={"Authorization": f"Bearer {access}"},
                                json={"name": "Vey Clipper", "mimeType": DriveStorage.FOLDER_MIME})
        created.raise_for_status()
        folder_id = created.json()["id"]
    drive = DriveStorage({**env, "GOOGLE_OAUTH_REFRESH_TOKEN": refresh,
                          "GOOGLE_DRIVE_FOLDER_ID": folder_id}, cfg["storage"]["folders"])
    drive.ensure_folders()
    drive._folder(AGENT_FOLDER)
    update_dotenv({"GOOGLE_OAUTH_REFRESH_TOKEN": refresh, "GOOGLE_DRIVE_FOLDER_ID": folder_id})
    print("GOOGLE_OAUTH_REFRESH_TOKEN et GOOGLE_DRIVE_FOLDER_ID enregistrés dans .env.")
    print("Pour le lancement automatique, copiez-les aussi depuis .env dans les secrets GitHub.")
    print(f"\nDossier : https://drive.google.com/drive/folders/{folder_id}")
    return 0
