# Grimperoots Backup

Outils locaux pour rapatrier une archive Akeeba Joomla 6 depuis le FTP OVH et
la restaurer dans une instance Docker isolée. Le dépôt ne contient aucun accès
ni archive de sauvegarde. Garder le dépôt GitHub privé.

## Préparer les accès

Créer `.env.local` à partir de `.env.example` et renseigner les identifiants FTP.
Le fichier est ignoré par Git. Le serveur OVH utilise `/backup` comme dossier
Akeeba par défaut.

## Télécharger et restaurer

Depuis la racine de ce dépôt :

```sh
python3 download_latest_akeeba_j6.py
python3 restore_akeeba_local.py --latest
```

Le téléchargement attend que l’archive soit stable, vérifie toutes ses parties
et leurs SHA-256, puis les conserve sous `bak/akeeba-j6/`. Une interruption peut
être reprise avec `--resume bak/akeeba-j6/<dossier-partiel>`.

La restauration utilise Kickstart en local et crée un compose, une base MySQL,
un volume et un dossier propres à cette archive sous `snapshots-j6/runs/`. Les
courriels sont désactivés et le site porte `noindex`. Un dossier stable basé sur
le nom et l’empreinte SHA-256 de l’archive empêche toute restauration répétée.
Les anciennes tentatives de restauration de `../grimperoots-migration` sont
également reconnues. Les archives et anciennes instances n’y sont pas modifiées.

Le lanceur généré `snapshots-j6/runs/<archive>-<empreinte>/joomla.sh` accepte
`start`, `status`, `stop` et `restart`. Pour reprendre une installation
interrompue, utiliser `--resume-run` avec le chemin du run existant.

Les scripts n’installent rien sur OVH et ne suppriment aucun fichier FTP. Les
mots de passe restent dans `.env.local`; ne jamais les ajouter au dépôt.
