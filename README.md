# Steam Analyzer (Steam Surveillance)

Ce script surveille automatiquement l'inventaire Steam de ton compte, estime la valeur des items, et propose une analyse de marché (items interessant a vendre ou pas).  
Il embarque aussi un mini serveur web pour visualiser les données en temps réel.
Je suis ouvert à toutes propositions d'améliorations

## Fonctionnalités
- Suivi des inventaires pour une liste de jeux configurés
- Estimation de la valeur totale (prix Steam Market)
- Détection des nouveaux items
- Analyse du marché (turnover + prix recommandé)
- Interface web simple (port 8181)
- Console web intégrée (logs + bouton clear)
- Gestion des cookies Steam (login automatisé)
- Compatible Windows (requêtes inventaire via `requests`)

## Prérequis
- Python 3.9+
- Playwright (pour les pages Market et le login)

## Installation
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests playwright
playwright install
```

## Configuration rapide
Les jeux suivis sont définis en haut du fichier `steam_surveillance.py` :
- `GAMES` (appid + context_id)
- `CURRENCY`, `LANGUAGE`, `POLL_SECONDS`

## Utilisation
- Lancer le script avec `python3 steam_surveillance.py --server --monitor`
- Au premier lancement il vous sera demandé les identifiants Steam pour pouvoir vous connecter au compte que vous voulez surveiller (validation Steam Guard sur un autre appareil)
- Apres la verification, il cree `cookies.txt` et `settings.json`
- Le serveur commence a lister les items du jeu configures et apparaitront sur le serveur web une fois la liste terminee
- Le serveur est accessible via http://localhost:8181

### Option utile
- Forcer regeneration cookies Steam : `--login`

## Comment ça marche
- Le script se connecte à Steam (via Playwright), récupère les cookies et le SteamID64.
- Les cookies sont utilisés pour interroger l’inventaire via `requests`.
- Le Market est consulté via `requests` + Playwright (HTML listings).
- Les données sont stockées localement et affichées via un mini serveur web.

## Fichiers générés
- `cookies.txt` : session Steam (fichier extremement sensible, ne le partage jamais)
- `settings.json` : SteamID64
- `inventory_state.json` : historique local


## Dépannage
Si la connexion bloque:
- Lance en mode UI (`STEAM_HEADLESS=0 python3 steam_surveillance.py --server --monitor`)
- Relance `--login` si besoin (recréé les fichiers de connexion)

## Licence
For license notifications:
- open a GitHub issue titled "License notification"
- send an mail to arthurepk@icloud.com
