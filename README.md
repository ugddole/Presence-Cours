# UGD Présences — appel des cours

## Déploiement Railway
1. Crée un dépôt GitHub avec ces fichiers, puis un projet Railway relié au dépôt.
2. Ajoute un **Volume** Railway monté sur `/data` (sinon la base est effacée à chaque déploiement).
3. Variables d'environnement :
   - `DATA_DIR=/data`
   - `ADMIN_PASSWORD=` ton mot de passe bureau
   - `SECRET_KEY=` une longue chaîne aléatoire
4. Railway lance l'appli avec le `Procfile` (gunicorn).

## Démarrage
1. `/bureau` → connexion → **Importer depuis Kalisport** : les gymnastes et les cours sont créés.
2. Complète chaque cours (jour, horaire, entraîneur) et donne son code à 4 chiffres à l'entraîneur.
3. L'entraîneur ouvre l'appli sur son téléphone, saisit le code une fois, puis fait l'appel.

À chaque nouvel import, les inscriptions des gymnastes suivent le fichier Kalisport ; l'historique des présences est conservé.
