#!/usr/bin/env python3
import time
import json
import subprocess
import requests
import random
import argparse
import threading
import re
import os
import atexit
import sys
import traceback
from pathlib import Path
from typing import Dict, Set, Tuple, Optional, Any
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from urllib.parse import quote
from html import unescape

# ---------------------------
# CONFIG
# ---------------------------
STEAM_ID64 = "76561199300997500"

GAMES = {
    2923300: {"name": "Banana", "context_id": 2},
    3419430: {"name": "Bongo Cat", "context_id": 2},
}

CURRENCY = 3         # 3 = EUR
LANGUAGE = "english"
POLL_SECONDS = 25 * 60

BASE_DIR = Path(__file__).resolve().parent
COOKIES_FILE = str(BASE_DIR / "cookies.txt")
SETTINGS_FILE = BASE_DIR / "settings.json"
STATE_FILE = BASE_DIR / "inventory_state.json"

# ---------------------------
# HTTP session (Market only)
# ---------------------------
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
})

MARKET_MIN_DELAY = 3.5   # secondes entre 2 requêtes Market
MARKET_JITTER = 1.5      # petite variation aléatoire
MARKET_MAX_RETRIES = 6

_last_market_call = 0.0
_pw = None
_browser = None
_page = None

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")


def get_steamid64() -> str:
    settings = load_settings()
    return str(settings.get("steamid64") or STEAM_ID64)


def _interrupted() -> bool:
    return STOP_EVENT.is_set() or UPDATE_EVENT.is_set()


def _sleep_interruptible(seconds: float, step: float = 0.2) -> None:
    remaining = max(0.0, float(seconds))
    while remaining > 0 and not _interrupted():
        chunk = min(step, remaining)
        time.sleep(chunk)
        remaining -= chunk

def _sleep_for_rate_limit():
    global _last_market_call
    now = time.time()
    wait = MARKET_MIN_DELAY - (now - _last_market_call)
    if wait > 0:
        _sleep_interruptible(wait + random.random() * MARKET_JITTER)
    _last_market_call = time.time()


def _get_playwright_page():
    global _pw, _browser, _page
    if _page is not None:
        return _page
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError(
            "Playwright est requis pour l'analyse HTML. "
            "Installe-le avec: pip install playwright && playwright install"
        ) from e
    _pw = sync_playwright().start()
    _browser = _pw.chromium.launch(headless=True)
    _page = _browser.new_page()
    return _page


@atexit.register
def _close_playwright():
    global _pw, _browser, _page
    try:
        if _page is not None:
            _page.close()
        if _browser is not None:
            _browser.close()
        if _pw is not None:
            _pw.stop()
    except Exception:
        pass


def _extract_steamid64(html: str) -> Optional[str]:
    m = re.search(r'"steamid"\s*:\s*"(\d{17})"', html)
    if m:
        return m.group(1)
    m = re.search(r"g_steamID\s*=\s*\"(\d{17})\"", html)
    if m:
        return m.group(1)
    return None


def _cookies_to_netscape(cookies: list) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = c.get("domain", "")
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expires") or 0)
        name = c.get("name", "")
        value = c.get("value", "")
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        lines.append("\t".join([domain, include_subdomains, path, secure, str(expires), name, value]))
    return "\n".join(lines) + "\n"


def _wait_for_steamid(page, timeout_s: int = 180) -> Optional[str]:
    start = time.time()
    while time.time() - start < timeout_s and not _interrupted():
        try:
            html = page.content()
            steamid64 = _extract_steamid64(html)
            if steamid64:
                return steamid64
        except Exception:
            pass
        time.sleep(1.0)
    return None


def _wait_for_login_complete(page, timeout_s: int = 180) -> Optional[str]:
    start = time.time()
    asked_guard = False
    while time.time() - start < timeout_s and not _interrupted():
        try:
            html = page.content()
            steamid64 = _extract_steamid64(html)
            if steamid64:
                return steamid64
        except Exception:
            pass

        # Detect Steam Guard input (email/app code) and prompt once.
        try:
            guard_selectors = [
                "input[name='steamguardcode']",
                "input[name='authcode']",
                "input[name='emailauth']",
            ]
            for sel in guard_selectors:
                loc = page.locator(sel)
                if loc.count() > 0 and not asked_guard:
                    code = input("Steam Guard code (laisser vide si validation via appli): ").strip()
                    if code:
                        loc.first.fill(code)
                        page.locator("button[type='submit'], button:has-text('Submit'), button:has-text('Valider')").first.click()
                    asked_guard = True
                    break
        except Exception:
            pass

        time.sleep(1.0)
    return None


def _human_click(locator, page) -> bool:
    try:
        locator.first.hover()
        box = locator.first.bounding_box()
        if box:
            x = box["x"] + box["width"] * 0.5
            y = box["y"] + box["height"] * 0.5
            page.mouse.move(x, y, steps=12)
            page.mouse.down()
            time.sleep(0.08)
            page.mouse.up()
            return True
    except Exception:
        pass
    return False


