# Grimperoots Backup — Joomla 6

Télécharger les archives Akeeba du site Joomla 6 en production et restaurer
plusieurs copies locales indépendantes. Le script principal est `./j6.sh`.
Il ne crée pas de nouvelle sauvegarde sur OVH et ne modifie aucun fichier distant.

## Préparer la machine

Prérequis : Bash, Python 3 (bibliothèque standard), Docker et Docker Compose v2.
La première restauration construit une image PHP 8.3 et télécharge les images
Docker nécessaires.

Copier `.env.example` vers `.env.local` et renseigner `FTP_HOST`, `FTP_USER` et
`FTP_PASSWORD`. Le dossier FTP de production par défaut est
`/backup/j6-latest/`. `FTP_PLAIN=true` correspond à l’accès OVH actuel ; mettre
`false` si le serveur accepte FTP/TLS. Les secrets, archives et copies locales
sont ignorés par Git.

## Commandes

| Commande | Effet |
| --- | --- |
| `./j6.sh list` | Inventaire FTP, téléchargements locaux et restaurations. Affiche les identifiants utilisables ci-dessous. |
| `./j6.sh list --local-only` | Inventaire local sans connexion FTP. |
| `./j6.sh download` | Télécharger la dernière archive du dossier de production. |
| `./j6.sh download ftp:/backup/j6-latest/NOM.jpa` | Télécharger l’archive désignée par son identifiant FTP affiché par `list`. |
| `./j6.sh download NOM.jpa` | Télécharger cette archive précise depuis le dossier de production. |
| `./j6.sh download --reference` | Utiliser le dossier de référence `/backup/j6-20261004/`. |
| `./j6.sh restore` | Restaurer le dernier téléchargement local complet dans une nouvelle copie. |
| `./j6.sh restore IDENTIFIANT_LOCAL` | Restaurer le téléchargement local désigné par `list`. |
| `./j6.sh restore --resume-run RUN` | Reprendre une restauration interrompue. |
| `./j6.sh status` | Lister les copies, leur étape de restauration et leurs conteneurs Docker. |
| `./j6.sh start RUN`, `stop RUN`, `restart RUN` | Gérer les conteneurs de cette copie. |
| `./j6.sh status RUN`, `verify RUN` | Voir les conteneurs ou relancer les contrôles HTTP de cette copie. |
| `./j6.sh clean RUN` | Après confirmation, supprimer cette copie et son volume MySQL ; conserver les archives. |
| `./j6.sh cleanall` | Après confirmation, supprimer toutes les copies, leurs volumes et les téléchargements locaux. |
| `./j6.sh diskusage` | Voir l’espace occupé par les archives et les fichiers des copies, hors volumes et images Docker. |

`list` interroge `/backup/j6-latest/` et `/backup/j6-20261004/`. Un identifiant
`ftp:/chemin/dossier/nom.jpa` désigne une archive distante et peut être passé
directement à `download`. Un identifiant local est le nom du sous-dossier dans
`j6/backup/akeeba/` et peut être passé à `restore`. Une restauration possède un
nom de run affiché par `list` ou `status`, utilisable par les commandes de gestion.

`download --latest` reste accepté comme alias du dossier de production.
`download --remote-dir CHEMIN` permet de choisir un autre dossier FTP.
Un téléchargement interrompu reprend automatiquement au prochain `download`
de la même archive distante. `download --resume j6/backup/akeeba/IDENTIFIANT`
permet de désigner explicitement le transfert partiel. Le téléchargeur attend
45 secondes pour vérifier que les tailles et dates distantes restent stables,
contrôle la continuité des parties, puis écrit `manifest.json` et `SHA256SUMS`.

Les archives résident sous `j6/backup/akeeba/IDENTIFIANT/`. Une restauration
vérifie à nouveau taille et SHA-256 de chaque partie, puis crée
`j6/runs/NOM-EMPREINTE/` avec ses propres fichiers, sa base MySQL, son projet
Compose, son port local et son `joomla.sh`. Une même archive n’est restaurée
qu’une fois ; ses copies et les archives restent conservées. Pour les commandes
qui ciblent un run, son nom peut être omis lorsqu’il est le seul présent.

Kickstart extrait les fichiers et ANGIE/BRS importe la base. Le site restauré
désactive l’envoi de courriels, applique `noindex` et n’interprète pas le
`.htaccess` de production. Apache fournit les règles SEF locales. La fin de la
restauration vérifie l’accueil, des articles et leurs images, la navigation et
la page de connexion d’administration. Le rapport est dans
`j6/runs/RUN/verification.json` ; une erreur laisse le run pour diagnostic ou
reprise. Seul le proxy expose un port, sur `127.0.0.1`.

Les commandes `clean` et `cleanall` exigent un terminal et la saisie exacte du
mot de confirmation affiché. Elles ne touchent pas à OVH. Conserver ce dépôt
privé : les archives téléchargées contiennent les données du site.
