import json
import re
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
import matplotlib.pyplot as plt

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ----------------------------
# Config (según enunciado)
# ----------------------------
ORIGIN = "MAD"

DESTS = {"Budapest": "BUD"}
START = date(2026, 3, 29)
END = date(2026, 3, 29)

"""
DESTS = {
    "Budapest": "BUD",
    "Praga": "PRG",
    "Viena": "VIE",
}
"""

#START = date(2026, 3, 29)
#END = date(2026, 4, 5)   # inclusive
MIN_FLIGHTS_PER_DAY = 5

# Kayak ES
BASE = "https://www.kayak.es/flights"


from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

def accept_cookies_kayak(driver):
    possible = [
        (By.XPATH, "//button[contains(., 'Aceptar')]"),
        (By.XPATH, "//button[contains(., 'Acepto')]"),
        (By.XPATH, "//button[contains(., 'Accept')]"),
        (By.CSS_SELECTOR, "button[aria-label*='Aceptar']"),
        (By.CSS_SELECTOR, "button[aria-label*='Accept']"),
    ]
    for by, sel in possible:
        try:
            btn = WebDriverWait(driver, 4).until(EC.element_to_be_clickable((by, sel)))
            btn.click()
            time.sleep(1)
            return
        except Exception:
            pass

# ----------------------------
# Helpers: parsing
# ----------------------------
def parse_price(text: str) -> float:
    # ejemplos: "123 €", "€123", "1.234 €"
    t = text.replace("\u202f", " ").strip()
    nums = re.findall(r"[\d.,]+", t)
    if not nums:
        raise ValueError(f"Cannot parse price from: {text}")
    num = nums[0].replace(".", "").replace(",", ".")
    return float(num)

def parse_duration_to_minutes(text: str) -> int:
    # ejemplos: "3h 10m", "2h", "55m", "3 h 10 min"
    t = text.lower().strip()
    h = 0
    m = 0
    mh = re.search(r"(\d+)\s*h", t)
    mm = re.search(r"(\d+)\s*m(in)?", t)
    if mh:
        h = int(mh.group(1))
    if mm:
        m = int(mm.group(1))
    total = h * 60 + m
    if total <= 0:
        raise ValueError(f"Cannot parse duration from: {text}")
    return total

def parse_stops(text: str) -> int:
    # ejemplos ES/EN: "Directo", "Sin escalas", "1 escala", "2 escalas", "Nonstop", "1 stop"
    t = text.lower().strip()
    if "direct" in t or "nonstop" in t or "sin escalas" in t or "directo" in t:
        return 0
    m = re.search(r"(\d+)\s*stop", t)
    if m:
        return int(m.group(1))
    m2 = re.search(r"(\d+)\s*escala", t)
    if m2:
        return int(m2.group(1))
    raise ValueError(f"Cannot parse stops from: {text}")

def build_url(origin: str, dest: str, d: date) -> str:
    # Ej: https://www.kayak.es/flights/MAD-BUD/2026-03-29
    # Puedes añadir parámetros si quieres (ordenación), pero mejor empezar simple.
    return f"{BASE}/{origin}-{dest}/{d.isoformat()}"


# ----------------------------
# Selenium setup + CDP network
# ----------------------------
def make_driver() -> webdriver.Chrome:
    opts = Options()
    # NO headless al principio (en muchas webs reduce bloqueos)
    # opts.add_argument("--headless=new")

    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")

    # Perfil persistente ayuda a no “parecer” sesión nueva cada vez
    # (si te da problemas, puedes quitarlo)
    # opts.add_argument(r"--user-data-dir=chrome_profile_kayak")

    # Habilitar logs de rendimiento (para ver eventos de red)
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(60)

    # Activar Network en CDP para poder pedir cuerpos de respuestas
    driver.execute_cdp_cmd("Network.enable", {})
    return driver

def _safe_json_loads(s: str) -> Optional[Any]:
    try:
        return json.loads(s)
    except Exception:
        return None


# ----------------------------
# Extracción “robusta” desde peticiones XHR (JSON)
# ----------------------------
import base64
import json
import time

