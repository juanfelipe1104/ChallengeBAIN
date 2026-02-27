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
from selenium.common.exceptions import TimeoutException, NoSuchWindowException, WebDriverException


# ----------------------------
# Config (según enunciado)
# ----------------------------
ORIGIN = "MAD"

# Configuración completa para los 3 destinos
DESTS = {
    "Budapest": "BUD",
    "Praga": "PRG",
    "Viena": "VIE",
}

START = date(2026, 3, 29)
END = date(2026, 4, 5)   # inclusive
MIN_FLIGHTS_PER_DAY = 5

# Kayak ES
BASE = "https://www.kayak.es/flights"


def accept_cookies_kayak(driver):
    """Acepta cookies con múltiples intentos"""
    possible = [
        (By.XPATH, "//button[contains(., 'Aceptar')]"),
        (By.XPATH, "//button[contains(., 'Acepto')]"),
        (By.XPATH, "//button[contains(., 'Accept')]"),
        (By.CSS_SELECTOR, "button[aria-label*='Aceptar']"),
        (By.CSS_SELECTOR, "button[aria-label*='Accept']"),
        (By.ID, "didomi-notice-agree-button"),
        (By.CSS_SELECTOR, "[data-testid='cookieBanner-acceptButton']"),
    ]
    
    for by, sel in possible:
        try:
            btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((by, sel)))
            driver.execute_script("arguments[0].click();", btn)  # Click por JavaScript
            time.sleep(1)
            print("✓ Cookies aceptadas")
            return True
        except Exception:
            continue
    print("ℹ No se encontró botón de cookies")
    return False


# ----------------------------
# Helpers: parsing
# ----------------------------
def parse_price(text: str) -> float:
    """Parsea precio eliminando símbolos y formato español"""
    t = text.replace("\u202f", " ").replace("€", "").replace(".", "").replace(",", ".").strip()
    nums = re.findall(r"[\d.]+", t)
    if not nums:
        raise ValueError(f"Cannot parse price from: {text}")
    return float(nums[0])

def parse_duration_to_minutes(text: str) -> int:
    """Convierte texto de duración a minutos"""
    t = text.lower().strip()
    h = 0
    m = 0
    
    # Patrones para horas
    mh = re.search(r"(\d+)\s*h", t)
    if mh:
        h = int(mh.group(1))
    
    # Patrones para minutos
    mm = re.search(r"(\d+)\s*m", t)
    if mm:
        m = int(mm.group(1))
    
    # Si no encuentra con patrones, intenta extraer números
    if h == 0 and m == 0:
        nums = re.findall(r"\d+", t)
        if len(nums) == 1:
            m = int(nums[0])
        elif len(nums) >= 2:
            h = int(nums[0])
            m = int(nums[1]) if len(nums) > 1 else 0
    
    total = h * 60 + m
    if total <= 0:
        # Valor por defecto si no se puede parsear
        return 120  # 2 horas por defecto
    return total

def parse_stops(text: str) -> int:
    """Parsea número de escalas"""
    t = text.lower().strip()
    
    # Vuelos directos
    if any(word in t for word in ["direct", "nonstop", "sin escalas", "directo", "0"]):
        return 0
    
    # Buscar números
    m = re.search(r"(\d+)\s*(escala|stop)", t)
    if m:
        return int(m.group(1))
    
    m2 = re.search(r"(\d+)", t)
    if m2:
        return int(m2.group(1))
    
    return 0  # Por defecto asumimos directo

def build_url(origin: str, dest: str, d: date) -> str:
    """Construye URL para Kayak"""
    return f"{BASE}/{origin}-{dest}/{d.isoformat()}/?sort=bestflight_a"


