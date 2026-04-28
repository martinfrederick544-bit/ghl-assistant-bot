#!/usr/bin/env python3
"""
Lead Finder — Ateliers de fabrication/conception en génie mécanique
Région : Montréal / Québec, Canada

Usage:
    python lead_finder.py 30          # importe 30 leads
    python lead_finder.py 10 --dry-run  # simule sans importer dans GHL
"""

import os, re, json, sys, time, logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

GHL_API_KEY     = os.environ.get("GHL_API_KEY", "")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID", "")
GHL_PIPELINE_ID = os.environ.get("GHL_PIPELINE_ID", "")
GHL_STAGE_ID    = os.environ.get("GHL_STAGE_ID", "")   # optionnel

GHL_BASE = "https://services.leadconnectorhq.com"
GHL_HEADERS = {
    "Authorization": f"Bearer {GHL_API_KEY}",
    "Content-Type":  "application/json",
    "Version":       "2021-07-28",
}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Mots-clés de recherche sur Pages Jaunes
SEARCH_KEYWORDS = [
    "usinage CNC",
    "atelier usinage",
    "fabrication mécanique",
    "tôlerie industrielle",
    "atelier soudure fabrication",
    "usinage précision",
    "fabrication pièces mécaniques",
    "atelier mécanique industriel",
]

# Villes de la région Montréal/Québec
SEARCH_CITIES = [
    "Montreal+QC",
    "Laval+QC",
    "Longueuil+QC",
    "Brossard+QC",
    "Saint-Laurent+QC",
    "Terrebonne+QC",
    "Repentigny+QC",
    "Quebec+QC",
]

# Patterns pour exclure les faux emails
JUNK_EMAIL_PATTERNS = [
    "example.", "test@", "noreply", "no-reply", "donotreply",
    ".png", ".jpg", ".gif", ".svg", ".webp", "@2x",
    "sentry.", "wixpress.", "wordpress.", "schema.",
    "jquery.", "bootstrap.", "fontawesome.",
    "domain.com", "email.com", "yourcompany", "votre@",
    "info@example", "contact@example",
]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


# ── Scraping Pages Jaunes ─────────────────────────────────────────────────────

def scrape_pages_jaunes(keyword: str, city: str, page: int = 1) -> list:
    url = (
        f"https://www.pagesjaunes.ca/search/si/{page}/"
        f"{keyword.replace(' ', '+')}/{city}"
    )
    log.debug(f"PJ GET {url}")
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
        if r.status_code != 200:
            log.warning(f"Pages Jaunes {r.status_code} — {keyword} / {city}")
            return []

        soup = BeautifulSoup(r.text, "lxml")
        results = []

        for card in soup.select(
            "[class*='listing__content'], [class*='result-with-buttons-wrap']"
        ):
            name_el  = card.select_one("[class*='listing__name'], h3.listing__name")
            phone_el = card.select_one("[class*='mlr__item--phone'], [data-bi-name='phone']")
            web_el   = card.select_one(
                "a[data-bi-name='website'], a[class*='listing__website-link']"
            )
            addr_el  = card.select_one(
                "[class*='listing__address--full'], [class*='listing__address']"
            )

            if not name_el:
                continue

            website = web_el.get("href", "") if web_el else ""
            # Pages Jaunes emballe les URLs externes — extraire la vraie URL
            if website and ("redirect" in website or "pagesjaunes.ca/link" in website):
                m = re.search(r"url=([^&]+)", website)
                if m:
                    website = requests.utils.unquote(m.group(1))

            # Ignorer les entrées sans site web (on ne peut pas vérifier l'email)
            if not website or not website.startswith("http"):
                continue

            results.append({
                "name":    name_el.get_text(strip=True),
                "phone":   phone_el.get_text(strip=True) if phone_el else "",
                "website": website,
                "address": addr_el.get_text(strip=True)  if addr_el  else "",
                "email":   "",
                "source":  "Pages Jaunes",
            })

        return results

    except Exception as e:
        log.error(f"scrape_pages_jaunes: {e}")
        return []


# ── Extraction email depuis site web ──────────────────────────────────────────