def login_and_save_cookies() -> str:
    try:
        from getpass import getpass
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError(
            "Playwright est requis pour la connexion Steam. "
            "Installe-le avec: pip install playwright && playwright install"
        ) from e

    username = (os.environ.get("STEAM_USERNAME") or "").strip() or input("Steam username: ").strip()
    if not username:
        raise RuntimeError("Username Steam vide.")
    password = (os.environ.get("STEAM_PASSWORD") or "").strip() or getpass("Steam password: ")
    if not password:
        raise RuntimeError("Mot de passe Steam vide.")

    headless = os.environ.get("STEAM_HEADLESS", "").strip() != "0"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        print("[login] Open login page...")
        page.goto("https://steamcommunity.com/login/home/?goto=%2Fmy%2F", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        def fill_in_frame(frame) -> bool:
            try:
                print(f"[login] frame url: {frame.url}")
                # Try label-based targeting (localised UI on community login).
                try:
                    label_user = frame.get_by_label("Se connecter avec un nom de compte")
                    label_pass = frame.get_by_label("Mot de passe")
                    if label_user.count() > 0 and label_pass.count() > 0:
                        print("[login] label fields found")
                        label_user.first.fill(username)
                        label_pass.first.fill(password)
                        submit_label = frame.get_by_role("button", name="Se connecter")
                        if submit_label.count() > 0:
                            print("[login] clicking submit by label")
                            submit_label.first.click()
                            try:
                                frame.page.wait_for_load_state("domcontentloaded", timeout=15000)
                            except Exception:
                                pass
                        return True
                except Exception:
                    pass

                user_locator = frame.locator(
                    "input[name='username'], input[type='text'][autocomplete='username'], input[name='login'], "
                    "input#input_username, input[name='accountname'], input._2GBWeup5cttgbTw8FM3tfx[type='text']"
                )
                pass_locator = frame.locator(
                    "input[type='password'], input[name='password'], input#input_password, "
                    "input._2GBWeup5cttgbTw8FM3tfx[type='password']"
                )
                print(f"[login] user inputs: {user_locator.count()} pass inputs: {pass_locator.count()}")
                if user_locator.count() == 0 or pass_locator.count() == 0:
                    return False
                user_locator.first.wait_for(state="visible", timeout=8000)
                pass_locator.first.wait_for(state="visible", timeout=8000)
                try:
                    print("[login] fill username via fill()")
                    user_locator.first.fill(username)
                except Exception:
                    print("[login] fill username via type()")
                    user_locator.first.click()
                    user_locator.first.type(username, delay=20)
                try:
                    print("[login] fill password via fill()")
                    pass_locator.first.fill(password)
                except Exception:
                    print("[login] fill password via type()")
                    pass_locator.first.click()
                    pass_locator.first.type(password, delay=20)

                # Ensure values are set in case of custom JS handlers.
                frame.evaluate(
                    """(args) => {
                        const userSel = args.userSel;
                        const passSel = args.passSel;
                        const u = args.u;
                        const p = args.p;
                        const setVal = (sel, v) => {
                            const el = document.querySelector(sel);
                            if (!el) return;
                            el.value = v;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        };
                        setVal(userSel, u);
                        setVal(passSel, p);
                    }""",
                    {
                        "userSel": "input[name='username'], input[type='text'][autocomplete='username'], input[name='login'], input#input_username, input[name='accountname'], input._2GBWeup5cttgbTw8FM3tfx[type='text']",
                        "passSel": "input[type='password'], input[name='password'], input#input_password, input._2GBWeup5cttgbTw8FM3tfx[type='password']",
                        "u": username,
                        "p": password,
                    },
                )
                submit = frame.locator(
                    "button[type='submit'], button:has-text('Sign In'), button:has-text('Connexion'), "
                    "button#login_btn_signin, button.DjSvCZoKKfoNSmarsEcTS, button:has-text('Se connecter')"
                )
                print(f"[login] submit buttons: {submit.count()}")
                if submit.count() > 0:
                    try:
                        print("[login] clicking submit")
                        submit.first.click()
                    except Exception:
                        try:
                            print("[login] clicking submit force")
                            submit.first.click(force=True)
                        except Exception:
                            try:
                                print("[login] clicking submit via JS")
                                frame.evaluate(
                                    """(btnSel) => {
                                        const btn = document.querySelector(btnSel);
                                        if (btn) btn.click();
                                    }""",
                                    "button.DjSvCZoKKfoNSmarsEcTS",
                                )
                            except Exception:
                                pass
                    page_obj = frame.page if hasattr(frame, "page") else frame
                    if _human_click(submit, page_obj):
                        print("[login] human click ok")
                    else:
                        print("[login] human click failed")
                    try:
                        page_obj.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                else:
                    # Fallback: submit via Enter on password field.
                    try:
                        print("[login] submit via Enter")
                        pass_locator.first.press("Enter")
                        page_obj = frame.page if hasattr(frame, "page") else frame
                        page_obj.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                # Last resort: submit the nearest form via JS.
                try:
                    print("[login] submit form via JS")
                    frame.evaluate(
                        """(args) => {
                            const u = document.querySelector(args.userSel);
                            const p = document.querySelector(args.passSel);
                            const form = (u && u.closest('form')) || (p && p.closest('form'));
                            if (form && form.requestSubmit) form.requestSubmit();
                            else if (form) form.submit();
                        }""",
                        {
                            "userSel": "input[name='username'], input[type='text'][autocomplete='username'], input[name='login'], input#input_username, input[name='accountname'], input._2GBWeup5cttgbTw8FM3tfx[type='text']",
                            "passSel": "input[type='password'], input[name='password'], input#input_password, input._2GBWeup5cttgbTw8FM3tfx[type='password']",
                        },
                    )
                    page_obj = frame.page if hasattr(frame, "page") else frame
                    page_obj.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                return True
            except Exception as e:
                print(f"[login] fill_in_frame failed: {e}")
                print(traceback.format_exc())
                return False

        filled = False
        if fill_in_frame(page):
            filled = True
        else:
            for frame in page.frames:
                try:
                    frame.wait_for_timeout(500)
                except Exception:
                    pass
                if fill_in_frame(frame):
                    filled = True
                    break
        if not filled:
            print("[login] Impossible de remplir les champs automatiquement. Essaie en mode UI.")

        # Wait for login / Steam Guard app approval to complete.
        steamid64 = _wait_for_login_complete(page, timeout_s=180)
        if not steamid64:
            print("[login] En attente de validation Steam Guard (appli)...")
            steamid64 = _wait_for_login_complete(page, timeout_s=180)
        if not steamid64:
            page.goto("https://steamcommunity.com/my/", wait_until="domcontentloaded")
            steamid64 = _wait_for_steamid(page, timeout_s=60)
        if not steamid64 and not headless:
            input("Termine la connexion dans la fenetre, puis appuie sur Entree...")
            page.goto("https://steamcommunity.com/my/", wait_until="domcontentloaded")
            steamid64 = _wait_for_steamid(page, timeout_s=120)
        if not steamid64:
            browser.close()
            raise RuntimeError("Impossible de recuperer le SteamID64. Connexion echouee ?")

        cookies = context.cookies()
        cookies_text = _cookies_to_netscape(cookies)
        Path(COOKIES_FILE).write_text(cookies_text, encoding="utf-8")

        browser.close()

    settings = load_settings()
    settings["steamid64"] = steamid64
    save_settings(settings)
    print(f"[login] SteamID64 detecte: {steamid64}")
    print(f"[login] Cookies sauvegardes: {COOKIES_FILE}")
    return steamid64

# ---------------------------
# Helpers
# ---------------------------
def fetch_inventory_with_curl(steamid64: str, appid: int, context_id: int, count: int = 2000) -> dict:
    url = f"https://steamcommunity.com/inventory/{steamid64}/{appid}/{context_id}?l={LANGUAGE}&count={count}"

    cmd = [
        "curl",
        "-sL",
        "--compressed",
        "-b", COOKIES_FILE,
        "-H", "Accept: application/json, text/plain, */*",
        "-H", "Accept-Language: en-US,en;q=0.9,fr;q=0.8",
        "-H", f"Referer: https://steamcommunity.com/profiles/{steamid64}/inventory/",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        url
    ]

    r = subprocess.run(cmd, capture_output=True, text=True)

    if r.returncode != 0:
        raise RuntimeError(f"curl error (code {r.returncode}): {r.stderr.strip()}")

    body = (r.stdout or "").strip()

    # Steam peut renvoyer "null" ou HTML si cookies invalides
    if not body.startswith("{"):
        snippet = body[:600].replace("\n", "\\n")
        raise RuntimeError(
            f"Réponse non-JSON pour appid={appid}. Début:\n{snippet}\n"
            f"-> Vérifie {COOKIES_FILE} (session Steam valide)."
        )

    data = json.loads(body)
    if data.get("success") != 1:
        raise RuntimeError(f"Steam success!=1 pour appid={appid}: {data}")

    return data


def ensure_login_if_needed(force: bool = False) -> str:
    have_cookies = Path(COOKIES_FILE).exists()
    steamid64 = get_steamid64()
    if force or not have_cookies or not steamid64 or steamid64 == "CHANGE_ME":
        return login_and_save_cookies()
    return steamid64


def parse_inventory(inv_json: dict) -> Tuple[Set[str], Dict[str, Dict[str, Any]]]:
    """
    Returns:
      - asset_ids: set of assetid strings
      - asset_meta: mapping assetid -> {"market_hash_name": str, "amount": int}
    """
    assets = inv_json.get("assets", []) or []
    descriptions = inv_json.get("descriptions", []) or []

    # (classid, instanceid) -> description
    desc_map: Dict[Tuple[str, str], dict] = {}
    for d in descriptions:
        classid = str(d.get("classid", ""))
        instanceid = str(d.get("instanceid", "0"))
        if classid:
            desc_map[(classid, instanceid)] = d

    asset_ids: Set[str] = set()
    asset_meta: Dict[str, Dict[str, Any]] = {}

    for a in assets:
        assetid = str(a.get("assetid", ""))
        classid = str(a.get("classid", ""))
        instanceid = str(a.get("instanceid", "0"))
        amount = int(a.get("amount", "1") or 1)

        if not assetid:
            continue

        asset_ids.add(assetid)
        d = desc_map.get((classid, instanceid), {})

        mhn = d.get("market_hash_name") or d.get("market_name") or d.get("name") or f"assetid={assetid}"

        asset_meta[assetid] = {
            "market_hash_name": mhn,
            "amount": amount,
        }

    return asset_ids, asset_meta


def _parse_price_to_cents(text: str) -> Optional[int]:
    if not text:
        return None
    t = unescape(text).strip()
    t = t.replace("or more", "").strip()
    m = re.search(r"(\d[\d.,]*)", t)
    if not m:
        return None
    num = m.group(1)
    if "." in num and "," in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "")
            num = num.replace(",", ".")
        else:
            num = num.replace(",", "")
    elif num.count(",") == 1 and num.count(".") == 0:
        num = num.replace(",", ".")
    else:
        num = num.replace(",", "")
    try:
        return int(round(float(num) * 100))
    except ValueError:
        return None


