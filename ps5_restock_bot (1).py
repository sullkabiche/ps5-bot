#!/usr/bin/env python3
"""
Bot d'alerte PS5 Digital (France) - version GitHub Actions
- Lit les pages à surveiller dans urls.txt
- Alerte via ntfy si la PS5 est dispo ET que le prix est dans la fourchette
- Une seule alerte par restock (mémoire dans ps5_state.json)
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

import requests
from bs4 import BeautifulSoup

PRICE_TARGET = 599
PRICE_TOLERANCE = 100
PRICE_MIN = PRICE_TARGET - PRICE_TOLERANCE
PRICE_MAX = PRICE_TARGET + PRICE_TOLERANCE

CHECK_INTERVAL = 180  # utilisé seulement en mode boucle locale
JITTER = 30

BASE = Path(__file__).parent
URLS_FILE = BASE / "urls.txt"
STATE_FILE = BASE / "ps5_state.json"

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

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


def log(msg):
    print(f"[{datetime.now():%d/%m %H:%M:%S}] {msg}", flush=True)


def load_products():
    products = []
    if not URLS_FILE.exists():
        return products
    for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            store, url = [p.strip() for p in line.split("|", 1)]
        else:
            store, url = "Site", line
        if url.startswith("http"):
            products.append({"store": store, "url": url})
    return products


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


def check_product(session, product):
    try:
        r = session.get(product["url"], headers=HEADERS, timeout=20)
    except requests.RequestException as e:
        return {"ok": False, "error": f"réseau: {e}"}
    if r.status_code in (403, 429, 503):
        return {"ok": False, "error": f"bloqué (HTTP {r.status_code})"}
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    available, price, method = analyse_page(r.text)
    return {"ok": True, "available": available, "price": price, "method": method}


def notify(title, message, url=None):
    sent = False
    if NTFY_TOPIC:
        payload = {
            "topic": NTFY_TOPIC,
            "title": title,
            "message": message,
            "priority": 5,
            "tags": ["video_game"],
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
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
                timeout=15,
            )
            sent = True
        except requests.RequestException as e:
            log(f"Échec Telegram: {e}")
    if not sent:
        log("Aucune notification envoyée (NTFY_TOPIC manquant ou erreur).")
    return sent


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def run_once(session, state, products):
    for product in products:
        key = product["url"]
        res = check_product(session, product)
        prev = state.get(key, {})

        if not res["ok"]:
            fails = prev.get("fails", 0) + 1
            state[key] = {**prev, "fails": fails}
            log(f"{product['store']}: {res['error']}")
            if fails == 10:
                notify(
                    f"Bot PS5 : {product['store']} en erreur",
                    f"10 échecs consécutifs ({res['error']}). Page changée ou site qui bloque le bot.",
                )
            time.sleep(random.uniform(2, 5))
            continue

        available, price = res["available"], res["price"]
        in_range = price is None or PRICE_MIN <= price <= PRICE_MAX
        price_txt = f"{price:.2f} €" if price else "prix inconnu"
        status = "DISPO" if available else "indispo" if available is False else "?"
        log(f"{product['store']}: {status} | {price_txt} | {res['method']}")

        was_alerted = prev.get("alerted", False)
        if available and in_range and not was_alerted:
            warn = "" if price else "\n(Prix non détecté : vérifie-le avant d'acheter.)"
            notify(
                f"PS5 Digital dispo chez {product['store']} !",
                f"Prix : {price_txt}{warn}",
                url=product["url"],
            )
            alerted = True
        elif available:
            alerted = was_alerted or in_range
        else:
            alerted = False

        state[key] = {"alerted": alerted, "fails": 0, "last_price": price}
        time.sleep(random.uniform(2, 6))
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
    if not products:
        sys.exit("Aucune adresse dans urls.txt : ajoute au moins une ligne (sans le # devant).")

    log(f"Surveillance de {len(products)} page(s) | budget {PRICE_MIN}-{PRICE_MAX} €")
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
