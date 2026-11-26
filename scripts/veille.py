#!/usr/bin/env python3
"""Veille technologique quotidienne.

Lit technologies.csv, collecte pour chaque technologie :
  - la dernière version publiée (GitHub Releases, ou kernel.org pour le noyau),
  - les billets récents des flux officiels (RSS/Atom),
  - les actualités récentes (Google News, en français puis en anglais),
puis génère index.html (site vitrine statique, données embarquées)
et data/veille.json (instantané + historique des éléments déjà vus).

Uniquement la bibliothèque standard Python : aucune dépendance à installer.
"""
from __future__ import annotations

import csv
import html
import json
import os
import re
import sys
from fnmatch import fnmatch
import unicodedata
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "technologies.csv"
SOURCES_PATH = ROOT / "scripts" / "sources.json"
TEMPLATE_PATH = ROOT / "scripts" / "template.html"
DATA_PATH = ROOT / "data" / "veille.json"
OUT_PATH = ROOT / "index.html"

NOW = datetime.now(timezone.utc)
NEWS_WINDOW_DAYS = 30      # actualités plus anciennes ignorées
MAX_NEWS = 6               # actualités affichées par technologie
MAX_RELEASES = 3
HISTORY_DAYS = 120         # durée de conservation des liens « déjà vus »
CVE_WINDOW_DAYS = 30       # CVE publiées sur les 30 derniers jours
MAX_CVES = 5               # CVE affichées par technologie
UA = "veille-bot/1.0 (+https://github.com/laaandryy/Veille)"
TIMEOUT = 20


# --------------------------------------------------------------------------- utils
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fetch(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        d = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        d = None
    if d is None:
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def iso(d: datetime | None) -> str | None:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def clean_text(s: str | None, limit: int = 220) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(re.sub(r"\s+", " ", s)).strip()
    return s if len(s) <= limit else s[: limit - 1].rsplit(" ", 1)[0] + "…"


# --------------------------------------------------------------------------- inputs
def read_technologies() -> list[dict]:
    raw = CSV_PATH.read_bytes().decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(raw.splitlines(), delimiter=";"))
    techs, by_key = [], {}
    for row in rows[1:]:
        row = [c.strip() for c in row]
        if not row or not row[0]:
            continue
        # Ligne saisie avec des virgules (« Steam,latest; ») : on la redécoupe.
        if "," in row[0] and not any(row[1:]):
            row = [c.strip() for c in row[0].split(",")]
        name = row[0]
        info = row[1] if len(row) > 1 else ""
        installed = row[2] if len(row) > 2 else ""
        if not installed and info and not info.startswith("http") and is_latest(info):
            info, installed = "", info  # « Nom;latest » : version mise dans la 2e colonne
        key = tech_key(name)
        if key in by_key:  # doublon (« Pi Hole » / « Pi-Hole ») : on fusionne
            t = by_key[key]
            t["info"] = t["info"] or info
            t["installed"] = t["installed"] or installed
            log(f"  doublon ignoré dans le CSV : {name} (déjà suivi sous « {t['name']} »)")
            continue
        by_key[key] = {"name": name, "info": info, "installed": installed}
        techs.append(by_key[key])
    return techs


LATEST_WORDS = {"latest", "lastest", "last", "derniere", "dernière", "derniere version", "dernière version",
                "current", "stable", "a jour", "à jour"}


def is_latest(s: str | None) -> bool:
    return (s or "").strip().lower() in LATEST_WORDS


def tech_key(name: str) -> str:
    """Clé de comparaison : « Pi-Hole », « Pi Hole » et « pihole » -> « pihole »."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", s)


def load_sources() -> tuple[dict, dict]:
    data = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    sources = {}
    for k, v in data.items():
        if k.startswith("_"):
            continue
        for alias in [k, *v.get("aliases", [])]:
            sources[tech_key(alias)] = v
    return sources, data.get("_filtres", {})


# --------------------------------------------------------------------------- filtres
def norm(s: str) -> str:
    """Minuscules, sans accents ni ponctuation, pour comparer des titres."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " " + re.sub(r"[^a-z0-9.+]+", " ", s).strip() + " "


class NewsFilter:
    """Écarte les articles hors sujet ou commerciaux (pubs, promos, comparatifs…)."""

    def __init__(self, cfg: dict):
        self.title_re = re.compile("|".join(cfg.get("titres_exclus", [])) or r"(?!x)x", re.I)
        self.sources = [norm(x) for x in cfg.get("sources_exclues", [])]
        self.dropped = 0

    def ok(self, item: dict, terms: list[str]) -> bool:
        title = item["title"]
        if self.title_re.search(title):
            return self._drop()
        src = norm(item.get("source") or "") + norm(urllib.parse.urlparse(item["url"]).netloc)
        if any(b in src for b in self.sources):
            return self._drop()
        # Hors flux officiels : la technologie doit être nommée dans le titre.
        if not item.get("official") and terms and not any(t in norm(title) for t in terms):
            return self._drop()
        return True

    def _drop(self) -> bool:
        self.dropped += 1
        return False


NEWS_FILTER: NewsFilter | None = None
NEWS_FILTER_CFG: dict = {}
REFERENCE_ITEMS: list[dict] = []   # articles des sites de référence (IT-Connect, Korben…)

# Mots qui rendent un article plus utile pour une veille IT (sécurité, versions).
SIGNAL_RE = re.compile(
    r"\b(cve-\d{4}|faille|vuln\w*|patch\w*|correctif|mise à jour|update[sd]?|release[sd]?|version|"
    r"sortie|disponible|available|security|sécurité|exploit\w*|zero[- ]day|0day|ransomware|rançongiciel|"
    r"fin de support|end of life|eol|lts|deprecat\w*|breach|fuite|alerte|advisory|bulletin)\b", re.I)


def fetch_references(refs: list[dict]) -> list[dict]:
    """Flux des sites de référence, récupérés une seule fois pour toutes les technologies."""
    out = []
    cutoff = NOW - timedelta(days=NEWS_WINDOW_DAYS)
    seen: set[str] = set()
    for ref in refs:
        got = 0
        for page in range(1, int(ref.get("pages", 1)) + 1):
            url = ref["url"] if page == 1 else f"{ref['url']}{'&' if '?' in ref['url'] else '?'}paged={page}"
            try:
                items = parse_feed(fetch(url), ref["name"])
            except Exception as e:  # noqa: BLE001
                log(f"  ! référence {ref['name']} ({url}): {e}")
                break
            for i in items:
                d = parse_date(i["date"])
                if (d and d < cutoff) or i["url"] in seen:
                    continue
                seen.add(i["url"])
                i.update(source=ref["name"], reference=True, ref_kind=ref.get("kind", "it"))
                out.append(i)
                got += 1
            if not items:
                break
        log(f"  référence {ref['name']} : {got} articles")
    return out


