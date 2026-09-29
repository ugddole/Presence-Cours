# UGD Présences

Appel des cours relié au planning partagé (Google Sheet du planning 2026-2027).

- Les cours et créneaux sont lus dans le planning : rien à paramétrer ici.
- L'admin importe les listes d'adhérents, cours par cours ou en un seul fichier.
- Chaque entraîneur choisit son nom une fois, puis voit ses cours du jour.

## Variables Railway

| Variable | Rôle |
|---|---|
| `CODE_ENTRAINEUR` | Code commun à tous les entraîneurs |
| `CODE_ADMIN` | Code administrateur (import des listes, exports) |
| `SECRET_KEY` | Longue chaîne aléatoire |
| `DATABASE_URL` | Fournie automatiquement par le service PostgreSQL de Railway |
| `PLANNING_URL` | Facultatif : adresse du script Google du planning (déjà renseignée) |

Commande de démarrage : `gunicorn app:app` (voir `Procfile`).