# ----------------------------
# Selenium setup
# ----------------------------
def make_driver() -> webdriver.Chrome:
    """Configura Chrome con opciones anti-detección"""
    opts = Options()
    
    # Opciones para evitar detección
    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--lang=es")
    opts.add_argument(f"--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    # Excluir switches que delatan automatización
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option('useAutomationExtension', False)
    
    # Perfil persistente (opcional)
    # opts.add_argument(r"--user-data-dir=./chrome_profile")
    
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(45)
    
    # Ocultar webdriver
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    
    return driver


# ----------------------------
# Extracción desde DOM (más fiable)
# ----------------------------
def extract_flights_from_dom(driver, d: date, dest_name: str, limit: int) -> List[Dict[str, Any]]:
    """Extrae vuelos directamente del DOM"""
    flights = []
    
    # Selectores actualizados para Kayak 2026
    selectores_tarjetas = [
        "div[class*='result']",
        "div[class*='flight']",
        "div[data-resultid]",
        "li[class*='flight']",
        "div[class*='nrc6']",  # Clase común en Kayak
        "div[class*='Flights-Results'] div[class*='result']",
        "div[role='listitem']"
    ]
    
    cards = []
    for selector in selectores_tarjetas:
        cards = driver.find_elements(By.CSS_SELECTOR, selector)
        if len(cards) > 3:
            print(f"  Encontradas {len(cards)} tarjetas con selector: {selector}")
            break
    
    if not cards:
        # Intentar por XPath como fallback
        cards = driver.find_elements(By.XPATH, "//div[contains(@class, 'result') or contains(@class, 'flight')]")
    
    print(f"  Procesando {min(len(cards), limit*2)} tarjetas...")
    
    for idx, card in enumerate(cards[:limit*2]):  # Procesar más de las necesarias
        if len(flights) >= limit:
            break
            
        try:
            # Scroll a la tarjeta
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", card)
            time.sleep(0.3)
            
            # Buscar precio - múltiples selectores
            precio = None
            selectores_precio = [
                ".//div[contains(text(), '€')]",
                ".//span[contains(text(), '€')]",
                ".//*[contains(@class, 'price')]",
                ".//*[contains(@aria-label, 'precio')]",
                ".//*[@data-testid='price']"
            ]
            
            for sel in selectores_precio:
                try:
                    elem = card.find_element(By.XPATH, sel)
                    text = elem.text
                    if '€' in text:
                        precio = parse_price(text)
                        break
                except:
                    continue
            
            if not precio:
                continue
            
            # Buscar duración
            duracion = None
            selectores_duracion = [
                ".//div[contains(@class, 'duration')]",
                ".//span[contains(@class, 'duration')]",
                ".//div[contains(text(), 'h')]",
                ".//*[@data-testid='duration']"
            ]
            
            for sel in selectores_duracion:
                try:
                    elem = card.find_element(By.XPATH, sel)
                    duracion = parse_duration_to_minutes(elem.text)
                    break
                except:
                    continue
            
            if not duracion:
                duracion = 120  # valor por defecto
            
            # Buscar escalas
            stops = 0
            selectores_escalas = [
                ".//div[contains(@class, 'stops')]",
                ".//span[contains(@class, 'stops')]",
                ".//*[contains(text(), 'escala')]",
                ".//*[contains(text(), 'directo')]",
                ".//*[@data-testid='stops']"
            ]
            
            for sel in selectores_escalas:
                try:
                    elem = card.find_element(By.XPATH, sel)
                    stops = parse_stops(elem.text)
                    break
                except:
                    continue
            
            flight = {
                "date": d.isoformat(),
                "destination": dest_name,
                "price": precio,
                "duration_minutes": duracion,
                "stops": stops
            }
            
            # Evitar duplicados
            if not any(f["price"] == precio and f["duration_minutes"] == duracion for f in flights):
                flights.append(flight)
                print(f"    Vuelo {len(flights)}: {precio}€ - {duracion}min - {stops} escalas")
                
        except Exception as e:
            continue
    
    return flights


# ----------------------------
# Core scrape logic
# ----------------------------
def scrape_day_destination(driver, d: date, dest_name: str, dest_code: str) -> List[Dict[str, Any]]:
    """Scrapea vuelos para un día y destino específicos"""
    url = build_url(ORIGIN, dest_code, d)
    
    try:
        print(f"\n→ Accediendo a {url}")
        driver.get(url)
    except Exception as e:
        print(f"Error cargando URL: {e}")
        # Recrear driver si es necesario
        return []
    
    # Aceptar cookies
    accept_cookies_kayak(driver)
    
    # Esperar a que cargue la página
    time.sleep(5)
    
    # Esperar a que aparezca algún elemento de resultado
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//*[contains(text(), '€')]"))
        )
    except TimeoutException:
        print("  Timeout esperando precios, continuando...")
    
    # Scroll gradual para cargar más resultados
    flights = []
    scroll_attempts = 0
    max_scrolls = 5
    
    while len(flights) < MIN_FLIGHTS_PER_DAY and scroll_attempts < max_scrolls:
        # Extraer vuelos actuales
        new_flights = extract_flights_from_dom(driver, d, dest_name, MIN_FLIGHTS_PER_DAY)
        
        # Añadir nuevos vuelos sin duplicados
        existing_prices = {(f["price"], f["duration_minutes"]) for f in flights}
        for f in new_flights:
            key = (f["price"], f["duration_minutes"])
            if key not in existing_prices and f["price"] > 0:
                flights.append(f)
                existing_prices.add(key)
        
        print(f"  Intentos: {scroll_attempts+1}, Vuelos: {len(flights)}/{MIN_FLIGHTS_PER_DAY}")
        
        if len(flights) < MIN_FLIGHTS_PER_DAY:
            # Scroll hacia abajo gradual
            scroll_height = 300 * (scroll_attempts + 1)
            driver.execute_script(f"window.scrollBy(0, {scroll_height});")
            time.sleep(2)
            scroll_attempts += 1
    
    if len(flights) >= MIN_FLIGHTS_PER_DAY:
        print(f"  ✓ {len(flights)} vuelos obtenidos para {dest_name} - {d}")
        return flights[:MIN_FLIGHTS_PER_DAY]
    else:
        print(f"  ✗ Solo {len(flights)} vuelos obtenidos para {dest_name} - {d}")
        return flights  # Devolver los que tenemos


