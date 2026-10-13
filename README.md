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
| `./j6.sh list` | Inventaire FTP, téléchargements locaux et restaurations avec leur identifiant. |
| `./j6.sh list --local-only` | Inventaire local sans connexion FTP. |
| `./j6.sh download` | Télécharger la dernière archive du dossier de production. |
| `./j6.sh download ID` | Télécharger l’archive FTP désignée par `list`, depuis la production ou le dossier de référence. Reprendre automatiquement un transfert interrompu. |
| `./j6.sh restore` | Restaurer le dernier téléchargement local complet, ou retrouver sa copie existante. |
| `./j6.sh restore ID` | Restaurer le téléchargement local désigné par `list`, ou reprendre sa restauration interrompue. |
| `./j6.sh status` | Lister les copies, leur étape de restauration et leurs conteneurs Docker. |
| `./j6.sh start ID`, `stop ID`, `restart ID` | Gérer les conteneurs de cette copie. |
| `./j6.sh status ID`, `verify ID` | Voir les conteneurs ou relancer les contrôles HTTP de cette copie. |
| `./j6.sh clean ID` | Après confirmation, supprimer cette copie et son volume MySQL ; conserver les archives. |
| `./j6.sh cleanall` | Après confirmation, supprimer toutes les copies, leurs volumes et les téléchargements locaux. |
| `./j6.sh diskusage [ID]` | Voir l’espace occupé par toutes les archives et copies, ou par un backup précis ; hors volumes et images Docker. |

`list` interroge `/backup/j6-latest/` et `/backup/j6-20261004/`. L’identifiant
vient de l’horodatage du nom Akeeba : `20261010` si une seule archive porte
cette date dans l’inventaire complet, ou `20261010-112009` si plusieurs archives
du même jour imposent de préciser l’heure. Le même identifiant s’utilise pour `download`, `restore`
et la gestion d’une copie. Une commande refuse un identifiant ambigu.

Un téléchargement interrompu reprend automatiquement au prochain `download`
de la même archive distante. Le téléchargeur attend
45 secondes pour vérifier que les tailles et dates distantes restent stables,
contrôle la continuité des parties, puis écrit `manifest.json` et `SHA256SUMS`.

Les archives résident sous `j6/backup/akeeba/` dans des dossiers internes datés
du téléchargement. Une restauration
vérifie à nouveau taille et SHA-256 de chaque partie, puis crée
`j6/runs/NOM-EMPREINTE/` avec ses propres fichiers, sa base MySQL, son projet
Compose, son port local et son `joomla.sh`. Une même archive n’est restaurée
qu’une fois ; ses copies et les archives restent conservées. Pour les commandes
qui ciblent un run, l’identifiant peut être omis lorsqu’il est le seul présent.

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
