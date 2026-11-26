# Veille technologique

Site vitrine (`index.html`) mis à jour chaque jour par la tâche planifiée **veille**
(GitHub Actions, `.github/workflows/veille.yml`).

## Fonctionnement

1. Chaque jour à 04:17 UTC (avec un passage de rattrapage à 09:43 UTC), le workflow lit `technologies.csv`.
2. Pour chaque technologie, `scripts/veille.py` collecte :
   - la dernière version publiée (GitHub Releases ; kernel.org pour le noyau) ;
   - les billets récents des flux officiels (RSS/Atom) ;
   - les actualités des 30 derniers jours (Google News, FR + EN).
3. Il génère `index.html` et `data/veille.json` (instantané + historique pour repérer les nouveautés),
   puis les pousse sur `main`.

Lancement manuel : onglet **Actions** → **veille** → **Run workflow**.
Modifier `technologies.csv` sur `main` relance aussi la génération.

## Ajouter une technologie

Ajoutez une ligne `Nom;Infos` dans `technologies.csv`. Si la colonne « Informations complémentaires »
contient une URL GitHub, ses releases sont suivies automatiquement. Pour affiner (catégorie, dépôt,
flux officiels, requête de recherche, lien officiel), ajoutez une entrée dans `scripts/sources.json`.

## Sites de référence et menaces

Les flux d'[IT-Connect](https://www.it-connect.fr), [Korben](https://korben.info),
[Cyberattaque.org](https://cyberattaque.org), [Zataz](https://www.zataz.com) et du CERT-FR sont lus une fois par passage ; un article
qui cite une technologie (titre ou résumé) est ajouté à sa carte avec la mention « Référence ».
L'onglet **Menaces** regroupe les dernières cyberattaques et alertes (Cyberattaque.org, CERT-FR) et
les revendications de rançongiciels publiées par [ransomware.live](https://www.ransomware.live) et [RansomLook](https://www.ransomlook.io), fusionnées (groupes les
plus actifs sur 7 jours, victimes françaises). Liste modifiable dans `_references` de `sources.json`.

## Exploitation : CISA KEV et EPSS

- [CISA KEV](https://www.cisa.gov/known-exploited-vulnerabilities-catalog) : catalogue des failles
  exploitées dans la nature. Les CVE présentes sont marquées « Exploitée » ; chaque carte de l'onglet
  Failles CVE liste aussi les ajouts au KEV des 90 derniers jours qui concernent la technologie
  (avec mention « Rançongiciels » si la faille sert à des campagnes connues) ; l'onglet Menaces
  affiche les 15 derniers ajouts.
- [FIRST EPSS](https://www.first.org/epss/) : probabilité d'exploitation dans les 30 jours de chaque
  CVE affichée (en rouge au-delà de 10 %). Le tri « Nouveautés / gravité » en tient compte.

## Filtrage des actualités

Les articles Google News sont filtrés pour écarter publicités, promotions, comparatifs,
tutoriels et billets hors sujet : le nom de la technologie (ou un terme de `match`) doit figurer
dans le titre, et les filtres globaux de `_filtres` dans `scripts/sources.json`
(titres exclus, médias exclus, mots exclus de la recherche) s'appliquent. Les articles sont ensuite
classés par pertinence (source officielle, site de référence, sujet sécurité/version, fraîcheur) avec
au plus 2 articles par média. Ajustez-les librement.

**Pas de doublons.** Une même information reprise par plusieurs médias (ou en français et en
anglais) n'apparaît qu'une fois : l'article le mieux classé est gardé et la carte indique « +N sources »
(survol pour les voir). Les titres sont comparés sur leurs numéros (version, CVE) et leurs mots
signifiants. Sur l'ensemble du site, un article, une CVE ou une faille KEV n'est affiché que dans une
seule technologie (la plus précise d'après le titre), les releases, les pages des sites de référence
et les revendications de rançongiciels (RansomLook + ransomware.live) sont aussi dédoublonnées.

## En local

```bash
python3 scripts/veille.py   # Python 3.10+, aucune dépendance
```

## Version installée

La 3e colonne de `technologies.csv` (« Version installée ») est facultative. Renseignée, elle permet
d'afficher sur la carte « ✓ À jour » ou « Mise à jour → x.y.z » (comparaison avec la dernière release ;
pour le noyau, dans la même branche, ex. 6.1.x) et de signaler les CVE dont la plage vulnérable
contient ta version. Exemple : `Ollama;;0.12.3`.

Valeurs spéciales : `latest` (ou `lastest`, `dernière`) = tu suis toujours la dernière version publiée :
la carte l'affiche et les CVE sont comparées à cette version. Pour Debian/Ubuntu, un nom de code
(`Trixie`, `Noble`…) désigne la branche correspondante. Les doublons du CSV (« Pi Hole » / « Pi-Hole »)
sont fusionnés et une ligne saisie avec des virgules (`Steam,latest`) est corrigée à la lecture.

Pour les produits suivis sur [endoflife.date](https://endoflife.date) (clé `eol` de `sources.json` :
noyau Linux, Debian, Ubuntu, Alpine, Docker Engine, Proxmox VE, VirtualBox), la carte indique
aussi la branche de ta version et sa date de fin de support (⚠ si elle n'est plus supportée), et la
mise à jour est comparée à la dernière version de cette branche.

## Failles (CVE)

L'onglet **Failles CVE** liste, pour chaque technologie, les 5 CVE les plus récentes publiées sur
30 jours dans la base [NVD](https://nvd.nist.gov) : recherche de l'expression `cve` de `sources.json`
dans la description, puis vérification des identifiants CPE (motifs `cpe` de `sources.json`). Une CVE
déjà analysée qui vise un autre produit est écartée. Pour les CVE pas encore analysées par la NVD,
le script consulte [CVE.org](https://www.cve.org) (produits et versions déclarés par l'éditeur dès
la publication, jusqu'à 150 CVE par technologie) ; à défaut, elles restent « À confirmer ». Les CVE touchant ta version installée sont affichées en priorité. Sans clé, la NVD limite le débit (≈ 3 min de collecte). Facultatif : créer
un secret `NVD_API_KEY` (clé gratuite sur nvd.nist.gov) pour accélérer.
