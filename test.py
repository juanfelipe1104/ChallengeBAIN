import re
import time
from datetime import date, timedelta

import pandas as pd
import matplotlib.pyplot as plt

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ----------------------------
# Config
# ----------------------------
ORIGIN = "mad"
DESTS = {
    "Budapest": "bud",
    "Praga": "prg",
    "Viena": "vie",
}

START = date(2026, 3, 29)
END = date(2026, 4, 5)   # inclusive
MIN_FLIGHTS_PER_DAY = 5

BASE = "https://www.skyscanner.net/transport/flights"

PARAMS = "adultsv2=1&cabinclass=economy&currency=EUR&locale=es-ES&market=ES"


# ----------------------------
# Helpers: parsing
# ----------------------------
def to_yymmdd(d: date) -> str:
    return d.strftime("%y%m%d")

def parse_price(text: str) -> float:
    # ejemplos: "€123", "123 €", "1.234 €"
    t = text.replace("\u202f", " ").strip()
    nums = re.findall(r"[\d.,]+", t)
    if not nums:
        raise ValueError(f"Cannot parse price from: {text}")
    num = nums[0].replace(".", "").replace(",", ".")
    return float(num)

def parse_duration_to_minutes(text: str) -> int:
    # ejemplos: "3h 10m", "2h", "55m"
    t = text.lower().strip()
    h = 0
    m = 0
    mh = re.search(r"(\d+)\s*h", t)
    mm = re.search(r"(\d+)\s*m", t)
    if mh:
        h = int(mh.group(1))
    if mm:
        m = int(mm.group(1))
    total = h * 60 + m
    if total <= 0:
        raise ValueError(f"Cannot parse duration from: {text}")
    return total

def parse_stops(text: str) -> int:
    # ejemplos: "Direct", "Nonstop", "1 stop", "2 stops"
    t = text.lower().strip()
    if "direct" in t or "nonstop" in t or "sin escalas" in t:
        return 0
    m = re.search(r"(\d+)\s*stop", t)
    if m:
        return int(m.group(1))
    m2 = re.search(r"(\d+)\s*escala", t)
    if m2:
        return int(m2.group(1))
    # fallback: si no se entiende, lo marcamos como inválido
    raise ValueError(f"Cannot parse stops from: {text}")


# ----------------------------
# Selenium setup
# ----------------------------
def make_driver() -> webdriver.Chrome:
    opts = Options()
    # headless a veces aumenta bloqueos; si te bloquea, prueba SIN headless.
    # opts.add_argument("--headless=new")

    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--incognito")
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(60)
    return driver

def accept_cookies_if_present(driver):
    # Selector típico (cambia por país/idioma). Ajusta con DevTools.
    possible = [
        (By.CSS_SELECTOR, "button[aria-label*='Accept']"),
        (By.CSS_SELECTOR, "button[aria-label*='Aceptar']"),
        (By.XPATH, "//button[contains(., 'Accept')]"),
        (By.XPATH, "//button[contains(., 'Aceptar')]"),
    ]
    for by, sel in possible:
        try:
            btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((by, sel)))
            btn.click()
            time.sleep(1)
            return
        except Exception:
            pass

def build_url(dest_code: str, d: date) -> str:
    return f"{BASE}/{ORIGIN}/{dest_code}/{to_yymmdd(d)}/?{PARAMS}"


