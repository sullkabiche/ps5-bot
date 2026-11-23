#!/usr/bin/env python3
"""
Bot d'alerte PS5 Digital (France) - version GitHub Actions, v2
- Pages produit : alerte restock (indispo -> dispo) et baisse de prix
- Pages "liste" (comparateurs, recherches) : alerte sur les NOUVELLES offres,
  les offres de retour et les baisses de prix (différence avec le passage précédent)
- Google Shopping (optionnel) via SerpApi
- Mémoire entre deux passages dans ps5_state.json
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

PRICE_TARGET = 599
PRICE_TOLERANCE = 100
PRICE_MIN = PRICE_TARGET - PRICE_TOLERANCE
PRICE_MAX = PRICE_TARGET + PRICE_TOLERANCE

CHECK_INTERVAL = 180  # mode boucle locale uniquement
JITTER = 30
MAX_LINES_PER_ALERT = 6

BASE = Path(__file__).parent
URLS_FILE = BASE / "urls.txt"
STATE_FILE = BASE / "ps5_state.json"

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Google Shopping via SerpApi (optionnel, désactivé si la clé est vide)
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "").strip()
SERPAPI_QUERY = os.getenv("SERPAPI_QUERY", "console PS5 Digital edition")
SERPAPI_EVERY_MIN = int(os.getenv("SERPAPI_EVERY_MIN", "480") or 480)  # 3 recherches/jour

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

UNAVAILABLE_WORDS = [
    "indisponible", "épuisé", "rupture de stock", "victime de son succès",
    "plus disponible", "actuellement indisponible", "bientôt disponible",
    "produit non disponible", "alerte disponibilité", "me prévenir",
]
AVAILABLE_WORDS = [
    "ajouter au panier", "ajouter à mon panier", "acheter maintenant",
    "en stock", "disponible immédiatement", "add to cart",
]

# Filtres pour les pages "liste"
KEYWORDS = ("ps5", "playstation 5", "playstation®5", "playstation5")
DIGITAL_WORDS = ("digital", "numérique", "numerique")
BAD_WORDS = (
    "lecteur de disque", "lecteur disque", "disc drive", "casque", "pulse",
    "station de charge", "coque", "façade", "facade", "housse",
    "ps5 pro", "playstation 5 pro", "reconditionn", "occasion",
)
PRICE_RE = re.compile(r"(\d[\d\s\u202f\xa0]{0,6}(?:[.,]\d{1,2})?)\s*€(\d{2})?")


def log(msg):
    print(f"[{datetime.now():%d/%m %H:%M:%S}] {msg}", flush=True)


# ----------------------------------------------------------------------------
# LECTURE DE urls.txt
# ----------------------------------------------------------------------------

def load_products():
    """Lignes : Enseigne | URL            (page produit)
                Enseigne | URL | liste    (page de recherche / comparateur)"""
    products = []
    if not URLS_FILE.exists():
        return products
    for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 1:
            store, url, kind = "Site", parts[0], "produit"
        else:
            store, url = parts[0], parts[1]
            kind = "liste" if len(parts) > 2 and parts[2].lower().startswith("list") else "produit"
        if url.startswith("http"):
            products.append({"store": store, "url": url, "kind": kind})
    return products


# ----------------------------------------------------------------------------
# PAGES PRODUIT
# ----------------------------------------------------------------------------

def _walk_jsonld(node):
    if isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)
    elif isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if "Offer" in types or "AggregateOffer" in types:
            yield node
        for v in node.values():
            yield from _walk_jsonld(v)


def _to_price(value):
    if value is None:
        return None
    s = str(value).replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    s = s.replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def analyse_page(html):
    soup = BeautifulSoup(html, "html.parser")
    available, price = None, None
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for offer in _walk_jsonld(data):
            avail = str(offer.get("availability", ""))
            if avail:
                if any(k in avail for k in ("InStock", "LimitedAvailability", "OnlineOnly")):
                    available = True
                elif available is None:
                    available = False
            p = _to_price(offer.get("price") or offer.get("lowPrice"))
            if p:
                price = p
    if available is not None:
        return available, price, "json-ld"

    text = soup.get_text(" ", strip=True).lower()
    if price is None:
        meta = soup.find("meta", property="product:price:amount") or soup.find(
            "meta", itemprop="price"
        )
        if meta and meta.get("content"):
            price = _to_price(meta["content"])
    if any(w in text for w in UNAVAILABLE_WORDS):
        return False, price, "mots-clés"
    if any(w in text for w in AVAILABLE_WORDS):
        return True, price, "mots-clés"
    return None, price, "indéterminé"


def fetch(session, url):
    try:
        r = session.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as e:
        return None, f"réseau: {e}"
    if r.status_code in (403, 429, 503):
        return None, f"bloqué (HTTP {r.status_code})"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    return r.text, None


# ----------------------------------------------------------------------------
# PAGES LISTE (nouvelles offres)
# ----------------------------------------------------------------------------

def find_prices(text):
    out = []
    for m in PRICE_RE.finditer(text):
        raw = re.sub(r"[\s\u202f\xa0]", "", m.group(1)).replace(",", ".")
        try:
            v = float(raw)
        except ValueError:
            continue
        if m.group(2):  # format 549€95
            v += int(m.group(2)) / 100
        out.append(v)
    return out


def is_ps5_digital(text):
    low = text.lower()
    return (
        any(k in low for k in KEYWORDS)
        and any(d in low for d in DIGITAL_WORDS)
        and not any(b in low for b in BAD_WORDS)
    )


def extract_items(html, base_url):
    """Repère les offres PS5 Digital d'une page liste. Renvoie {clé: {title, price, url}}."""
    soup = BeautifulSoup(html, "html.parser")
    items = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("#", "javascript:", "mailto:")):
            continue
        node, text = a, ""
        while node is not None and node.name not in ("body", "html", "[document]"):
            t = node.get_text(" ", strip=True)
            if len(t) > 500:
                break
            text = t
            if find_prices(t):
                break
            node = node.parent
        if not text or not is_ps5_digital(text):
            continue
        prices = [p for p in find_prices(text) if p >= 200]  # écarte mensualités et accessoires
        if not prices:
            continue
        full = urljoin(base_url, href)
        p = urlparse(full)
        key = p.netloc + p.path
        title = a.get_text(" ", strip=True)
        if len(title) < 12:
            title = text[:100]
        price = min(prices)
        if key not in items or price < items[key]["price"]:
            items[key] = {"title": title[:100], "price": price, "url": full}
    return items