def _parse_int_from_text(text: str) -> int:
    return int("".join(ch for ch in (text or "") if ch.isdigit()) or "0")


def fetch_listing_html(appid: int, market_hash_name: str) -> str:
    url = f"https://steamcommunity.com/market/listings/{appid}/{quote(market_hash_name)}"
    _sleep_for_rate_limit()
    page = _get_playwright_page()
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(1500)
    return page.content()


def parse_listing_html(html: str) -> dict:
    result = {
        "listings_total": 0,
        "price_levels": [],
        "price_history": [],
    }

    m = re.search(
        r'id="market_commodity_forsale".*?<span[^>]*>([^<]+)</span>.*?starting at.*?<span[^>]*>([^<]+)</span>',
        html,
        re.S,
    )
    if m:
        result["listings_total"] = _parse_int_from_text(m.group(1))

    m = re.search(
        r'id="market_commodity_forsale_table".*?<tbody>(.*?)</tbody>',
        html,
        re.S,
    )
    if m:
        tbody = m.group(1)
        rows = re.findall(
            r"<tr>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>",
            tbody,
            re.S,
        )
        for price_cell, qty_cell in rows:
            price_text = re.sub(r"<[^>]+>", "", price_cell).strip()
            qty_text = re.sub(r"<[^>]+>", "", qty_cell).strip()
            price_cents = _parse_price_to_cents(price_text)
            if price_cents is None:
                continue
            qty = _parse_int_from_text(qty_text)
            result["price_levels"].append({"price_cents": price_cents, "qty": qty})

    m = re.search(r"var\s+line1\s*=\s*(\[\[.*?\]\]);", html, re.S)
    if not m:
        m = re.search(r"line1\s*=\s*(\[\[.*?\]\]);", html, re.S)
    if m:
        try:
            result["price_history"] = json.loads(m.group(1))
        except Exception:
            result["price_history"] = []

    return result