# ----------------------------
# Core scrape logic
# ----------------------------
def scrape_day_destination(driver, d: date, dest_name: str, dest_code: str):
    url = build_url(dest_code, d)
    driver.get(url)
    accept_cookies_if_present(driver)

    # Espera a que aparezca "algo" de resultados.
    # OJO: estos selectores son los que más vas a retocar.
    wait = WebDriverWait(driver, 25)

    # Intentos de detectar cards / listado
    possible_list_selectors = [
        (By.CSS_SELECTOR, "[data-testid*='result']"),
        (By.CSS_SELECTOR, "div[class*='FlightsResults']"),
        (By.CSS_SELECTOR, "div[class*='result']"),
    ]

    results_root = None
    for by, sel in possible_list_selectors:
        try:
            results_root = wait.until(EC.presence_of_all_elements_located((by, sel)))
            if results_root:
                break
        except Exception:
            continue

    # Si no carga nada, levantamos error
    if not results_root:
        raise RuntimeError(f"No results detected for {dest_name} on {d} (URL: {url})")

    flights = []
    scroll_tries = 0

    while len(flights) < MIN_FLIGHTS_PER_DAY and scroll_tries < 8:
        # Vuelve a capturar cards visibles en cada iteración
        cards = driver.find_elements(By.CSS_SELECTOR, "[data-testid*='result'], article, div[class*='result']")
        for c in cards:
            if len(flights) >= MIN_FLIGHTS_PER_DAY:
                break

            try:
                # Selectores a retocar en Skyscanner real
                price_el = c.find_element(By.CSS_SELECTOR, "[data-testid*='price'], span[class*='price'], div[class*='price']")
                dur_el = c.find_element(By.CSS_SELECTOR, "[data-testid*='duration'], span[class*='duration'], div[class*='duration']")
                stops_el = c.find_element(By.CSS_SELECTOR, "[data-testid*='stops'], span[class*='stops'], div[class*='stops']")

                price = parse_price(price_el.text)
                duration_minutes = parse_duration_to_minutes(dur_el.text)
                stops = parse_stops(stops_el.text)

                flights.append({
                    "date": d.isoformat(),
                    "destination": dest_name,
                    "price": price,
                    "duration_minutes": duration_minutes,
                    "stops": stops,
                })
            except Exception:
                # card incompleta / selector no coincide / datos raros
                continue

        if len(flights) < MIN_FLIGHTS_PER_DAY:
            # Scroll para cargar más resultados
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2.0)
            scroll_tries += 1

    if len(flights) < MIN_FLIGHTS_PER_DAY:
        raise RuntimeError(f"Only got {len(flights)} flights for {dest_name} on {d}")

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

                # pausa “humana”
                time.sleep(2.0)

            d += timedelta(days=1)

    finally:
        driver.quit()

    # ----------------------------
    # flights.csv
    # ----------------------------
    df = pd.DataFrame(all_rows, columns=["date", "destination", "price", "duration_minutes", "stops"])

    # Validaciones fuertes (según enunciado)
    if df.isna().any().any():
        raise ValueError("Dataset contains nulls.")
    # 8 días * 3 destinos * 5 vuelos = 120 filas mínimo
    expected_min = 8 * 3 * MIN_FLIGHTS_PER_DAY
    if len(df) < expected_min:
        raise ValueError(f"Not enough rows: {len(df)} < {expected_min}")

    df.to_csv("flights.csv", index=False)

    # ----------------------------
    # summary.csv
    # ----------------------------
    g = df.groupby("destination", as_index=False).agg(
        avg_price=("price", "mean"),
        std_price=("price", "std"),
        min_price=("price", "min"),
        avg_duration=("duration_minutes", "mean"),
        direct_ratio=("stops", lambda s: (s == 0).mean()),
    )

    g["final_score"] = (g["avg_price"] * 0.5) + (g["avg_duration"] * 0.3) + (g["std_price"] * 0.2)

    # Orden útil (no obligatorio)
    g = g.sort_values("final_score", ascending=True)

    g.to_csv("summary.csv", index=False)

    # ----------------------------
    # PNGs obligatorios
    # ----------------------------
    # price_trend.png: tendencia de precio medio por día y destino
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

    # score_comparison.png: barras de final_score por destino
    plt.bar(g["destination"], g["final_score"])
    plt.xlabel("destination")
    plt.ylabel("final_score")
    plt.tight_layout()
    plt.savefig("score_comparison.png", dpi=150)
    plt.close()

    # Resultado final
    best = g.iloc[0]["destination"]
    print("\nBEST DESTINATION:", best)
    print(g)


if __name__ == "__main__":
    main()