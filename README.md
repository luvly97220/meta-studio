# meta-studio
Studio IA vidéo

- `index.html` : version web (20 versions, API Agnès)
- `studio/` : Méta-Studio complet (fal.ai) — histoires d'une minute, photo → vidéo, texte → vidéo, vidéo parlante, images, historique

## Lancer le studio dans Termux

```
pkg install git python ffmpeg
git clone https://github.com/luvly97220/meta-studio
cd meta-studio/studio
python server.py
```
Puis ouvrir http://localhost:8080 et coller sa clé fal.ai dans ⚙️.

Mettre à jour : `cd meta-studio && git pull`

La clé fal.ai reste dans le navigateur : elle n'est jamais écrite dans ce dépôt.