def fetch_price_overview(appid: int, market_hash_name: str, currency: int = 3) -> Optional[int]:
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {"appid": appid, "currency": currency, "market_hash_name": market_hash_name}

    for attempt in range(1, MARKET_MAX_RETRIES + 1):
        if _interrupted():
            return None
        _sleep_for_rate_limit()

        r = session.get(url, params=params, timeout=20)

        # Rate limit
        if r.status_code == 429:
            # backoff exponentiel + jitter
            backoff = min(60, (2 ** attempt)) + random.random() * 2.0
            print(f"  [market] 429 rate-limited. Backoff {backoff:.1f}s (attempt {attempt}/{MARKET_MAX_RETRIES})")
            _sleep_interruptible(backoff)
            continue

        # Autres erreurs HTTP
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            print(f"  [market] HTTP error: {e}")
            return None

        # JSON normal
        try:
            data = r.json()
        except Exception:
            return None

        if not data.get("success"):
            return None

        price_str = data.get("lowest_price") or data.get("median_price")
        if not price_str:
            return None

        cleaned = "".join(ch for ch in price_str if ch.isdigit() or ch in ",.")
        if cleaned.count(",") == 1 and cleaned.count(".") == 0:
            cleaned = cleaned.replace(",", ".")

        try:
            return int(round(float(cleaned) * 100))
        except ValueError:
            return None

    # trop de 429
    print("  [market] Too many 429s, giving up for this item.")
    return None