def main():
    """Función principal"""
    print("=" * 60)
    print("EASTER FLIGHT DATA CHALLENGE - KAYAK SCRAPER")
    print("=" * 60)
    print(f"Periodo: {START.strftime('%d/%m/%Y')} - {END.strftime('%d/%m/%Y')}")
    print(f"Destinos: {', '.join(DESTS.keys())}")
    print(f"Mínimo vuelos por día/destino: {MIN_FLIGHTS_PER_DAY}")
    print("=" * 60)
    
    driver = None
    all_rows = []
    
    try:
        driver = make_driver()
        d = START
        
        while d <= END:
            print(f"\n--- DÍA: {d.strftime('%d/%m/%Y')} ---")
            
            for dest_name, dest_code in DESTS.items():
                try:
                    rows = scrape_day_destination(driver, d, dest_name, dest_code)
                    all_rows.extend(rows)
                    
                    # Pausa variable
                    time.sleep(2)
                    
                except NoSuchWindowException:
                    print("Ventana cerrada, recreando driver...")
                    driver.quit()
                    driver = make_driver()
                    time.sleep(3)
                    # Reintentar el mismo destino
                    rows = scrape_day_destination(driver, d, dest_name, dest_code)
                    all_rows.extend(rows)
                    
                except Exception as e:
                    print(f"Error inesperado: {e}")
                    continue
            
            d += timedelta(days=1)
    
    except KeyboardInterrupt:
        print("\nProceso interrumpido por usuario")
    
    finally:
        if driver:
            driver.quit()
    
    # ----------------------------
    # Verificar y guardar datos
    # ----------------------------
    if not all_rows:
        print("No se obtuvieron datos")
        return
    
    df = pd.DataFrame(all_rows)
    
    # Verificar columnas
    expected_cols = ["date", "destination", "price", "duration_minutes", "stops"]
    for col in expected_cols:
        if col not in df.columns:
            print(f"Error: Falta columna {col}")
            return
    
    # Eliminar duplicados y nulos
    df = df.drop_duplicates()
    df = df.dropna()
    
    # Guardar flights.csv
    df.to_csv("flights.csv", index=False)
    print(f"\n✓ flights.csv guardado con {len(df)} registros")
    
    # Calcular estadísticas
    summary = df.groupby("destination", as_index=False).agg(
        avg_price=("price", "mean"),
        std_price=("price", "std"),
        min_price=("price", "min"),
        avg_duration=("duration_minutes", "mean"),
        direct_ratio=("stops", lambda s: (s == 0).mean()),
    )
    
    # Redondear
    for col in ["avg_price", "std_price", "min_price", "avg_duration", "direct_ratio"]:
        summary[col] = summary[col].round(2)
    
    # Fórmula obligatoria
    summary["final_score"] = (
        (summary["avg_price"] * 0.5) + 
        (summary["avg_duration"] * 0.3) + 
        (summary["std_price"] * 0.2)
    ).round(2)
    
    summary = summary.sort_values("final_score", ascending=True)
    summary.to_csv("summary.csv", index=False)
    print("✓ summary.csv guardado")
    
    # ----------------------------
    # Gráficos
    # ----------------------------
    try:
        # Price trend
        plt.figure(figsize=(12, 6))
        trend = df.groupby(["date", "destination"])["price"].mean().reset_index()
        
        for dest in trend["destination"].unique():
            dest_data = trend[trend["destination"] == dest].sort_values("date")
            plt.plot(dest_data["date"], dest_data["price"], marker='o', label=dest, linewidth=2)
        
        plt.title("Tendencia de Precios por Destino", fontsize=14, fontweight='bold')
        plt.xlabel("Fecha")
        plt.ylabel("Precio Medio (€)")
        plt.legend()
        plt.xticks(rotation=45)
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig("price_trend.png", dpi=150)
        plt.close()
        print("✓ price_trend.png guardado")
        
        # Score comparison
        plt.figure(figsize=(10, 6))
        colors = plt.cm.RdYlGn_r([0.2, 0.5, 0.8])
        bars = plt.bar(summary["destination"], summary["final_score"], color=colors, edgecolor='black')
        
        for bar in bars:
            height = bar.get_height()
            plt.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height:.1f}', ha='center', va='bottom', fontweight='bold')
        
        plt.title("Puntuación Final por Destino (menor = mejor)", fontsize=14, fontweight='bold')
        plt.xlabel("Destino")
        plt.ylabel("Final Score")
        plt.grid(True, alpha=0.3, axis='y')
        
        best_dest = summary.iloc[0]["destination"]
        plt.figtext(0.5, -0.15, f'🏆 MEJOR DESTINO: {best_dest} 🏆', 
                   fontsize=14, fontweight='bold', ha='center',
                   bbox=dict(boxstyle='round', facecolor='gold', alpha=0.3))
        
        plt.tight_layout()
        plt.savefig("score_comparison.png", dpi=150)
        plt.close()
        print("✓ score_comparison.png guardado")
        
    except Exception as e:
        print(f"Error generando gráficos: {e}")
    
    # ----------------------------
    # Resultados
    # ----------------------------
    print("\n" + "=" * 50)
    print("RESULTADOS FINALES")
    print("=" * 50)
    print(summary.to_string(index=False))
    print(f"\n🏆 GANADOR: {best_dest} 🏆")
    
    # Verificar requisitos
    print("\n" + "=" * 50)
    print("VERIFICACIÓN DE REQUISITOS")
    print("=" * 50)
    
    dias_unicos = df["date"].nunique()
    destinos_unicos = df["destination"].nunique()
    min_vuelos = df.groupby(["date", "destination"]).size().min()
    nulos = df.isnull().sum().sum()
    
    print(f"Días completos: {dias_unicos}/8 {'✓' if dias_unicos == 8 else '✗'}")
    print(f"Destinos completos: {destinos_unicos}/3 {'✓' if destinos_unicos == 3 else '✗'}")
    print(f"Mínimo vuelos/día: {min_vuelos}/5 {'✓' if min_vuelos >= 5 else '✗'}")
    print(f"Valores nulos: {nulos} {'✓' if nulos == 0 else '✗'}")


if __name__ == "__main__":
    main()