def collect_network_json(driver, seconds: float = 18.0):
    """
    Recolecta respuestas que *parezcan* JSON desde logs performance.
    Kayak a veces marca JSON como text/plain, así que NO filtramos por mimeType.
    """
    deadline = time.time() + seconds
    seen_request_ids = set()
    payloads = []

    while time.time() < deadline:
        entries = driver.get_log("performance")
        for entry in entries:
            try:
                msg = json.loads(entry["message"])["message"]
            except Exception:
                continue

            if msg.get("method") != "Network.responseReceived":
                continue

            params = msg.get("params", {})
            request_id = params.get("requestId")
            if not request_id or request_id in seen_request_ids:
                continue
            seen_request_ids.add(request_id)

            response = params.get("response", {})
            url = response.get("url", "")

            # Solo nos interesan cosas de flights / search / results
            u = url.lower()
            if not any(k in u for k in ["flight", "flights", "horizon", "results", "search", "poll"]):
                continue

            try:
                body = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
                text = body.get("body", "")
                if body.get("base64Encoded"):
                    text = base64.b64decode(text).decode("utf-8", errors="ignore")

                data = None
                try:
                    data = json.loads(text)
                except Exception:
                    data = None

                if isinstance(data, (dict, list)):
                    payloads.append({"url": url, "data": data})
            except Exception:
                continue

        time.sleep(0.2)

    return payloads


def extract_flights_from_payloads(payloads: List[Dict[str, Any]], d: date, dest_name: str) -> List[Dict[str, Any]]:
    """
    Kayak cambia el shape del JSON. Aquí hacemos heurística:
    buscamos objetos que contengan precio + duración + stops.
    Si tu clase ve que no saca nada, te digo dónde tocar.
    """
    flights: List[Dict[str, Any]] = []

    def walk(obj):
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from walk(v)
        elif isinstance(obj, list):
            for it in obj:
                yield from walk(it)

    for p in payloads:
        data = p["data"]
        for node in walk(data):
            # Heurística: detectar campos “parecidos”
            # - precio: a veces "price", "totalPrice", "amount", etc.
            # - duración: a veces "duration", "durationMinutes"
            # - escalas: "stops", "stopCount"
            price = None
            duration = None
            stops = None

            # Precio
            for k in ("price", "totalPrice", "amount", "displayPrice"):
                if k in node:
                    price = node.get(k)
                    break

            # Duración (en minutos o en texto)
            for k in ("durationMinutes", "duration", "totalDurationMinutes"):
                if k in node:
                    duration = node.get(k)
                    break

            # Escalas
            for k in ("stops", "stopCount", "numberOfStops"):
                if k in node:
                    stops = node.get(k)
                    break

            # Normalización
            try:
                # price puede venir como número o como string “123 €”
                if isinstance(price, (int, float)):
                    price_val = float(price)
                elif isinstance(price, str):
                    price_val = parse_price(price)
                else:
                    continue

                # duration puede venir como int(min) o string
                if isinstance(duration, (int, float)):
                    duration_min = int(duration)
                elif isinstance(duration, str):
                    duration_min = parse_duration_to_minutes(duration)
                else:
                    continue

                # stops puede venir int o string
                if isinstance(stops, (int, float)):
                    stops_val = int(stops)
                elif isinstance(stops, str):
                    stops_val = parse_stops(stops)
                else:
                    continue

                if price_val <= 0 or duration_min <= 0 or stops_val < 0:
                    continue

                flights.append({
                    "date": d.isoformat(),
                    "destination": dest_name,
                    "price": price_val,
                    "duration_minutes": duration_min,
                    "stops": stops_val,
                })
            except Exception:
                continue

    # Quitar duplicados “obvios”
    uniq = []
    seen = set()
    for f in flights:
        key = (f["price"], f["duration_minutes"], f["stops"])
        if key not in seen:
            seen.add(key)
            uniq.append(f)

    return uniq


# ----------------------------
# Fallback DOM (por si Kayak no da JSON fácil)
# ----------------------------
def extract_flights_from_dom(driver, d: date, dest_name: str, limit: int) -> List[Dict[str, Any]]:
    flights = []
    # Selectores genéricos que suelen funcionar en listados; tocar con DevTools si hace falta
    cards = driver.find_elements(By.CSS_SELECTOR, "div[class*='result'], article, li[class*='result']")
    for c in cards:
        if len(flights) >= limit:
            break
        try:
            price_el = c.find_element(By.CSS_SELECTOR, "[class*='price'], span[aria-label*='€'], span[class*='Price']")
            dur_el = c.find_element(By.CSS_SELECTOR, "[class*='duration'], span[class*='Duration']")
            stops_el = c.find_element(By.CSS_SELECTOR, "[class*='stops'], span[class*='Stops']")

            flights.append({
                "date": d.isoformat(),
                "destination": dest_name,
                "price": parse_price(price_el.text),
                "duration_minutes": parse_duration_to_minutes(dur_el.text),
                "stops": parse_stops(stops_el.text),
            })
        except Exception:
            continue
    return flights


# ----------------------------
# Core scrape logic
# ----------------------------
def scrape_day_destination(driver, d: date, dest_name: str, dest_code: str) -> List[Dict[str, Any]]:
    url = build_url(ORIGIN, dest_code, d)
    driver.get(url)
    accept_cookies_kayak(driver)

    wait = WebDriverWait(driver, 40)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

