# Steam Analyzer (Steam Surveillance)

Ce script surveille automatiquement l'inventaire Steam de ton compte, estime la valeur des items, et propose une analyse de marché (items interéssant a vendre ou pas).  
Il embarque aussi un mini serveur web pour visualiser les données en temps réel.

## Fonctionnalités
- Suivi des inventaires pour une liste de jeux configurés
- Estimation de la valeur totale (prix Steam Market)
- Détection des nouveaux items
- Analyse du marché (turnover + prix recommandé)
- Interface web simple (port 8181)
- Gestion des cookies Steam (login automatisé)

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
- Lancer le script avec python3 `steam_surveillance.py --server --monitor`
- au premier lancement il vous sera demandé les identifiants Steam pour pouvoir vous connecter au compte que vous voulez surveiller (le script est fait pour fonctionner via une validation Steam Guard sur un autre appareil)
- Apres la verification passer il cree un fichier cookies.txt et settings.json 
- Le serveur va commencer à lister les items du jeu que vous avez configurés et apparaîtront sur le serveur web une fois la liste terminée
- Le serveur est directement accessible via https://localhost:8181

## Comment ça marche
- Le script se connecte à Steam ( via Playwright), récupère les cookies et le SteamID64.
- Les cookies sont utilisés pour interroger l’inventaire via `curl`.
- Le Market est consulté via `requests` + Playwright (HTML listings).
- Les données sont stockées localement et affichées via un mini serveur web.

## Fichiers générés
- `cookies.txt` : session Steam (fichier extrememement sensible ne le partager jamais !!!)
- `settings.json` : SteamID64
- `inventory_state.json` : historique local


## Dépannage
Si la connexion bloque:
- Lance en mode UI (`STEAM_HEADLESS=0 steam_surveillance.py --server --monitor`)
- Relance `--login` si besoin (recréé les fichiers de connexion)

## Licence
Copyright (c) 2026 Arthur Lesage

Permission is granted to use and redistribute this software
for non-commercial purposes, without modification, free of charge.

You MUST notify the author if:
- you modify the source code
- you use this software or a derivative work for commercial purposes

Notification must be sent prior to usage and include:
- the nature of the modification or commercial use
- the name of the project or company

The author reserves the right to grant or deny permission
for modified or commercial usage.

THIS SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
