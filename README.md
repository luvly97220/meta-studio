# meta-studio
Studio IA vidéo

- `index.html` : version web (20 versions, API Agnès)
- `studio/` : Méta-Studio complet (fal.ai)
  - 🎬 Histoire : script IA, scènes parlantes, dialogues à plusieurs voix, 22 langues, sous-titres incrustés
  - 🎵 Clip musical : plans calés sur le morceau, play-back chanté
  - 🖼️ Animer : photo → vidéo, chorégraphie multi-plans (combats), assemblage
  - ✍️ Texte → vidéo, 🗣️ Parlant, 🎨 Image, 📁 Créations
  - 👤 Personnages : fiches réutilisables (photos, voix, personnalité)
  - 📝 Légende + hashtags TikTok générés pour chaque vidéo finie

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