def _filter_emails(raw: list) -> list:
    out = []
    for e in raw:
        e = e.lower().strip().rstrip(".")
        if len(e) < 7 or e.count("@") != 1:
            continue
        if any(p in e for p in JUNK_EMAIL_PATTERNS):
            continue
        out.append(e)
    # Dédupliquer en gardant l'ordre
    return list(dict.fromkeys(out))


def _emails_from_html(html: str) -> list:
    decoded = (
        html
        .replace("%40", "@")
        .replace("&#64;", "@")
        .replace("&#x40;", "@")
        .replace("[at]", "@")
        .replace("(at)", "@")
    )
    return _filter_emails(EMAIL_RE.findall(decoded))


def extract_email_from_website(url: str) -> str:
    """
    Visite le site web et les pages de contact pour trouver un email réel.
    Retourne une chaîne vide si aucun email valide n'est trouvé.
    """
    if not url or not url.startswith("http"):
        return ""
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=12, allow_redirects=True)
        emails = _emails_from_html(r.text)
        if emails:
            return emails[0]

        # Chercher la page de contact
        soup = BeautifulSoup(r.text, "lxml")
        contact_hrefs = []
        for a in soup.find_all("a", href=True):
            href = a["href"].lower()
            if any(k in href for k in ["contact", "nous-joindre", "joindre", "rejoindre", "coordonnee"]):
                contact_hrefs.append(a["href"])

        for href in contact_hrefs[:3]:
            contact_url = urljoin(url, href)
            try:
                cr = requests.get(contact_url, headers=BROWSER_HEADERS, timeout=10)
                emails = _emails_from_html(cr.text)
                if emails:
                    return emails[0]
            except Exception:
                pass

    except Exception as e:
        log.debug(f"extract_email error for {url}: {e}")

    return ""


# ── GHL helpers ───────────────────────────────────────────────────────────────

def ghl_contact_exists(email: str) -> bool:
    """Vérifie si un contact avec cet email existe déjà dans GHL."""
    try:
        r = requests.get(
            f"{GHL_BASE}/contacts/search",
            headers=GHL_HEADERS,
            params={"locationId": GHL_LOCATION_ID, "query": email},
            timeout=10,
        )
        contacts = r.json().get("contacts", [])
        return any(c.get("email", "").lower() == email.lower() for c in contacts)
    except Exception:
        return False


def ghl_create_contact(biz: dict) -> dict:
    """Crée un contact dans GHL à partir d'un dict business."""
    parts = biz["name"].split(" ", 1)
    payload = {
        "locationId":  GHL_LOCATION_ID,
        "firstName":   parts[0],
        "lastName":    parts[1] if len(parts) > 1 else "",
        "companyName": biz["name"],
        "email":       biz["email"],
        "phone":       biz.get("phone", ""),
        "address1":    biz.get("address", ""),
        "source":      "Lead Finder — Génie mécanique",
        "tags":        ["atelier-fabrication", "génie-mécanique", "montréal-qc"],
    }
    r = requests.post(f"{GHL_BASE}/contacts/", headers=GHL_HEADERS, json=payload, timeout=10)
    log.debug(f"GHL create_contact {r.status_code} {r.text[:200]}")
    return r.json()


def ghl_add_to_pipeline(contact_id: str, title: str) -> dict:
    """Ajoute une opportunité dans le pipeline configuré."""
    payload = {
        "locationId": GHL_LOCATION_ID,
        "pipelineId": GHL_PIPELINE_ID,
        "contactId":  contact_id,
        "name":       title,
        "status":     "open",
    }
    if GHL_STAGE_ID:
        payload["pipelineStageId"] = GHL_STAGE_ID
    r = requests.post(f"{GHL_BASE}/opportunities/", headers=GHL_HEADERS, json=payload, timeout=10)
    log.debug(f"GHL add_opportunity {r.status_code} {r.text[:200]}")
    return r.json()


# ── Moteur principal ──────────────────────────────────────────────────────────