def load_state() -> dict:
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("events", [])
        state.setdefault("value_history", [])
        state.setdefault("games", {})
        for appid in GAMES:
            state["games"].setdefault(
                str(appid),
                {
                    "known_assetids": [],
                    "total_value_cents": 0,
                    "price_cache": {},
                    "item_counts": {},
                    "inventory_total_cents": 0,
                    "market_analysis": {},
                },
            )
        return state

    return {
        "games": {
            str(appid): {
                "known_assetids": [],
                "total_value_cents": 0,
                "price_cache": {},
                "item_counts": {},
                "inventory_total_cents": 0,
                "market_analysis": {},
            } for appid in GAMES
        },
        "events": [],
        "value_history": [],
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------
# Market analysis
# ---------------------------
SELL_TURNOVER_THRESHOLD = 0.15  # 15% des listings vendus par jour = vendu rapidement
SELL_MIN_DAILY_SALES = 2


def _parse_int(s: str) -> int:
    return int("".join(ch for ch in s if ch.isdigit()) or "0")


def fetch_item_nameid(appid: int, market_hash_name: str) -> Optional[int]:
    url = f"https://steamcommunity.com/market/listings/{appid}/{quote(market_hash_name)}"
    if _interrupted():
        return None
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
    except Exception:
        return None

    m = re.search(r"Market_LoadOrderSpread\(\s*\d+\s*,\s*(\d+)", r.text)
    if m:
        return int(m.group(1))
    m = re.search(r"item_nameid\"\s*:\s*(\d+)", r.text)
    if m:
        return int(m.group(1))
    return None


def fetch_orders_histogram(item_nameid: int, currency: int = 3) -> Optional[dict]:
    url = "https://steamcommunity.com/market/itemordershistogram"
    params = {
        "country": "US",
        "language": LANGUAGE,
        "currency": currency,
        "item_nameid": str(item_nameid),
        "two_factor": 0,
    }
    if _interrupted():
        return None
    try:
        r = session.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            return None
        return data
    except Exception:
        return None


def fetch_price_history(appid: int, market_hash_name: str, currency: int = 3) -> Optional[dict]:
    url = "https://steamcommunity.com/market/pricehistory/"
    params = {"appid": appid, "currency": currency, "market_hash_name": market_hash_name}
    if _interrupted():
        return None
    try:
        r = session.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            return None
        return data
    except Exception:
        return None


def analyze_item_market(appid: int, market_hash_name: str) -> dict:
    if _interrupted():
        return {"status": "skipped", "reason": "interrupted", "decision": "hold"}
    html = fetch_listing_html(appid, market_hash_name)
    listing = parse_listing_html(html)

    price_levels = listing.get("price_levels", []) or []
    total_listings = 0
    for level in price_levels:
        total_listings += int(level.get("qty", 0) or 0)

    if total_listings <= 0:
        total_listings = int(listing.get("listings_total", 0) or 0)

    prices = listing.get("price_history", []) or []
    if not prices and not price_levels and total_listings <= 0:
        return {"status": "skipped", "reason": "no_listings_or_no_history", "decision": "hold"}

    recent = prices[-7:] if len(prices) >= 7 else prices
    daily_sales = []
    for p in recent:
        if len(p) < 3:
            continue
        vol = _parse_int(str(p[2]))
        daily_sales.append(vol)
    avg_daily_sales = 0.0 if not daily_sales else sum(daily_sales) / len(daily_sales)

    turnover = 0.0 if total_listings <= 0 else avg_daily_sales / total_listings

    recommended_price_cents = 0
    if price_levels:
        if avg_daily_sales > 0:
            cumulative = 0
            for level in price_levels:
                cumulative += level["qty"]
                if cumulative >= avg_daily_sales:
                    recommended_price_cents = level["price_cents"]
                    break
        if recommended_price_cents == 0:
            recommended_price_cents = price_levels[0]["price_cents"]

    decision = "hold"
    if avg_daily_sales >= SELL_MIN_DAILY_SALES and turnover >= SELL_TURNOVER_THRESHOLD:
        decision = "sell"

    return {
        "status": "ok",
        "listings_total": total_listings,
        "avg_daily_sales": avg_daily_sales,
        "turnover": turnover,
        "recommended_price_cents": recommended_price_cents,
        "decision": decision,
    }


# ---------------------------
# Web server
# ---------------------------
STOP_EVENT = threading.Event()
HTTPD = None
UPDATE_EVENT = threading.Event()


def run_git_pull() -> bool:
    try:
        r = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            print(f"[update] git pull failed: {r.stderr.strip()}")
            return False
        out = (r.stdout or "").strip()
        print(f"[update] git pull ok: {out}")
        return True
    except Exception as e:
        print(f"[update] git pull error: {e}")
        return False


def restart_self() -> None:
    try:
        print("[update] restarting...")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        print(f"[update] restart failed: {e}")


def build_payload(state: dict) -> dict:
    games = state.get("games", {}) or {}
    events = state.get("events", []) or []
    value_history = state.get("value_history", []) or []

    events_sorted = sorted(events, key=lambda e: int(e.get("ts", 0)))
    last_items = events_sorted[-10:]

    expensive_items = []
    items_to_sell = []
    for appid_str, gstate in games.items():
        try:
            appid = int(appid_str)
        except ValueError:
            appid = appid_str
        price_cache = gstate.get("price_cache", {}) or {}
        item_counts = gstate.get("item_counts", {}) or {}
        market_analysis = gstate.get("market_analysis", {}) or {}
        for mhn, count in item_counts.items():
            unit_price = int(price_cache.get(mhn, 0) or 0)
            total_cents = unit_price * int(count or 0)
            expensive_items.append({
                "appid": appid,
                "game": GAMES.get(appid, {}).get("name", str(appid)),
                "market_hash_name": mhn,
                "count": int(count or 0),
                "unit_price_cents": unit_price,
                "total_cents": total_cents,
            })
            analysis = market_analysis.get(mhn)
            if analysis and analysis.get("decision") == "sell":
                items_to_sell.append({
                    "appid": appid,
                    "game": GAMES.get(appid, {}).get("name", str(appid)),
                    "market_hash_name": mhn,
                    "count": int(count or 0),
                    "recommended_price_cents": int(analysis.get("recommended_price_cents", 0) or 0),
                    "listings_total": int(analysis.get("listings_total", 0) or 0),
                    "avg_daily_sales": float(analysis.get("avg_daily_sales", 0) or 0),
                    "turnover": float(analysis.get("turnover", 0) or 0),
                })

    expensive_items.sort(key=lambda x: x.get("unit_price_cents", 0), reverse=True)
    expensive_items = expensive_items[:10]
    items_to_sell.sort(key=lambda x: x.get("turnover", 0), reverse=True)

    return {
        "last_items": last_items,
        "value_history": value_history[-2000:],
        "expensive_items": expensive_items,
        "items_to_sell": items_to_sell,
    }


INDEX_HTML = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Steam Surveillance</title>
  <style>
    :root {
      --bg: #f6f1e7;
      --ink: #231f20;
      --accent: #f26b38;
      --muted: #6f5f54;
      --card: #fff7ec;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Libre Baskerville", "Georgia", serif;
      background: radial-gradient(circle at 20% 10%, #fbead5, transparent 55%),
                  radial-gradient(circle at 80% 0%, #f7d1c6, transparent 45%),
                  var(--bg);
      color: var(--ink);
    }
    header {
      padding: 24px 20px 8px;
    }
    h1 {
      margin: 0 0 8px;
      font-size: 28px;
      letter-spacing: 0.5px;
    }
    .subtitle { color: var(--muted); font-size: 14px; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 16px;
      padding: 16px 20px 28px;
    }
    .card {
      background: var(--card);
      border: 1px solid #e9d7c6;
      border-radius: 16px;
      padding: 16px;
      box-shadow: 0 6px 18px rgba(0,0,0,0.08);
    }
    .card h2 {
      margin: 0 0 10px;
      font-size: 18px;
    }
    ul { list-style: none; padding: 0; margin: 0; }
    li { margin: 8px 0; font-size: 14px; }
    .price { color: var(--accent); font-weight: 700; }
    .chart {
      width: 100%;
      height: 260px;
      border: 1px dashed #e4c9b8;
      border-radius: 12px;
      background: #fffaf2;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      text-align: left;
      padding: 6px 4px;
      border-bottom: 1px solid #ead9c6;
    }
    .muted { color: var(--muted); font-size: 12px; }
  </style>
</head>
<body>
  <header>
    <h1>Steam Surveillance</h1>
    <div class="subtitle">Derniere mise a jour: <span id="lastUpdate">-</span></div>
    <button id="stopBtn" style="margin-top:8px;padding:6px 10px;border:1px solid #e4c9b8;border-radius:10px;background:#fff3e2;cursor:pointer;">
      Arreter le script
    </button>
    <button id="updateBtn" style="margin-top:8px;margin-left:8px;padding:6px 10px;border:1px solid #e4c9b8;border-radius:10px;background:#ffe6cf;cursor:pointer;">
      Mettre a jour
    </button>
  </header>
  <div class="grid">
    <section class="card">
      <h2>10 derniers nouveaux items</h2>
      <ul id="lastItems"></ul>
    </section>
    <section class="card">
      <h2>Valeur totale du compte</h2>
      <div id="chart" class="chart"></div>
      <div class="muted">Graphique base sur value_history</div>
    </section>
    <section class="card">
      <h2>Items les plus chers</h2>
      <table>
        <thead>
          <tr><th>Item</th><th>Jeu</th><th>Prix</th><th>Qt</th><th>Total</th></tr>
        </thead>
        <tbody id="expensiveItems"></tbody>
      </table>
    </section>
    <section class="card">
      <h2>A vendre (selon le modele)</h2>
      <table>
        <thead>
          <tr><th>Item</th><th>Jeu</th><th>Prix suggere</th><th>Ventes/j</th><th>Listings</th><th>Turnover</th></tr>
        </thead>
        <tbody id="sellItems"></tbody>
      </table>
    </section>
  </div>
<script>
  function euro(cents) {
    return (cents / 100).toFixed(2) + "€";
  }

  function renderChart(points) {
    const el = document.getElementById("chart");
    const width = el.clientWidth || 600;
    const height = el.clientHeight || 240;
    if (!points.length) {
      el.innerHTML = "<div class=\\"muted\\" style=\\"padding:12px\\">Pas de donnees</div>";
      return;
    }
    const values = points.map(p => p.total_cents || 0);
    let min = Math.min(...values);
    let max = Math.max(...values);
    if (min === max) max = min + 1;
    const pad = 20;
    const scaleX = (i) => pad + (i * (width - pad * 2) / Math.max(points.length - 1, 1));
    const scaleY = (v) => height - pad - ((v - min) * (height - pad * 2) / (max - min));
    const d = points.map((p, i) => `${scaleX(i)},${scaleY(p.total_cents || 0)}`).join(" ");
    const svg = `
      <svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg">
        <polyline fill="none" stroke="#f26b38" stroke-width="3" points="${d}" />
        <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${height - pad}" stroke="#e0c6b5" />
        <line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" stroke="#e0c6b5" />
        <text x="${pad}" y="${pad - 6}" font-size="11" fill="#6f5f54">${euro(max)}</text>
        <text x="${pad}" y="${height - 6}" font-size="11" fill="#6f5f54">${euro(min)}</text>
      </svg>
    `;
    el.innerHTML = svg;
  }

  let lastSeenTs = 0;
  let hasRendered = false;

  async function refresh() {
    const r = await fetch("/data", { cache: "no-store" });
    const data = await r.json();

    const items = data.last_items || [];
    const history = data.value_history || [];
    const maxItemTs = items.reduce((m, it) => Math.max(m, it.ts || 0), 0);
    const lastHistoryTs = history.length ? (history[history.length - 1].ts || 0) : 0;
    const maxTs = Math.max(maxItemTs, lastHistoryTs);
    if (hasRendered && maxTs <= lastSeenTs) return;
    lastSeenTs = maxTs;

    const lastItems = document.getElementById("lastItems");
    lastItems.innerHTML = "";
    items.slice().reverse().forEach(item => {
      const li = document.createElement("li");
      const ts = new Date((item.ts || 0) * 1000).toLocaleString();
      li.innerHTML = `<span class="price">${euro(item.add_cents || 0)}</span> ${item.market_hash_name || ""} x${item.amount || 1} <span class="muted">(${item.game || item.appid || ""}, ${ts})</span>`;
      lastItems.appendChild(li);
    });

    const tbody = document.getElementById("expensiveItems");
    tbody.innerHTML = "";
    (data.expensive_items || []).forEach(item => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${item.market_hash_name || ""}</td>
        <td>${item.game || item.appid || ""}</td>
        <td>${euro(item.unit_price_cents || 0)}</td>
        <td>${item.count || 0}</td>
        <td>${euro(item.total_cents || 0)}</td>
      `;
      tbody.appendChild(tr);
    });

    renderChart(history);
    document.getElementById("lastUpdate").textContent = new Date().toLocaleString();

    const sellBody = document.getElementById("sellItems");
    sellBody.innerHTML = "";
    (data.items_to_sell || []).forEach(item => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${item.market_hash_name || ""}</td>
        <td>${item.game || item.appid || ""}</td>
        <td>${euro(item.recommended_price_cents || 0)}</td>
        <td>${(item.avg_daily_sales || 0).toFixed(2)}</td>
        <td>${item.listings_total || 0}</td>
        <td>${(item.turnover || 0).toFixed(3)}</td>
      `;
      sellBody.appendChild(tr);
    });
    hasRendered = true;
  }

  refresh();
  setInterval(refresh, 15000);

  document.getElementById("stopBtn").addEventListener("click", async () => {
    if (!confirm("Arreter le script maintenant ?")) return;
    await fetch("/stop", { method: "POST" });
  });

  document.getElementById("updateBtn").addEventListener("click", async () => {
    if (!confirm("Mettre a jour via GitHub et redemarrer ensuite ?")) return;
    await fetch("/update", { method: "POST" });
    alert("Mise a jour lancee. Le script va redemarrer.");
  });
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/data":
            state = load_state()
            payload = build_payload(state)
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/stop":
            body = b"stopping"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            STOP_EVENT.set()
            if HTTPD is not None:
                threading.Thread(target=HTTPD.shutdown, daemon=True).start()
            # Force process exit shortly after responding, like killing the task.
            threading.Thread(target=lambda: (time.sleep(0.4), os._exit(0)), daemon=True).start()
            return
        if path == "/update":
            body = b"updating"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            UPDATE_EVENT.set()
            STOP_EVENT.set()
            if HTTPD is not None:
                threading.Thread(target=HTTPD.shutdown, daemon=True).start()
            return
        self.send_response(404)
        self.end_headers()


def serve_web() -> None:
    global HTTPD
    HTTPD = HTTPServer(("0.0.0.0", 8181), Handler)
    print("Serving on http://0.0.0.0:8181")
    HTTPD.serve_forever()


# ---------------------------
# Main loop
# ---------------------------
def main(steamid64: str):
    state = load_state()

    state.setdefault("games", {})
    for appid in GAMES:
        state["games"].setdefault(str(appid), {"known_assetids": [], "total_value_cents": 0, "price_cache": {}})

    print("Monitoring inventories… (Ctrl+C to stop)")
    print("Games:", ", ".join([f"{appid} ({GAMES[appid]['name']})" for appid in GAMES]))

    while not STOP_EVENT.is_set():
        if UPDATE_EVENT.is_set():
            break
        cycle_total_cents = 0
        inventory_changed = False
        for appid, cfg in GAMES.items():
            if STOP_EVENT.is_set():
                break
            name = cfg["name"]
            context_id = cfg["context_id"]

            gstate = state["games"][str(appid)]
            known_assetids: Set[str] = set(gstate.get("known_assetids", []))
            total_value_cents: int = int(gstate.get("total_value_cents", 0))
            price_cache: Dict[str, int] = {k: int(v) for k, v in (gstate.get("price_cache", {}) or {}).items()}
            item_counts: Dict[str, int] = {k: int(v) for k, v in (gstate.get("item_counts", {}) or {}).items()}
            market_analysis: Dict[str, dict] = {k: v for k, v in (gstate.get("market_analysis", {}) or {}).items()}

            try:
                inv = fetch_inventory_with_curl(steamid64, appid, context_id)
                asset_ids, asset_meta = parse_inventory(inv)

                item_counts = {}
                for meta in asset_meta.values():
                    mhn = meta.get("market_hash_name", "")
                    if not mhn:
                        continue
                    amount = int(meta.get("amount", 1))
                    item_counts[mhn] = item_counts.get(mhn, 0) + amount

                new_assets = asset_ids - known_assetids
                removed_assets = known_assetids - asset_ids

                if new_assets:
                    print(f"\n[{name}] + {len(new_assets)} new item(s) detected")
                    inventory_changed = True

                    for assetid in sorted(new_assets):
                        meta = asset_meta.get(assetid, {})
                        mhn = meta.get("market_hash_name", f"assetid={assetid}")
                        amount = int(meta.get("amount", 1))

                        # Always check market, even if inventory says not marketable
                        if mhn in price_cache:
                            unit_price_cents = price_cache[mhn]
                        else:
                            unit_price_cents = fetch_price_overview(appid, mhn, CURRENCY) or 0
                            price_cache[mhn] = unit_price_cents
                        add_cents = unit_price_cents * amount
                        total_value_cents += add_cents

                        print(f"  - {mhn} x{amount} : +{add_cents/100:.2f}€ (unit {unit_price_cents/100:.2f}€)")
                        state["events"].append({
                            "ts": int(time.time()),
                            "appid": appid,
                            "game": name,
                            "market_hash_name": mhn,
                            "amount": amount,
                            "unit_price_cents": unit_price_cents,
                            "add_cents": add_cents,
                        })

                    print(f"[{name}] Total value added so far: {total_value_cents/100:.2f}€")

                if removed_assets:
                    print(f"[{name}] - {len(removed_assets)} item(s) removed (ignored for total)")
                    inventory_changed = True

                inventory_total_cents = 0
                for mhn, count in item_counts.items():
                    if mhn not in price_cache:
                        price_cache[mhn] = fetch_price_overview(appid, mhn, CURRENCY) or 0
                    inventory_total_cents += price_cache.get(mhn, 0) * count

                for mhn in item_counts.keys():
                    if mhn in market_analysis:
                        continue
                    unit_price_cents = price_cache.get(mhn, 0) or 0
                    if unit_price_cents <= 0:
                        market_analysis[mhn] = {
                            "status": "skipped",
                            "reason": "price_zero",
                            "decision": "hold",
                        }
                        continue
                    print(f"[{name}] analyzing market for {mhn}")
                    market_analysis[mhn] = analyze_item_market(appid, mhn)

                gstate["known_assetids"] = sorted(list(asset_ids))
                gstate["total_value_cents"] = total_value_cents
                gstate["price_cache"] = price_cache
                gstate["item_counts"] = item_counts
                gstate["inventory_total_cents"] = inventory_total_cents
                gstate["market_analysis"] = market_analysis
                cycle_total_cents += inventory_total_cents

            except requests.HTTPError as e:
                print(f"[{name}] HTTP error: {e}")
            except Exception as e:
                print(f"[{name}] Error: {e}")

        state["events"] = state.get("events", [])[-10000:]
        # Always log the total value so the chart reflects current account value.
        state["value_history"] = state.get("value_history", [])[-10000:]
        state["value_history"].append({"ts": int(time.time()), "total_cents": cycle_total_cents})
        state["value_history"] = state["value_history"][-10000:]
        save_state(state)
        _sleep_interruptible(POLL_SECONDS, step=0.5)

    if UPDATE_EVENT.is_set():
        run_git_pull()
        restart_self()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", action="store_true", help="Run the web server on port 8181")
    parser.add_argument("--monitor", action="store_true", help="Run the inventory monitor loop")
    parser.add_argument("--login", action="store_true", help="Force Steam login and refresh cookies.txt")
    args = parser.parse_args()

    steamid64 = ensure_login_if_needed(force=args.login)

    if args.server and args.monitor:
        threading.Thread(target=serve_web, daemon=True).start()
        main(steamid64)
    elif args.server:
        serve_web()
        if UPDATE_EVENT.is_set():
            run_git_pull()
            restart_self()
    else:
        main(steamid64)
