from flask import Flask, render_template, request
import requests
import re
import logging
from urllib.parse import urlparse, urljoin
from datetime import date


app = Flask(__name__)

# ---------- ЛОГИ ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("shopify-checker")

# ---------- КОНСТАНТИ ----------
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

BODY_MARKERS = [
    r"cdn\.shopify\.com",
    r"\bmyshopify\.com\b",
    r"window\.Shopify\b",
    r"Shopify\.theme\b",
    r"shopify-digital-wallet",
]

# ---------- ДОП. ФУНКЦІЇ ----------
def normalize_candidates(raw: str):
    raw = raw.strip()
    if not raw:
        return []
    if not re.match(r"^https?://", raw, re.I):
        raw_https = "https://" + raw
    else:
        raw_https = raw
    p = urlparse(raw_https)
    host = p.netloc or p.path
    host = host.strip("/")
    if not host:
        return []
    base_hosts = {host}
    if not host.startswith("www."):
        base_hosts.add("www." + host)
    else:
        base_hosts.add(host.replace("www.", "", 1))

    candidates = []
    for h in base_hosts:
        candidates += [f"https://{h}/", f"http://{h}/"]

    seen, uniq = set(), []
    for u in candidates:
        if u not in seen:
            uniq.append(u); seen.add(u)
    return uniq

def fetch(url: str):
    log.info(f"GET {url}")
    r = requests.get(url, headers=UA, timeout=8, allow_redirects=True)
    log.info(f" -> {r.status_code} {r.url}")
    return r

def check_body_markers(text: str):
    hits = []
    for pat in BODY_MARKERS:
        if re.search(pat, text, re.I):
            hits.append(pat)
    return hits

def try_cart_json(final_url: str):
    cart_url = urljoin(final_url, "/cart.js")
    try:
        log.info(f"ПЕРЕВІРКА /cart.js -> {cart_url}")
        r = requests.get(cart_url, headers=UA, timeout=8)
        ctype = r.headers.get("Content-Type", "").lower()
        log.info(f" /cart.js status={r.status_code} content-type={ctype}")
        if r.status_code == 200 and ctype.startswith("application/json"):
            data = r.json()
            # достатньо будь-якого характерного ключа
            if isinstance(data, dict) and (
                "items" in data or "token" in data or "attributes" in data
            ):
                return True, f"Доступний {cart_url} (валідний JSON із ключами Shopify)"
        return False, f"/cart.js не підтвердив Shopify (status={r.status_code}, type={ctype})"
    except Exception as e:
        log.warning(f"/cart.js ПОМИЛКА: {e}")
        return False, f"/cart.js помилка: {e}"

# ---------- ОСНОВНА ЛОГІКА ----------
def is_shopify_site(input_url: str):
    reasons = []
    for candidate in normalize_candidates(input_url):
        try:
            r = fetch(candidate)
            final_url = r.url

            # 1) Заголовки
            shopify_headers = [k for k in r.headers.keys() if k.lower().startswith("x-shopify-")]
            if shopify_headers:
                reasons.append(f"Заголовки містять X-Shopify-* ({', '.join(shopify_headers[:5])}...)")
                log.info(" СИГНАЛ: X-Shopify-* headers -> ВИСОКА")
                # Спробуємо cart.js (тільки як плюс)
                ok_cart, cart_reason = try_cart_json(final_url)
                if ok_cart:
                    reasons.append(cart_reason)
                return True, "висока", final_url, reasons

            # 2) Кукі
            shopify_cookies = [c.name for c in r.cookies if c.name.lower().startswith("_shopify")]
            if shopify_cookies:
                reasons.append(f"Знайдено Shopify-кукі: {', '.join(shopify_cookies[:6])}")
                log.info(f" СИГНАЛ: cookies={shopify_cookies}")

            # 3) Маркери в HTML
            text = r.text or ""
            marker_hits = check_body_markers(text)
            if marker_hits:
                reasons.append("У розмітці є маркери: " + ", ".join(marker_hits))
                log.info(f" СИГНАЛ: markers={marker_hits}")

            # 4) cart.js (НЕ знижує впевненість; тільки додає)
            ok_cart, cart_reason = try_cart_json(final_url)
            if ok_cart:
                reasons.append(cart_reason)
                log.info(" СИГНАЛ: /cart.js JSON -> ВИСОКА")
                return True, "висока", final_url, reasons

            # ---------- Підсумок по сигналам без cart.js ----------
            # Нове правило: і кукі, і маркери -> висока
            if shopify_cookies and marker_hits:
                log.info(" РІШЕННЯ: cookies+markers -> ВИСОКА")
                return True, "висока", final_url, reasons + [cart_reason]

            # Є або кукі, або маркери -> середня
            if shopify_cookies or marker_hits:
                log.info(" РІШЕННЯ: cookies АБО markers -> СЕРЕДНЯ")
                return True, "середня", final_url, reasons + [cart_reason]

            # Немає — пробуємо наступного кандидата
            reasons.append(f"Немає ознак Shopify на {final_url}")
            log.info(" БЕЗ СИГНАЛІВ, переходимо до наступного кандидата")

        except Exception as e:
            msg = f"Не вдалося відкрити {candidate}: {e}"
            reasons.append(msg)
            log.warning(msg)
            continue

    # У всіх кандидатів ознак немає
    log.info(" РІШЕННЯ: НЕ Shopify")
    return False, "низька", None, reasons

@app.route("/privacy")
def privacy():
    return render_template(
        "privacy.html",
        company_name="Shopify Detector",
        contact_email="quantumwebs.official@gmail.com",
        last_updated=date.today().strftime("%Y-%m-%d"),
    )

# ---------- МАРШРУТИ ----------
@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    details = []
    confidence = None
    final_url = None
    input_value = ""

    if request.method == "POST":
        input_value = request.form.get("site_url", "").strip()
        log.info(f"==== СТАРТ ПЕРЕВІРКИ: {input_value} ====")
        ok, confidence, final_url, details = is_shopify_site(input_value)
        if ok:
            result = f"✅ Схоже, що це сайт на Shopify (впевненість: {confidence})."
        else:
            result = "❌ Ознак Shopify не знайдено."
        log.info(f"==== ФІНІШ: {result} ====")

    return render_template(
        "index.html",
        result=result,
        details=details,
        confidence=confidence,
        final_url=final_url,
        input_value=input_value,
    )

if __name__ == "__main__":
    app.run(debug=False)