def in_budget(price):
    return price is not None and PRICE_MIN <= price <= PRICE_MAX


def diff_items(prev, items):
    """Compare avec le passage précédent. Renvoie une liste (type, item, ancien_prix)."""
    events = []
    for key, it in items.items():
        if not in_budget(it["price"]):
            continue
        if key not in prev:
            events.append(("nouvelle", it, None))
        else:
            old = prev[key]["price"]
            if it["price"] <= old - 1:
                events.append(("baisse", it, old))
    return events


def notify_events(label, events):
    lines = []
    for kind, it, old in events[:MAX_LINES_PER_ALERT]:
        tag = "NOUVEAU" if kind == "nouvelle" else "BAISSE"
        extra = f" (avant {old:.2f} €)" if old else ""
        lines.append(f"[{tag}] {it['title'][:70]} : {it['price']:.2f} €{extra}")
    if len(events) > MAX_LINES_PER_ALERT:
        lines.append(f"... et {len(events) - MAX_LINES_PER_ALERT} autre(s)")
    notify(f"PS5 Digital : {len(events)} nouveauté(s) chez {label}", "\n".join(lines), url=events[0][1]["url"])


def process_list(state, key, label, items):
    """Gère l'état d'une page liste. Renvoie True si l'extraction a servi."""
    entry = state.get(key, {})
    prev = entry.get("items")
    if not items:
        state[key] = {**entry, "fails": entry.get("fails", 0) + 1}
        log(f"{label}: aucune offre PS5 Digital repérée (page vide ou chargée en JavaScript)")
        return False
    if prev is None:
        log(f"{label}: {len(items)} offre(s) mémorisée(s) (première passe, pas d'alerte)")
    else:
        events = diff_items(prev, items)
        log(f"{label}: {len(items)} offre(s), {len(events)} nouveauté(s)")
        if events:
            notify_events(label, events)
    state[key] = {"items": items, "fails": 0}
    return True


# ----------------------------------------------------------------------------
# GOOGLE SHOPPING (SerpApi, optionnel)
# ----------------------------------------------------------------------------

def run_google_shopping(state):
    if not SERPAPI_KEY:
        return
    entry = state.get("google-shopping", {})
    if time.time() - entry.get("ts", 0) < SERPAPI_EVERY_MIN * 60:
        return
    try:
        r = requests.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google_shopping", "q": SERPAPI_QUERY, "gl": "fr",
                "hl": "fr", "google_domain": "google.fr", "api_key": SERPAPI_KEY,
            },
            timeout=40,
        )
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log(f"Google Shopping: échec ({e})")
        return
    if "error" in data:
        log(f"Google Shopping: {data['error']}")
        return
    items = {}
    for it in data.get("shopping_results", []):
        title, source = it.get("title", ""), it.get("source", "")
        price = it.get("extracted_price")
        if not title or price is None or not is_ps5_digital(title) or price < 200:
            continue
        key = f"{source.lower()}|{title.lower()[:80]}"
        items[key] = {
            "title": f"{source} - {title}"[:100],
            "price": float(price),
            "url": it.get("product_link") or it.get("link") or "https://www.google.fr/shopping",
        }
    ok = process_list(state, "google-shopping", "Google Shopping", items)
    state["google-shopping"] = {**state.get("google-shopping", {}), "ts": time.time()}
    return ok