def find_and_import_leads(target: int = 30, dry_run: bool = False) -> dict:
    """
    1. Collecte des entreprises sur Pages Jaunes
    2. Extrait les emails depuis leurs sites web
    3. Importe dans GHL (contact + opportunité dans le pipeline)
    """
    log.info(f"{'[DRY-RUN] ' if dry_run else ''}Objectif : {target} leads qualifiés")

    # ── Étape 1 : Collecte ───────────────────────────────────────────────────
    raw = []
    for city in SEARCH_CITIES:
        if len(raw) >= target * 4:
            break
        for kw in SEARCH_KEYWORDS:
            if len(raw) >= target * 4:
                break
            batch = scrape_pages_jaunes(kw, city)
            if batch:
                log.info(f"  [{city}] {kw} → {len(batch)} résultats")
                raw.extend(batch)
            time.sleep(1.2)

    # ── Étape 2 : Déduplication par nom d'entreprise ─────────────────────────
    seen_names = set()
    unique = []
    for biz in raw:
        key = biz["name"].lower().strip()
        if key not in seen_names:
            seen_names.add(key)
            unique.append(biz)

    log.info(f"Entreprises uniques avec site web : {len(unique)}")

    # ── Étape 3 : Email + Import ─────────────────────────────────────────────
    imported      = []
    no_email      = []
    already_in_ghl = []
    errors        = []

    for biz in unique:
        if len(imported) >= target:
            break

        log.info(f"→ {biz['name']}  ({biz['website']})")
        email = extract_email_from_website(biz["website"])
        time.sleep(0.8)

        if not email:
            no_email.append(biz["name"])
            log.info("    ✗ aucun email trouvé — ignoré")
            continue

        biz["email"] = email

        if not dry_run and ghl_contact_exists(email):
            already_in_ghl.append(biz["name"])
            log.info(f"    ⚠  déjà dans GHL ({email}) — ignoré")
            continue

        if dry_run:
            imported.append({**biz})
            log.info(f"    ✅ [DRY-RUN] {email}")
            continue

        # Créer le contact
        c_result = ghl_create_contact(biz)
        contact  = c_result.get("contact", {})
        if not contact or not contact.get("id"):
            errors.append(biz["name"])
            log.warning(f"    ✗ création contact échouée : {c_result}")
            continue

        # Ajouter au pipeline
        opp = ghl_add_to_pipeline(contact["id"], f"Lead — {biz['name']}")
        if opp.get("opportunity") or opp.get("id"):
            imported.append({
                "name":    biz["name"],
                "email":   email,
                "phone":   biz.get("phone", ""),
                "address": biz.get("address", ""),
                "website": biz["website"],
            })
            log.info(f"    ✅ importé : {email}")
        else:
            errors.append(biz["name"])
            log.warning(f"    ✗ erreur pipeline : {opp}")

        time.sleep(0.5)

    return {
        "imported":       imported,
        "no_email":       no_email,
        "already_in_ghl": already_in_ghl,
        "errors":         errors,
        "total_scraped":  len(unique),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _check_config():
    missing = [v for v in ["GHL_API_KEY", "GHL_LOCATION_ID", "GHL_PIPELINE_ID"]
               if not os.environ.get(v)]
    if missing:
        print(f"⚠️  Variables manquantes dans .env : {', '.join(missing)}")
        print("   Copie .env.example → .env et remplis les valeurs.")
        sys.exit(1)


if __name__ == "__main__":
    args    = sys.argv[1:]
    target  = 10
    dry_run = "--dry-run" in args

    for a in args:
        if a.isdigit():
            target = int(a)

    if not dry_run:
        _check_config()

    results = find_and_import_leads(target, dry_run=dry_run)

    w = 56
    print(f"\n{'═' * w}")
    mode = " [DRY-RUN — rien importé dans GHL]" if dry_run else ""
    print(f"  Résultats{mode}")
    print(f"{'═' * w}")
    print(f"  ✅  Importés           : {len(results['imported'])}")
    print(f"  ✗   Sans email         : {len(results['no_email'])}")
    print(f"  ⚠   Déjà dans GHL      : {len(results['already_in_ghl'])}")
    print(f"  ✗   Erreurs            : {len(results['errors'])}")
    print(f"  📊  Total scrapé       : {results['total_scraped']}")

    if results["imported"]:
        print(f"\n  Leads {'(simulés) ' if dry_run else ''}importés :")
        for lead in results["imported"]:
            print(f"    • {lead['name']}")
            print(f"      {lead['email']}  |  {lead.get('phone', '')}")
            if lead.get("address"):
                print(f"      {lead['address']}")
    print()