def score_news(n: dict) -> int:
    """Classement : officiel > référence > reste, bonus si l'article parle sécurité/version."""
    sc = 0
    if n.get("official"):
        sc += 4
    if n.get("reference"):
        sc += 3
    if SIGNAL_RE.search(n["title"]):
        sc += 2
    d = parse_date(n.get("date"))
    if d and NOW - d < timedelta(days=3):
        sc += 2
    elif d and NOW - d < timedelta(days=10):
        sc += 1
    return sc


# --------------------------------------------------------------------------- doublons
# Mots trop courants pour signer une information (FR/EN), comparés sur leurs 6 premières lettres.
_GENERIC = set("""
le la les un une des du de d l et ou en au aux a à sur pour par avec sans dans ce cet cette ces son sa ses
leur leurs qui que quoi dont est sont plus moins tres tout tous toute votre vos notre nos vous nous il elle
the an and or of to in on at for by with from into over your our its is are be has have can how what why
new now just more this that these those you we it will not all out up
nouvea nouvel nouvelle mise jour update updated releas sortie dispon availa versio securi linux
patch fix fixes corrig correc faille vulner flaw secu critiq critic alert warnin avis
""".split())
_NUM_RE = re.compile(r"\b(?:cve-\d{4}-\d+|\d+(?:\.\d+)+|\d{3,})\b", re.I)


def _story(n: dict, ignore: set[str]) -> dict:
    """Empreinte d'un titre : mots signifiants (racines) et numéros (versions, CVE)."""
    title = re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", n["title"])  # « Titre - Média »
    txt = norm(title)
    nums = {x.lower() for x in _NUM_RE.findall(txt) if not re.fullmatch(r"(19|20)\d\d", x)}  # sans les années
    words = {w[:6] for w in re.findall(r"[a-z][a-z0-9+]{2,}", txt)}
    words -= ignore
    return {"nums": nums, "all": words, "key": words - _GENERIC, "date": parse_date(n.get("date"))}


def same_story(a: dict, b: dict) -> bool:
    """Deux articles racontent-ils la même information ?"""
    gap = abs(a["date"] - b["date"]) if a["date"] and b["date"] else timedelta(days=3)
    cves_a = {x for x in a["nums"] if x.startswith("cve")}
    cves_b = {x for x in b["nums"] if x.startswith("cve")}
    if cves_a and cves_b:
        return bool(cves_a & cves_b)
    if a["nums"] & b["nums"]:
        return gap <= timedelta(days=7)
    if a["nums"] and b["nums"]:  # versions différentes : informations différentes
        return False
    common = len(a["key"] & b["key"])
    union = a["all"] | b["all"]
    jaccard = len(a["all"] & b["all"]) / len(union) if union else 0
    return (common >= 3 and gap <= timedelta(days=21)) or (common >= 2 and gap <= timedelta(days=14)) or (jaccard >= 0.4 and gap <= timedelta(days=7)) \
        or (common == 1 and gap <= timedelta(days=2) and len(next(iter(a["key"] & b["key"]))) >= 4)