# ----------------------------------------------------------------------------
# NOTIFICATIONS
# ----------------------------------------------------------------------------

def notify(title, message, url=None):
    sent = False
    if NTFY_TOPIC:
        payload = {
            "topic": NTFY_TOPIC, "title": title, "message": message,
            "priority": 5, "tags": ["video_game"],
        }
        if url:
            payload["click"] = url
        try:
            r = requests.post(NTFY_SERVER, json=payload, timeout=15)
            sent = r.status_code == 200
            if not sent:
                log(f"ntfy a répondu HTTP {r.status_code}")
        except requests.RequestException as e:
            log(f"Échec ntfy: {e}")
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            text = f"🎮 {title}\n{message}" + (f"\n{url}" if url else "")
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15,
            )
            sent = True
        except requests.RequestException as e:
            log(f"Échec Telegram: {e}")
    if not sent:
        log("Aucune notification envoyée (NTFY_TOPIC manquant ou erreur).")
    return sent


# ----------------------------------------------------------------------------
# ÉTAT / BOUCLE
# ----------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))


def handle_failure(state, key, prev, store, error):
    fails = prev.get("fails", 0) + 1
    state[key] = {**prev, "fails": fails}
    log(f"{store}: {error}")
    if fails == 10:
        notify(
            f"Bot PS5 : {store} en erreur",
            f"10 échecs consécutifs ({error}). Page changée ou site qui bloque le bot.",
        )


def handle_product(session, state, product):
    key, store = product["url"], product["store"]
    prev = state.get(key, {})
    html, error = fetch(session, key)
    if error:
        handle_failure(state, key, prev, store, error)
        return
    available, price, method = analyse_page(html)
    in_range = price is None or in_budget(price)
    price_txt = f"{price:.2f} €" if price else "prix inconnu"
    status = "DISPO" if available else "indispo" if available is False else "?"
    log(f"{store}: {status} | {price_txt} | {method}")

    was_alerted = prev.get("alerted", False)
    old_price = prev.get("last_price")
    if available and in_range and not was_alerted:
        warn = "" if price else "\n(Prix non détecté : vérifie-le avant d'acheter.)"
        notify(f"PS5 Digital dispo chez {store} !", f"Prix : {price_txt}{warn}", url=key)
        alerted = True
    elif available and in_range and was_alerted and price and old_price and price <= old_price - 1:
        notify(f"Baisse de prix chez {store}", f"{old_price:.2f} € -> {price:.2f} €", url=key)
        alerted = True
    elif available:
        alerted = was_alerted or in_range
    else:
        alerted = False  # re-arme l'alerte pour le prochain restock

    state[key] = {"alerted": alerted, "fails": 0, "last_price": price or old_price}


def handle_list(session, state, product):
    key, store = "liste::" + product["url"], product["store"]
    prev = state.get(key, {})
    html, error = fetch(session, product["url"])
    if error:
        handle_failure(state, key, prev, store, error)
        return
    process_list(state, key, store, extract_items(html, product["url"]))


def run_once(session, state, products):
    for product in products:
        try:
            if product["kind"] == "liste":
                handle_list(session, state, product)
            else:
                handle_product(session, state, product)
        except Exception as e:  # un site cassé ne doit pas bloquer les autres
            log(f"{product['store']}: erreur inattendue ({e})")
        time.sleep(random.uniform(2, 6))
    try:
        run_google_shopping(state)
    except Exception as e:
        log(f"Google Shopping: erreur inattendue ({e})")
    save_state(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="un seul passage")
    ap.add_argument("--test", action="store_true", help="notification de test")
    args = ap.parse_args()

    if args.test:
        ok = notify("Test bot PS5", "Si tu lis ça, les notifications fonctionnent ✅")
        sys.exit(0 if ok else 1)

    products = load_products()
    if not products and not SERPAPI_KEY:
        sys.exit("Aucune adresse dans urls.txt : ajoute au moins une ligne (sans le # devant).")

    log(f"Surveillance de {len(products)} page(s) | budget {PRICE_MIN}-{PRICE_MAX} €"
        + (" | Google Shopping activé" if SERPAPI_KEY else ""))
    state = load_state()
    session = requests.Session()

    if args.once:
        run_once(session, state, products)
        return
    while True:
        try:
            run_once(session, state, load_products())
        except Exception as e:
            log(f"Erreur inattendue: {e}")
        time.sleep(CHECK_INTERVAL + random.uniform(0, JITTER))


if __name__ == "__main__":
    main()