# Espera a que Kayak muestre algo típico del listado (si no aparece, seguimos igualmente)
    possible_results_hints = [
        (By.CSS_SELECTOR, "div[class*='result']"),
        (By.XPATH, "//*[contains(., 'resultados')]"),
        (By.XPATH, "//*[contains(., 'Resultados')]"),
        (By.XPATH, "//*[contains(., '€')]"),
    ]
    for by, sel in possible_results_hints:
        try:
            wait.until(EC.presence_of_element_located((by, sel)))
            break
        except Exception:
            pass

    # Dale un margen a Kayak para lanzar XHR
    time.sleep(2.0)

    # 1) Intento por red (recomendado)
    payloads = collect_network_json(driver, seconds=12.0)
    flights = extract_flights_from_payloads(payloads, d, dest_name)

    # 2) Si no salen suficientes, intenta DOM + scroll
    scroll_tries = 0
    while len(flights) < MIN_FLIGHTS_PER_DAY and scroll_tries < 8:
        more = extract_flights_from_dom(driver, d, dest_name, limit=MIN_FLIGHTS_PER_DAY)
        # merge sin duplicados
        existing = {(f["price"], f["duration_minutes"], f["stops"]) for f in flights}
        for f in more:
            k = (f["price"], f["duration_minutes"], f["stops"])
            if k not in existing:
                flights.append(f)
                existing.add(k)
            if len(flights) >= MIN_FLIGHTS_PER_DAY:
                break

        if len(flights) < MIN_FLIGHTS_PER_DAY:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2.0)
            scroll_tries += 1

    if len(flights) < MIN_FLIGHTS_PER_DAY:
        print("\n[DEBUG] No se extrajeron vuelos suficientes.")
        print("[DEBUG] URL:", url)
        print("[DEBUG] payloads capturados:", len(payloads))
        for p in payloads[:8]:
            print("[DEBUG] payload url:", p["url"])
        raise RuntimeError(f"Only got {len(flights)} flights for {dest_name} on {d} (URL: {url})")

    return flights[:MIN_FLIGHTS_PER_DAY]


def main():
    driver = make_driver()
    all_rows = []

    try:
        d = START
        while d <= END:
            for dest_name, dest_code in DESTS.items():
                print(f"Scraping {dest_name} {d}...")
                rows = scrape_day_destination(driver, d, dest_name, dest_code)
                all_rows.extend(rows)

                # pausa “humana” variable
                time.sleep(1.5)

            d += timedelta(days=1)

    finally:
        driver.quit()

    # ----------------------------
    # flights.csv (requerido)
    # ----------------------------
    df = pd.DataFrame(all_rows, columns=["date", "destination", "price", "duration_minutes", "stops"])

    # Validaciones fuertes del enunciado
    if df.isna().any().any():
        raise ValueError("Dataset contains nulls.")

    expected_min = 8 * 3 * MIN_FLIGHTS_PER_DAY
    if len(df) < expected_min:
        raise ValueError(f"Not enough rows: {len(df)} < {expected_min}")

    df.to_csv("flights.csv", index=False)

    # ----------------------------
    # summary.csv (requerido)
    # ----------------------------
    g = df.groupby("destination", as_index=False).agg(
        avg_price=("price", "mean"),
        std_price=("price", "std"),
        min_price=("price", "min"),
        avg_duration=("duration_minutes", "mean"),
        direct_ratio=("stops", lambda s: (s == 0).mean()),
    )

    # Fórmula obligatoria
    g["final_score"] = (g["avg_price"] * 0.5) + (g["avg_duration"] * 0.3) + (g["std_price"] * 0.2)
    g = g.sort_values("final_score", ascending=True)
    g.to_csv("summary.csv", index=False)

    # ----------------------------
    # PNGs obligatorios
    # ----------------------------
    trend = df.groupby(["date", "destination"], as_index=False)["price"].mean()
    for dest in trend["destination"].unique():
        sub = trend[trend["destination"] == dest].sort_values("date")
        plt.plot(sub["date"], sub["price"], label=dest)
    plt.xticks(rotation=45, ha="right")
    plt.xlabel("date")
    plt.ylabel("avg price")
    plt.legend()
    plt.tight_layout()
    plt.savefig("price_trend.png", dpi=150)
    plt.close()

    plt.bar(g["destination"], g["final_score"])
    plt.xlabel("destination")
    plt.ylabel("final_score")
    plt.tight_layout()
    plt.savefig("score_comparison.png", dpi=150)
    plt.close()

    best = g.iloc[0]["destination"]
    print("\nBEST DESTINATION:", best)
    print(g)


if __name__ == "__main__":
    main()