def group_stories(items: list[dict], ignore_terms: list[str]) -> list[dict]:
    """Regroupe les articles d'une même info (plusieurs médias, FR/EN) : le mieux classé est gardé,
    les autres médias sont listés dans « also »."""
    ignore = {w[:6] for t in ignore_terms for w in norm(t).split()}
    ranked = sorted(items, key=lambda x: (score_news(x), x["date"] or ""), reverse=True)
    fps = [_story(n, ignore) for n in ranked]
    parent = list(range(len(ranked)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(ranked)):
        for j in range(i):
            if ranked[i].get("official") and ranked[j].get("official"):
                continue  # deux billets officiels sont deux informations
            if same_story(fps[i], fps[j]):
                a, b = root(i), root(j)
                parent[max(a, b)] = min(a, b)  # la tête reste l'article le mieux classé
    heads: dict[int, dict] = {}
    for i, n in enumerate(ranked):
        r = root(i)
        if r == i:
            heads[i] = n
        else:
            heads[r].setdefault("also", []).append({"source": n.get("source"), "url": n["url"]})
    return list(heads.values())


FR_RE = re.compile(r"(\.fr\b|\bfrance\b|\bfrench\b)", re.I)


def _ransomlook(cfg: dict) -> list[dict]:
    try:
        data = json.loads(fetch(cfg.get("api", "https://www.ransomlook.io/api/recent")))
    except Exception as e:  # noqa: BLE001
        log(f"  ! ransomlook: {e}")
        return []
    posts = []
    for p in data if isinstance(data, list) else []:
        if p.get("private"):
            continue
        d = parse_date((p.get("discovered") or "").replace(" ", "T").split(".")[0] + "Z")
        link = p.get("link") or ""
        if link.startswith("/"):
            link = "https://www.ransomlook.io" + link
        title = clean_text(p.get("post_title"), 120)
        posts.append({
            "title": title, "group": p.get("group_name"), "date": iso(d), "country": None, "sector": None,
            "url": link or f"https://www.ransomlook.io/group/{urllib.parse.quote(p.get('group_name') or '')}",
            "france": bool(FR_RE.search(title)), "source": "RansomLook",
        })
    log(f"  ransomlook : {len(posts)} revendications")
    return posts


def _ransomware_live(cfg: dict) -> list[dict]:
    try:
        data = json.loads(fetch(cfg.get("api", "https://api.ransomware.live/v2/recentvictims")))
    except Exception as e:  # noqa: BLE001
        log(f"  ! ransomware.live: {e}")
        return []
    posts = []
    for p in data if isinstance(data, list) else []:
        raw = (p.get("discovered") or p.get("attackdate") or "").replace(" ", "T")
        d = parse_date(re.sub(r"\.\d+", "", raw)) if raw else None
        title = clean_text(p.get("victim") or p.get("domain"), 120)
        country = (p.get("country") or "").upper() or None
        posts.append({
            "title": title, "group": p.get("group"), "date": iso(d), "country": country,
            "sector": p.get("activity") or None, "domain": p.get("domain"),
            "url": p.get("url") or "https://www.ransomware.live",
            "france": country == "FR" or bool(FR_RE.search(f"{title} {p.get('domain') or ''}")),
            "source": "ransomware.live",
        })
    log(f"  ransomware.live : {len(posts)} revendications")
    return posts


def fetch_ransomware(cfg: dict) -> dict:
    """Revendications récentes des groupes de rançongiciels (RansomLook + ransomware.live)."""
    if not cfg:
        return {}
    live = _ransomware_live(cfg.get("ransomware_live", {})) if cfg.get("ransomware_live", True) else []
    look = _ransomlook(cfg)
    # Fusion : ransomware.live d'abord (pays et secteur renseignés), doublons écartés.
    seen, posts = set(), []
    for p in live + look:
        group = (p["group"] or "").lower()
        name = re.sub(r"^(https?://)?(www\.)?", "", (p["title"] or "").lower())
        name = re.sub(r"\.(com|net|org|fr|de|it|es|co\.uk|uk|io|eu|us|ca|au|br|in)(/.*)?$", "", name)
        keys = {(re.sub(r"[^a-z0-9]", "", name)[:40], group)}
        for dom in (p.get("domain"), p["title"]):
            if dom and re.fullmatch(r"(https?://)?[\w.-]+\.[a-z]{2,}/?", dom.strip().lower()):
                keys.add((re.sub(r"^(https?://)?(www\.)?", "", dom.strip().lower()).rstrip("/"), group))
        if keys & seen:
            continue
        seen |= keys
        posts.append(p)
    if not posts:
        return {}
    posts.sort(key=lambda x: x["date"] or "", reverse=True)
    week = [p for p in posts if (d := parse_date(p["date"])) and NOW - d < timedelta(days=7)]
    groups: dict[str, int] = {}
    for p in week:
        g = (p["group"] or "?").lower()
        groups[g] = groups.get(g, 0) + 1
    top = sorted(groups.items(), key=lambda kv: -kv[1])[:8]
    france = [p for p in posts if p["france"]]
    sectors: dict[str, int] = {}
    for p in france:
        if p.get("sector"):
            sectors[p["sector"]] = sectors.get(p["sector"], 0) + 1
    log(f"  rançongiciels : {len(posts)} revendications fusionnées, {len(week)} sur 7 jours, {len(france)} en France")
    # Les API ne renvoient que les dernières revendications (~100 chacune) : si tout tient sur 7 jours,
    # le total réel de la semaine est supérieur (affiché « N+ »).
    capped = len(week) == len(posts) and len(posts) >= 100
    return {"posts": posts[: int(cfg.get("max", 30))], "week_total": len(week), "week_capped": capped,
            "sample": len(week), "groups": [{"name": g, "count": c} for g, c in top],
            "france": france[:12], "france_sectors": sorted(sectors.items(), key=lambda kv: -kv[1])[:5],
            "sources": sorted({p["source"] for p in posts})}


def github_repo_from(url: str) -> str | None:
    m = re.match(r"https?://github\.com/([^/\s]+)/([^/\s#?]+)", url or "")
    return f"{m.group(1)}/{m.group(2).removesuffix('.git')}" if m else None


# --------------------------------------------------------------------------- collectors
PRERELEASE_RE = re.compile(r"\b(nightly|beta|alpha|dev|preview|canary|rc\d*|snapshot|insider)\b|-rc\.?\d", re.I)


def gh_releases(repo: str) -> tuple[list[dict], dict | None]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    releases: list[dict] = []
    try:
        data = json.loads(fetch(f"https://api.github.com/repos/{repo}/releases?per_page=30", headers))
        for r in data:
            if r.get("draft") or r.get("prerelease") or PRERELEASE_RE.search(
                    f"{r.get('name') or ''} {r.get('tag_name') or ''}"):
                continue
            releases.append({
                "version": r.get("name") or r.get("tag_name"),
                "tag": r.get("tag_name"),
                "date": iso(parse_date(r.get("published_at"))),
                "url": r.get("html_url"),
            })
            if len(releases) >= MAX_RELEASES:
                break
        if not releases:  # dépôt sans releases : repli sur les tags
            tags = json.loads(fetch(f"https://api.github.com/repos/{repo}/tags?per_page=3", headers))
            releases = [{"version": t["name"], "tag": t["name"], "date": None,
                         "url": f"https://github.com/{repo}/releases/tag/{urllib.parse.quote(t['name'])}"}
                        for t in tags[:MAX_RELEASES]]
    except Exception as e:  # noqa: BLE001
        log(f"  ! releases {repo}: {e}")
    repo_meta = None
    try:
        meta = json.loads(fetch(f"https://api.github.com/repos/{repo}", headers))
        repo_meta = {"stars": meta.get("stargazers_count"), "pushed": meta.get("pushed_at"),
                     "url": meta.get("html_url"), "description": meta.get("description")}
    except Exception as e:  # noqa: BLE001
        log(f"  ! repo {repo}: {e}")
    return releases, repo_meta


def kernel_releases() -> list[dict]:
    try:
        data = json.loads(fetch("https://www.kernel.org/releases.json"))
    except Exception as e:  # noqa: BLE001
        log(f"  ! kernel.org: {e}")
        return []
    out = []
    for r in data.get("releases", []):
        if r.get("moniker") not in ("mainline", "stable", "longterm"):
            continue
        ts = (r.get("released") or {}).get("timestamp")
        out.append({
            "version": f"{r['version']} ({r['moniker']})",
            "tag": r["version"],
            "date": iso(datetime.fromtimestamp(ts, timezone.utc)) if ts else None,
            "url": r.get("changelog") or "https://www.kernel.org",
        })
    out.sort(key=lambda x: x["date"] or "", reverse=True)
    return out[:MAX_RELEASES]


def parse_feed(xml_bytes: bytes, source_label: str | None = None) -> list[dict]:
    items = []
    root = ET.fromstring(xml_bytes)
    ns_atom = "{http://www.w3.org/2005/Atom}"
    # RSS 2.0 / RDF
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        def get(*names, _it=it):
            # NB : un Element sans enfant est « falsy », d'où les tests explicites à None.
            for name in names:
                for c in _it:
                    if c.tag.split("}")[-1] == name:
                        return c
            return None
        title_el = get("title")
        title = clean_text(title_el.text if title_el is not None else "", 200)
        link = None
        link_el = get("link")
        if link_el is not None:
            link = link_el.get("href") or (link_el.text or "").strip()
        if tag == "entry":
            for l in it.findall(f"{ns_atom}link"):
                if l.get("rel", "alternate") == "alternate":
                    link = l.get("href")
                    break
        date_el = get("pubDate", "published", "updated", "date")
        desc_el = get("description", "summary", "content")
        src_el = get("source")
        source = (src_el.text.strip() if src_el is not None and src_el.text else None) or source_label
        if title and source and tag == "item" and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")]
        if not title or not link:
            continue
        items.append({
            "title": title,
            "url": link,
            "date": iso(parse_date(date_el.text if date_el is not None else None)),
            "source": source,
            "summary": "" if "news.google.com" in link else clean_text(desc_el.text if desc_el is not None else ""),
        })
    return items


def official_feed(url: str) -> list[dict]:
    try:
        host = urllib.parse.urlparse(url).netloc.removeprefix("www.")
        return [dict(i, official=True) for i in parse_feed(fetch(url), host)]
    except Exception as e:  # noqa: BLE001
        log(f"  ! flux {url}: {e}")
        return []


def google_news(query: str, lang: str, negative: str = "") -> list[dict]:
    params = {"fr": "hl=fr&gl=FR&ceid=FR:fr", "en": "hl=en-US&gl=US&ceid=US:en"}[lang]
    q = " ".join(x for x in (query, negative, "when:30d") if x)
    url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&{params}"
    try:
        return [dict(i, lang=lang) for i in parse_feed(fetch(url))]
    except Exception as e:  # noqa: BLE001
        log(f"  ! news {lang} '{query}': {e}")
        return []


def eol_cycles(product: str) -> list[dict]:
    """Branches d'un produit sur endoflife.date, normalisées.

    Chaque branche : {cycle, release, eol (date ISO, True/False), lts, latest, latest_date, link}.
    Gère l'API v1 et, en repli, l'ancienne API /api/<produit>.json.
    """
    def as_eol(v):
        return v if isinstance(v, (bool, str)) or v is None else None
    try:
        data = json.loads(fetch(f"https://endoflife.date/api/v1/products/{product}"))
        out = []
        for r in (data.get("result") or {}).get("releases", []):
            latest = r.get("latest") or {}
            eol = r.get("eolFrom") or r.get("isEol")
            out.append({"cycle": str(r.get("name")), "codename": r.get("codename"),
                        "release": r.get("releaseDate"), "eol": as_eol(eol),
                        "lts": bool(r.get("isLts")), "latest": latest.get("name"),
                        "latest_date": latest.get("date"), "link": latest.get("link")})
        if out:
            return out
    except Exception as e:  # noqa: BLE001
        log(f"  ! endoflife v1 {product}: {e}")
    try:
        data = json.loads(fetch(f"https://endoflife.date/api/{product}.json"))
        return [{"cycle": str(r.get("cycle")), "codename": r.get("codename"), "release": r.get("releaseDate"), "eol": as_eol(r.get("eol")),
                 "lts": bool(r.get("lts")), "latest": r.get("latest"),
                 "latest_date": r.get("latestReleaseDate"), "link": r.get("link")} for r in data]
    except Exception as e:  # noqa: BLE001
        log(f"  ! endoflife {product}: {e}")
        return []


def eol_status(cycle: dict) -> tuple[bool, str | None]:
    """(branche en fin de support ?, date de fin de support ISO ou None)."""
    eol = cycle.get("eol")
    if isinstance(eol, str):
        d = parse_date(eol + "T00:00:00Z") if len(eol) == 10 else parse_date(eol)
        return (bool(d and d <= NOW), eol[:10])
    return (bool(eol), None)


CODENAMES = {  # repli si endoflife.date ne fournit pas les noms de code
    "buster": "10", "bullseye": "11", "bookworm": "12", "trixie": "13", "forky": "14", "duke": "15",
    "focal": "20.04", "jammy": "22.04", "noble": "24.04", "plucky": "25.04", "questing": "25.10",
    "resolute": "26.04",
}


def eol_by_codename(cycles: list[dict], text: str) -> dict | None:
    """Branche désignée par son nom de code (« Trixie » -> Debian 13)."""
    word = (text or "").strip().lower()
    if not word:
        return None
    for c in cycles:
        if (c.get("codename") or "").lower().split(" ")[0] == word.split(" ")[0]:
            return c
    cycle = CODENAMES.get(word.split(" ")[0])
    return next((c for c in cycles if c["cycle"] == cycle), None)


def eol_match(cycles: list[dict], installed: tuple | None) -> dict | None:
    """Branche correspondant à la version installée (préfixe le plus long : 6.1 pour 6.1.115)."""
    if not installed:
        return None
    best = None
    for c in cycles:
        cv = parse_version(c["cycle"])
        if cv and installed[: len(cv)] == cv and (best is None or len(cv) > len(parse_version(best["cycle"]))):
            best = c
    return best


def parse_version(s: str | None) -> tuple[int, ...] | None:
    """'v1.2.3', 'Release 1.2.3 (…)', '2.51.0.windows.1' -> (1, 2, 3)."""
    m = re.search(r"\d+(?:\.\d+)*", s or "")
    return tuple(int(x) for x in m.group(0).split(".")) if m else None


def vcmp(a: tuple, b: tuple) -> int:
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def cpe_affects(match: dict, version: tuple) -> bool:
    """La version installée tombe-t-elle dans la plage vulnérable d'un cpeMatch NVD ?"""
    exact = match["criteria"].split(":")[5]
    if exact not in ("*", "-"):
        v = parse_version(exact)
        return bool(v) and vcmp(version, v) == 0
    bounds = (("versionStartIncluding", lambda c: c >= 0), ("versionStartExcluding", lambda c: c > 0),
              ("versionEndIncluding", lambda c: c <= 0), ("versionEndExcluding", lambda c: c < 0))
    for key, ok in bounds:
        if match.get(key):
            b = parse_version(match[key])
            if b and not ok(vcmp(version, b)):
                return False
    return True


def cpe_check(cve: dict, patterns: list[str], installed: tuple | None) -> tuple[str, bool | None]:
    """Statut produit d'une CVE d'après ses configurations CPE.

    'confirmed' : un CPE vulnérable correspond à la technologie ;
    'pending'   : CVE pas encore analysée par la NVD (aucun CPE) ;
    'other'     : analysée, mais concerne un autre produit (à écarter).
    Second élément : la version installée est-elle concernée ? (None si inconnu)
    """
    matches = [m for conf in cve.get("configurations", []) for node in conf.get("nodes", [])
               for m in node.get("cpeMatch", []) if m.get("vulnerable")]
    if not matches:
        return "pending", None
    mine = []
    for m in matches:
        parts = m.get("criteria", "").split(":")
        if len(parts) > 5 and any(fnmatch(f"{parts[3]}:{parts[4]}", p) for p in patterns):
            mine.append(m)
    if not mine:
        return "other", None
    if not installed:
        return "confirmed", None
    return "confirmed", any(cpe_affects(m, installed) for m in mine)


def nvd_cves(keyword: str, patterns: list[str] | None = None, installed: str = "") -> list[dict] | None:
    """CVE récentes de la base NVD (NIST) dont la description contient `keyword`.

    Si `patterns` (motifs 'éditeur:produit' des CPE) est fourni, les CVE déjà analysées
    qui visent un autre produit sont écartées, et la version installée est comparée
    aux plages vulnérables.

    Renvoie None en cas d'échec (pour conserver les données de la veille précédente).
    Sans clé API, la NVD autorise 5 requêtes / 30 s : l'appelant espace les appels.
    """
    start = (NOW - timedelta(days=CVE_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end = NOW.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    params = urllib.parse.urlencode({
        "keywordSearch": keyword, "keywordExactMatch": "",
        "pubStartDate": start, "pubEndDate": end, "resultsPerPage": 2000,
        "noRejected": "",
    }).replace("keywordExactMatch=&", "keywordExactMatch&").replace("&noRejected=", "&noRejected")
    url = f"https://services.nvd.nist.gov/rest/json/cves/2.0?{params}"
    headers = {"apiKey": os.environ["NVD_API_KEY"]} if os.environ.get("NVD_API_KEY") else {}
    data = None
    for attempt in range(3):
        try:
            data = json.loads(fetch(url, headers))
            break
        except Exception as e:  # noqa: BLE001
            log(f"  ! nvd '{keyword}' (essai {attempt + 1}): {e}")
            time.sleep(12 * (attempt + 1))
    if data is None:
        return None
    out = []
    for v in data.get("vulnerabilities", []):
        c = v.get("cve", {})
        if c.get("vulnStatus") == "Rejected":
            continue
        desc = next((d["value"] for d in c.get("descriptions", []) if d.get("lang") == "en"), "")
        sev, score = None, None
        metrics = c.get("metrics", {})
        for k in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
            if metrics.get(k):
                m = metrics[k][0]
                cd = m.get("cvssData", {})
                score = cd.get("baseScore")
                sev = cd.get("baseSeverity") or m.get("baseSeverity")
                break
        status, affects = ("keyword", None)
        if patterns:
            status, affects = cpe_check(c, patterns, parse_version(installed) if installed else None)
            if status == "other":
                continue
        out.append({
            "status": status,
            "affects": affects,
            "id": c.get("id"),
            "url": f"https://nvd.nist.gov/vuln/detail/{c.get('id')}",
            "date": iso(parse_date(c.get("published"))),
            "severity": (sev or "").upper() or None,
            "score": score,
            "exploited": bool(c.get("cisaExploitAdd")),
            "summary": clean_text(desc, 260),
        })
    out.sort(key=lambda x: x["date"] or "", reverse=True)
    for c in out:
        d = parse_date(c["date"])
        c["new"] = bool(d and NOW - d < timedelta(hours=48))
    return out


CVEORG_MAX_PER_TECH = 150  # CVE non analysées par la NVD vérifiées sur CVE.org, par techno


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9*]+", "_", (s or "").lower()).strip("_")


def _le_wild(v: tuple, bound: str) -> bool:
    """v <= bound, où bound peut finir par '*' (ex. '6.1.*' = toute la branche 6.1)."""
    if bound.strip() == "*":
        return True
    if bound.endswith("*"):
        p = parse_version(bound)
        return bool(p) and v[: len(p)] <= p
    b = parse_version(bound)
    return bool(b) and vcmp(v, b) <= 0


def cna_affects(entry: dict, installed: tuple) -> bool | None:
    """Statut d'une version d'après un bloc 'affected' du format CVE 5 (CVE.org)."""
    hits = []
    for ver in entry.get("versions", []):
        if ver.get("versionType") == "git":
            continue
        start = parse_version(ver.get("version")) if ver.get("version") not in (None, "*", "n/a") else (0,)
        if start is None:
            continue
        lt, le = ver.get("lessThan"), ver.get("lessThanOrEqual")
        if lt or le:
            inside = vcmp(installed, start) >= 0
            if le:
                inside = inside and _le_wild(installed, le)
            if lt and lt.strip() != "*":
                if lt.endswith("*"):
                    inside = inside and _le_wild(installed, lt)
                else:
                    b = parse_version(lt)
                    inside = inside and bool(b) and vcmp(installed, b) < 0
        else:
            inside = vcmp(installed, start) == 0
        if inside:
            hits.append(ver.get("status"))
    if "unaffected" in hits:
        return False
    if "affected" in hits:
        return True
    default = entry.get("defaultStatus")
    return True if default == "affected" else False if default == "unaffected" else None


def cveorg_check(cve_id: str, patterns: list[str], name: str, installed: tuple | None):
    """Interroge CVE.org (données de l'organisme qui publie la CVE, disponibles dès sa publication).

    Renvoie (statut, version installée concernée ?) ou None si l'API ne répond pas.
    """
    try:
        rec = json.loads(fetch(f"https://cveawg.mitre.org/api/cve/{cve_id}"))
    except Exception as e:  # noqa: BLE001
        log(f"  ! cve.org {cve_id}: {e}")
        return None
    cont = rec.get("containers", {})
    entries = list(cont.get("cna", {}).get("affected", []))
    for adp in cont.get("adp", []):
        entries += adp.get("affected", [])
    entries = [e for e in entries if e.get("product") or e.get("vendor")]
    if not entries:
        return ("pending", None)
    key = _slug(name)
    mine = [e for e in entries
            if any(fnmatch(f"{_slug(e.get('vendor'))}:{_slug(e.get('product'))}", p) for p in patterns)
            or (key and key in _slug(e.get("product")))]
    if not mine:
        return ("other", None)
    if not installed:
        return ("cna", None)
    verdicts = [cna_affects(e, installed) for e in mine]
    return ("cna", True if True in verdicts else False if False in verdicts else None)


KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
KEV_WINDOW_DAYS = 90       # ajouts récents au catalogue KEV affichés par technologie
EPSS_URL = "https://api.first.org/data/v1/epss"


def fetch_kev() -> list[dict]:
    """Catalogue CISA KEV : vulnérabilités exploitées dans la nature."""
    try:
        data = json.loads(fetch(KEV_URL))
    except Exception as e:  # noqa: BLE001
        log(f"  ! CISA KEV: {e}")
        return []
    out = []
    for v in data.get("vulnerabilities", []):
        out.append({
            "id": v.get("cveID"), "vendor": v.get("vendorProject"), "product": v.get("product"),
            "name": v.get("vulnerabilityName"), "summary": clean_text(v.get("shortDescription"), 240),
            "added": v.get("dateAdded"), "due": v.get("dueDate"),
            "ransomware": (v.get("knownRansomwareCampaignUse") or "").lower() == "known",
            "url": f"https://nvd.nist.gov/vuln/detail/{v.get('cveID')}",
        })
    out.sort(key=lambda x: x["added"] or "", reverse=True)
    log(f"  CISA KEV : {len(out)} vulnérabilités au catalogue")
    return out


def kev_for(kev: list[dict], terms: list[str]) -> list[dict]:
    """Entrées KEV récentes qui concernent une technologie (nom cité dans produit/titre/description)."""
    limit = (NOW - timedelta(days=KEV_WINDOW_DAYS)).strftime("%Y-%m-%d")
    out = []
    for k in kev:
        if (k["added"] or "") < limit:
            break
        text = norm(f"{k['vendor']} {k['product']} {k['name']} {k['summary']}")
        if any(t in text for t in terms):
            out.append(k)
    return out


def fetch_epss(ids: list[str]) -> dict[str, dict]:
    """Scores EPSS (FIRST) : probabilité d'exploitation dans les 30 jours."""
    scores: dict[str, dict] = {}
    ids = sorted({i for i in ids if i})
    for start in range(0, len(ids), 80):
        chunk = ids[start:start + 80]
        try:
            data = json.loads(fetch(f"{EPSS_URL}?cve={','.join(chunk)}"))
        except Exception as e:  # noqa: BLE001
            log(f"  ! EPSS: {e}")
            continue
        for d in data.get("data", []):
            try:
                scores[d["cve"]] = {"epss": float(d["epss"]), "percentile": float(d["percentile"])}
            except (KeyError, TypeError, ValueError):
                pass
    log(f"  EPSS : {len(scores)}/{len(ids)} scores")
    return scores


def enrich_exploitation(results: list[dict], kev: list[dict], sources: dict, threats: dict) -> None:
    """Ajoute KEV (exploitée) et EPSS (probabilité) aux CVE, et les ajouts KEV récents par techno."""
    kev_ids = {k["id"] for k in kev}
    for r in results:
        cfg = sources.get(tech_key(r["name"]), {})
        terms = [" " + norm(t).strip() for t in cfg.get("match", [r["name"]])]
        r["kev"] = kev_for(kev, terms)[:5]
        for c in r["cves"]:
            if c["id"] in kev_ids:
                c["exploited"] = True
    ids = [c["id"] for r in results for c in r["cves"]] + [k["id"] for r in results for k in r["kev"]]
    ids += [k["id"] for k in kev[:15]]
    scores = fetch_epss(ids)
    for r in results:
        for c in r["cves"] + r["kev"]:
            if c["id"] in scores:
                c.update(scores[c["id"]])
    tracked = {k["id"]: r["name"] for r in results for k in r["kev"]}
    threats["kev"] = [dict(k, **scores.get(k["id"], {}), tech=tracked.get(k["id"])) for k in kev[:15]]


def collect_cves(results: list[dict], sources: dict, previous: dict) -> None:
    """Interroge la NVD séquentiellement (limite de débit) et remplit r['cves']."""
    prev = {r["name"]: r.get("cves", []) for r in previous.get("results", [])}
    pause = 0.8 if os.environ.get("NVD_API_KEY") else 6.5
    for i, r in enumerate(results):
        cfg = sources.get(tech_key(r["name"]), {})
        keyword = cfg.get("cve", r["name"])
        if not keyword:  # "cve": "" dans sources.json pour désactiver
            r["cves"], r["cve_total"] = [], 0
            continue
        if i:
            time.sleep(pause)
        found = nvd_cves(keyword, cfg.get("cpe"), r.get("effective_version") or "")
        if found and cfg.get("cpe"):
            # CVE pas encore analysées par la NVD : on complète avec CVE.org.
            pending = [c for c in found if c["status"] == "pending"][:CVEORG_MAX_PER_TECH]
            iv = parse_version(r.get("effective_version") or "")
            with ThreadPoolExecutor(max_workers=8) as pool:
                checks = list(pool.map(lambda c: cveorg_check(c["id"], cfg["cpe"], r["name"], iv), pending))
            for c, res in zip(pending, checks):
                if res:
                    c["status"], c["affects"] = res
            found = [c for c in found if c["status"] != "other"]
        if found is None:
            r["cves"] = prev.get(r["name"], [])
            r["cve_total"] = len(r["cves"])
            r["cve_stale"] = True
        else:
            # Les CVE qui touchent la version installée passent en priorité dans les 5 affichées.
            mine = [c for c in found if c.get("affects")]
            rest = [c for c in found if not c.get("affects")]
            r["cves"] = sorted(mine[:MAX_CVES] + rest[: max(0, MAX_CVES - len(mine))],
                               key=lambda c: c["date"] or "", reverse=True)
            r["cve_total"] = len(found)
            r["cves_affecting"] = len(mine)
            r["cves_pending"] = sum(1 for c in found if c["status"] == "pending")
            r["cves_cna"] = sum(1 for c in found if c["status"] == "cna")
        log(f"- CVE {r['name']} ({keyword}) : {r['cve_total']} "
            f"(CVE.org {r.get('cves_cna', 0)}, à confirmer {r.get('cves_pending', 0)}, "
            f"version installée {r.get('cves_affecting', 0)})")


# --------------------------------------------------------------------------- pipeline
def collect(tech: dict, cfg: dict) -> dict:
    name = tech["name"]
    log(f"- {name}")
    info = tech["info"]
    repo = github_repo_from(info) or cfg.get("github")
    releases, repo_meta = ([], None)
    if cfg.get("kernel"):
        releases = kernel_releases()
    elif repo:
        releases, repo_meta = gh_releases(repo)

    query = cfg.get("query") or f'"{name}"'
    news: list[dict] = []
    for f in cfg.get("feeds", []):
        news += official_feed(f)
    negative = " ".join(f"-{w}" for w in NEWS_FILTER_CFG.get("mots_exclus_recherche", []))
    news += google_news(query, "fr", negative)
    news += google_news(query, "en", negative)
    # Termes devant figurer dans le titre : "match" de sources.json, sinon le nom.
    terms = [" " + norm(t).strip() for t in cfg.get("match", [name])]
    extra = re.compile("|".join(cfg["exclure"]), re.I) if cfg.get("exclure") else None
    news = [n for n in news if NEWS_FILTER.ok(n, terms) and not (extra and extra.search(n["title"]))]
    # Sites de référence : la techno peut être citée dans le titre ou le résumé.
    for n in REFERENCE_ITEMS:
        text = norm(n["title"] + " " + n.get("summary", ""))
        if any(t in text for t in terms) and not NEWS_FILTER.title_re.search(n["title"]) \
                and not (extra and extra.search(n["title"])):
            news.append(dict(n))

    cutoff = NOW - timedelta(days=NEWS_WINDOW_DAYS)
    seen_urls, seen_titles, kept = set(), set(), []
    for n in sorted(news, key=lambda x: x["date"] or "", reverse=True):
        d = parse_date(n["date"])
        if d and d < cutoff:
            continue
        key_t = re.sub(r"\W+", "", n["title"].lower())[:80]
        if n["url"] in seen_urls or key_t in seen_titles:
            continue
        seen_urls.add(n["url"]); seen_titles.add(key_t)
        kept.append(n)
    # Même information reprise par plusieurs médias (ou en FR et en EN) : un seul article.
    kept = group_stories(kept, cfg.get("match", []) + [name])
    # Classement par pertinence (officiel, référence, sécurité/version, fraîcheur), puis par date.
    # Au plus 2 articles par média, et 1 par média et par jour (évite les doublons FR/EN).
    kept.sort(key=lambda x: (score_news(x), x["date"] or ""), reverse=True)
    per_source: dict[str, int] = {}
    per_day: set[tuple] = set()
    final = []
    for n in kept:
        src = norm(n.get("source") or "").strip()
        day = (src, (n.get("date") or "")[:10])
        if not n.get("official") and (per_source.get(src, 0) >= 2 or day in per_day):
            continue
        per_source[src] = per_source.get(src, 0) + 1
        per_day.add(day)
        final.append(n)
        if len(final) >= MAX_NEWS:
            break
    final.sort(key=lambda x: x["date"] or "", reverse=True)

    seen_rel, uniq = set(), []
    for x in releases:
        k = norm(x.get("tag") or x.get("version") or "").strip().lstrip("v")
        if k and k in seen_rel:
            continue
        seen_rel.add(k)
        uniq.append(x)
    releases = uniq

    links = []
    if cfg.get("site"):
        links.append({"label": "Site officiel", "url": cfg["site"]})
    if repo:
        links.append({"label": "GitHub", "url": f"https://github.com/{repo}"})
    if info and not info.startswith("http"):
        links.append({"label": f"Note : {info}", "url": None})

    installed = tech.get("installed", "")
    latest, status = None, None
    track_latest = is_latest(installed)
    iv = None if track_latest else parse_version(installed)
    effective = None  # version réellement comparée aux CVE

    # endoflife.date : branches, dernière version de chaque branche et fin de support.
    eol = None
    if cfg.get("eol"):
        cycles = eol_cycles(cfg["eol"])
        if cycles:
            current = cycles[0]
            if not releases and current.get("latest"):  # pas de releases GitHub : on prend endoflife.date
                releases = [{"version": f"{current['latest']}", "tag": current["latest"],
                             "date": iso(parse_date((current.get("latest_date") or "") + "T00:00:00Z"))
                             if current.get("latest_date") else None,
                             "url": current.get("link") or f"https://endoflife.date/{cfg['eol']}"}]
            if track_latest:
                mine = current
            elif iv:
                mine = eol_match(cycles, iv)
            else:
                mine = eol_by_codename(cycles, installed)  # ex. « Trixie »
                if mine and mine.get("latest"):
                    effective = mine["latest"]
            eol = {"product": cfg["eol"], "url": f"https://endoflife.date/{cfg['eol']}",
                   "current": current["cycle"]}
            if mine:
                ended, until = eol_status(mine)
                eol.update(cycle=mine["cycle"], ended=ended, until=until, lts=mine.get("lts"),
                           codename=mine.get("codename"), cycle_latest=mine.get("latest"))
                if effective and not iv:  # nom de code : on suit la dernière version de la branche
                    latest, status = mine["latest"], "branch"
                elif iv and mine.get("latest") and parse_version(mine["latest"]):
                    # mise à jour comparée dans la branche installée (ex. 6.1.x, Debian 12.x)
                    latest = mine["latest"]
                    status = "update" if vcmp(iv, parse_version(latest)) < 0 else "ok"

    if track_latest:
        # « latest » : on suit toujours la dernière version publiée.
        cands = [(parse_version(x["tag"] or x["version"]), x) for x in releases]
        cands = [(v, x) for v, x in cands if v]
        if cands:
            v, x = max(cands, key=lambda t: t[0] + (0,) * (6 - len(t[0])))
            latest = effective = x["tag"] or x["version"]
        status = "latest"
    elif iv:
        effective = installed

    if iv and releases and status is None:
        cands = [(parse_version(x["tag"] or x["version"]), x) for x in releases]
        cands = [(v, x) for v, x in cands if v]
        if cfg.get("kernel"):  # noyau : comparer dans la même branche (ex. 6.1.x LTS)
            same = [(v, x) for v, x in cands if v[:2] == iv[:2]]
            cands = same or cands
        if cands:
            v, x = max(cands, key=lambda t: t[0] + (0,) * (6 - len(t[0])))
            latest = x["tag"] or x["version"]
            status = "update" if vcmp(iv, v) < 0 else "ok"

    return {
        "name": name,
        "installed": installed,
        "latest_version": latest,
        "version_status": status,
        "effective_version": effective,
        "eol": eol,
        "category": cfg.get("category", "Autres"),
        "repo": repo,
        "repo_meta": repo_meta,
        "releases": releases,
        "news": final,
        "news_count": len(kept),
        "links": links,
    }


def dedupe_across(results: list[dict], sources: dict) -> None:
    """Un article, une CVE ou une faille KEV n'apparaît qu'une fois sur tout le site."""
    def title_key(t: str) -> str:
        return re.sub(r"\W+", "", re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", t).lower())[:80]

    def where(r: dict, title: str) -> int:
        """Position de la techno dans le titre (-1 si absente) : la plus à droite est la plus précise
        (« noyau Linux de Debian » → Debian)."""
        cfg = sources.get(tech_key(r["name"]), {})
        t = norm(title)
        return max((t.rfind(" " + norm(m).strip()) for m in cfg.get("match", []) + [r["name"]]), default=-1)

    owners: dict[str, list[dict]] = {}
    for r in results:
        for n in r["news"]:
            for k in {n["url"], title_key(n["title"])} | {a["url"] for a in n.get("also", [])}:
                owners.setdefault(k, [])
                if r not in owners[k]:
                    owners[k].append(r)
    removed = 0
    for r in results:
        keep = []
        for n in r["news"]:
            keys = {n["url"], title_key(n["title"])} | {a["url"] for a in n.get("also", [])}
            rivals = list({id(o): o for k in keys for o in owners.get(k, [])}.values())
            if len(rivals) > 1:
                best = max(rivals, key=lambda o: (where(o, n["title"]), -len(o["news"]), -results.index(o)))
                if best is not r:
                    removed += 1
                    continue
            keep.append(n)
        r["news"] = keep
    # CVE : gardée là où elle touche la version installée, sinon dans la première techno.
    owner: dict[str, dict] = {}
    for r in results:
        for c in r.get("cves", []):
            cur = owner.get(c["id"])
            if cur is None or (c.get("affects") and not any(
                    x.get("affects") for x in cur.get("cves", []) if x["id"] == c["id"])):
                owner[c["id"]] = r
    for r in results:
        before = len(r.get("cves", []))
        r["cves"] = [c for c in r.get("cves", []) if owner.get(c["id"]) is r]
        removed += before - len(r["cves"])
    # KEV : inutile de répéter une CVE déjà affichée (elle porte déjà le badge « exploitée »).
    shown = set(owner)
    for r in results:
        before = len(r.get("kev", []))
        r["kev"] = [k for k in r.get("kev", []) if k["id"] not in shown]
        shown |= {k["id"] for k in r["kev"]}
        removed += before - len(r["kev"])
    for r in results:
        r["new_count"] = sum(1 for i in r["news"] + r["releases"] if i.get("new"))
    log(f"Doublons entre technologies écartés : {removed}")


def mark_new(results: list[dict], history: dict) -> dict:
    """Marque comme « nouveau » ce qui n'a jamais été vu lors des passages précédents."""
    today = NOW.strftime("%Y-%m-%d")
    first_run = not history
    for r in results:
        for item in r["news"] + r["releases"]:
            url = item.get("url")
            if not url:
                continue
            first = history.setdefault(url, today)
            item["new"] = (first == today) and not first_run
            if first_run:  # premier passage : ce qui date de < 48 h est « nouveau »
                d = parse_date(item.get("date"))
                item["new"] = bool(d and NOW - d < timedelta(hours=48))
        r["new_count"] = sum(1 for i in r["news"] + r["releases"] if i.get("new"))
    limit = (NOW - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    return {u: d for u, d in history.items() if d >= limit}


def render(payload: dict) -> str:
    tpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return tpl.replace("/*__DATA__*/null", blob)


def main() -> int:
    global NEWS_FILTER, NEWS_FILTER_CFG
    techs = read_technologies()
    sources, NEWS_FILTER_CFG = load_sources()
    NEWS_FILTER = NewsFilter(NEWS_FILTER_CFG)
    raw_sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    REFERENCE_ITEMS.extend(fetch_references(raw_sources.get("_references", [])))
    threats = fetch_ransomware(raw_sources.get("_ransomware", {}))
    threats["news"] = sorted([i for i in REFERENCE_ITEMS if i.get("ref_kind") == "cyber"],
                             key=lambda x: x["date"] or "", reverse=True)[:15]
    log(f"{len(techs)} technologies à suivre")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda t: collect(t, sources.get(tech_key(t["name"]), {})), techs))

    previous = {}
    if DATA_PATH.exists():
        try:
            previous = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
    log(f"Filtre actualités : {NEWS_FILTER.dropped} articles écartés")
    history = mark_new(results, previous.get("history", {}))
    collect_cves(results, sources, previous)
    enrich_exploitation(results, fetch_kev(), sources, threats)
    dedupe_across(results, sources)

    total_items = sum(len(r["news"]) + len(r["releases"]) for r in results)
    if total_items == 0 and previous.get("results"):
        log("Aucune donnée collectée (réseau ?) : on conserve la version précédente.")
        return 1

    payload = {
        "generated_at": iso(NOW),
        "threats": threats,
        "results": results,
        "stats": {
            "technologies": len(results),
            "cves": sum(len(r["cves"]) for r in results),
            "to_update": sum(1 for r in results if r.get("version_status") == "update"),
            "eol_ended": sum(1 for r in results if (r.get("eol") or {}).get("ended")),
            "cves_affecting": sum(r.get("cves_affecting", 0) for r in results),
            "exploited": len({c["id"] for r in results for c in r["cves"] if c.get("exploited")}
                             | {k["id"] for r in results for k in r.get("kev", [])}),
            "cves_critical": sum(1 for r in results for c in r["cves"] if c["severity"] in ("CRITICAL", "HIGH")),
            "news": sum(len(r["news"]) for r in results),
            "new": sum(r["new_count"] for r in results),
            "releases_week": sum(
                1 for r in results for rel in r["releases"][:1]
                if (d := parse_date(rel.get("date"))) and NOW - d < timedelta(days=7)
            ),
        },
    }
    DATA_PATH.parent.mkdir(exist_ok=True)
    DATA_PATH.write_text(json.dumps({**payload, "history": history}, ensure_ascii=False, indent=1), encoding="utf-8")
    OUT_PATH.write_text(render(payload), encoding="utf-8")
    log(f"OK : {payload['stats']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
