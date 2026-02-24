"""Scrape Blocket Bil listings with full filter support.

Version 3 — straight question-and-answer wizard at startup.
Every filter is a simple question; press Enter to skip it (no filter applied).
Default sort: price ascending. Default target: 15 listings.

CLI flags:
  --headless / --no-headless    Browser visibility (default: headless)
  --no-interactive              Skip wizard, use pure defaults
  --url "..."                   Use a custom URL directly
  --no-details                  Skip detail pages (faster, less data)
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict

from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, Page


# ---------------------------------------------------------------------------
# Lookup tables (from blocket_url_filter_guide_komplett.md)
# ---------------------------------------------------------------------------

BRANDS: Dict[str, str] = {
    "abarth": "0.8093", "alfa romeo": "0.3233", "alpina": "0.8092",
    "aston martin": "0.6733", "audi": "0.744", "bentley": "0.7166",
    "bmw": "0.749", "byd": "0.8101", "chevrolet": "0.753",
    "chrysler": "0.754", "citroen": "0.757", "cupra": "0.8106",
    "dacia": "0.8079", "dodge": "0.764", "ds": "0.8091",
    "ferrari": "0.2999", "fiat": "0.766", "ford": "0.767",
    "honda": "0.771", "hummer": "0.7672", "hyundai": "0.772",
    "infiniti": "0.8065", "jaguar": "0.775", "jeep": "0.776",
    "kia": "0.777", "lamborghini": "0.6731", "land rover": "0.781",
    "lexus": "0.782", "lotus": "0.7191", "maserati": "0.3001",
    "mazda": "0.784", "mclaren": "0.8087", "mercedes-benz": "0.785",
    "mercedes": "0.785", "mg": "0.786", "mini": "0.7147",
    "mitsubishi": "0.787", "nio": "0.8109", "nissan": "0.792",
    "opel": "0.795", "peugeot": "0.796", "polestar": "0.8102",
    "porsche": "0.801", "ram": "0.8100", "renault": "0.804",
    "rolls royce": "0.7170", "saab": "0.806", "seat": "0.807",
    "skoda": "0.808", "smart": "0.7137", "subaru": "0.810",
    "suzuki": "0.811", "tesla": "0.8078", "toyota": "0.813",
    "volkswagen": "0.817", "vw": "0.817", "volvo": "0.818",
    "xpeng": "0.8104", "zeekr": "0.200841",
}

FUELS: Dict[str, str] = {
    "bensin": "1", "diesel": "2", "gas": "3", "cng": "3",
    "el": "4", "elektrisk": "4", "hybrid gas": "5",
    "hybrid bensin": "6", "hybrid": "6", "hybrid diesel": "8",
}

GEARBOXES: Dict[str, str] = {
    "manuell": "1", "manuell växellåda": "1", "automat": "2",
    "automatisk": "2", "automatisk växellåda": "2",
}

WHEEL_DRIVES: Dict[str, str] = {
    "2wd": "1", "tvåhjul": "1", "framhjul": "1", "bakhjul": "1",
    "4wd": "2", "awd": "2", "fyrhjul": "2", "4x4": "2",
}

SALES_FORMS: Dict[str, str] = {
    "privat": "1", "privatperson": "1",
    "företag": "2", "handlare": "2",
}

COLOURS: Dict[str, str] = {
    "blå": "2", "bla": "2", "grå": "6", "gra": "6",
    "vit": "9", "svart": "14", "silver": "15",
}

LOCATIONS: Dict[str, str] = {
    "stockholm": "0.300001", "uppsala": "0.300003",
    "södermanland": "0.300004", "ostergotland": "0.300005",
    "östergötland": "0.300005", "jonkoping": "0.300006",
    "jönköping": "0.300006", "kronoberg": "0.300007",
    "kalmar": "0.300008", "gotland": "0.300009",
    "blekinge": "0.300010", "skåne": "0.300012", "skane": "0.300012",
    "halland": "0.300013", "västra götaland": "0.300014",
    "vastra gotaland": "0.300014", "göteborg": "0.300014",
    "goteborg": "0.300014", "värmland": "0.300017", "varmland": "0.300017",
    "örebro": "0.300018", "orebro": "0.300018",
    "västmanland": "0.300019", "vastmanland": "0.300019",
    "dalarna": "0.300020", "gävleborg": "0.300021", "gavleborg": "0.300021",
    "västernorrland": "0.300022", "vasternorrland": "0.300022",
    "jämtland": "0.300023", "jamtland": "0.300023",
    "västerbotten": "0.300024", "vasterbotten": "0.300024",
    "norrbotten": "0.300025",
}

BODY_TYPES: Dict[str, str] = {
    "halvkombi 3": "1", "halvkombi3": "1", "3-dörrar": "1",
    "halvkombi 5": "2", "halvkombi5": "2", "5-dörrar": "2", "halvkombi": "2",
    "sedan": "3", "kombi": "4", "familjebuss": "5", "minivan": "5",
    "coupe": "6", "coupé": "6", "cab": "7", "cabriolet": "7", "convertible": "7",
    "pickup": "8", "suv": "9", "skåpbil": "10", "skåp": "10", "van": "10",
}

SORT_OPTIONS: Dict[str, str] = {
    "pris": "price", "price": "price",
    "pris_desc": "price_desc", "dyrast": "price_desc",
    "datum": "date", "date": "date", "nyast publicerad": "date",
    "år": "year_desc", "year": "year_desc", "nyast": "year_desc",
    "år_asc": "year", "äldst": "year",
    "mil": "mileage", "mileage": "mileage",
    "mil_desc": "mileage_desc",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Item:
    url: str
    title: str
    price: Optional[str]
    year: Optional[str]
    fuel: Optional[str]
    gearbox: Optional[str]
    location: Optional[str]
    seller_name: Optional[str]
    seller_type: Optional[str]
    make: Optional[str] = None
    model: Optional[str] = None
    body_type: Optional[str] = None
    color: Optional[str] = None
    wheel_drive: Optional[str] = None
    engine_power: Optional[str] = None
    mileage: Optional[str] = None
    engine_volume: Optional[str] = None
    seats: Optional[str] = None
    regnr: Optional[str] = None
    chassis_nr: Optional[str] = None
    reg_date: Optional[str] = None
    tax_class: Optional[str] = None
    max_trailer: Optional[str] = None


@dataclass
class RunResult:
    run_at: str
    source: str
    query_url: str
    total_scraped: int
    items: List[Item]


# ---------------------------------------------------------------------------
# Extraction – listing cards
# ---------------------------------------------------------------------------

def extract_card(card_el) -> Optional[Item]:
    try:
        link = card_el.query_selector('a[href*="/mobility/item/"]')
        if not link:
            return None
        href = link.get_attribute("href") or ""
        url = href if href.startswith("http") else f"https://www.blocket.se{href}"

        # Title
        title = ""
        title_span = link.query_selector("span:not([aria-hidden])")
        if title_span:
            title = title_span.inner_text().strip()
        if not title:
            h2 = card_el.query_selector("h2")
            if h2:
                title = h2.inner_text().strip()

        # Price
        price: Optional[str] = None
        price_el = card_el.query_selector("span.t3, [class*='price']")
        if price_el:
            price = price_el.inner_text().strip()
        if not price:
            m = re.search(r"(\d[\d\s\u00a0]+kr)", card_el.inner_text())
            if m:
                price = m.group(1).replace("\u00a0", " ").strip()

        # Subtitle: "2025 · 2 600 mil · Bensin · Automatisk"
        year = fuel = gearbox = None
        full_text = card_el.inner_text()
        subtitle_match = re.search(
            r"(\d{4})\s*[·•]\s*([\d\s]+mil)\s*[·•]\s*([^\n·•]+?)\s*[·•]\s*([^\n·•]+)",
            full_text,
        )
        if subtitle_match:
            year     = subtitle_match.group(1)
            fuel     = subtitle_match.group(3).strip()
            gearbox  = subtitle_match.group(4).strip()
        else:
            # Fallback: scan parts split by middot
            for line in full_text.splitlines():
                parts = [p.strip() for p in re.split(r"[·•\u00b7]", line) if p.strip()]
                for p in parts:
                    if re.match(r"^\d{4}$", p) and not year:
                        year = p
                    elif any(f in p for f in ["Bensin", "Diesel", "El", "Hybrid", "Gas", "Etanol", "Plug"]) and not fuel:
                        fuel = p
                    elif any(g in p for g in ["Automat", "Manuell", "Sekventiell"]) and not gearbox:
                        gearbox = p

        # Location / seller
        location = seller_name = seller_type = None
        for el in card_el.query_selector_all("span, div"):
            t = el.inner_text().strip()
            if not t or len(t) > 60 or "\n" in t:
                continue
            if any(x in t for x in ["Företag", "Privat", "Bytesrätt"]) and not seller_type:
                seller_type = t
            elif not re.search(r"\d", t) and len(t) > 2 and location is None:
                location = t
            elif location and not seller_name and t != location and not re.search(r"\d", t):
                seller_name = t

        make = model = None
        if title:
            parts = title.split(" ", 1)
            make  = parts[0]
            model = parts[1] if len(parts) > 1 else ""

        return Item(
            url=url, title=title, price=price, year=year,
            fuel=fuel, gearbox=gearbox, location=location,
            seller_name=seller_name, seller_type=seller_type,
            make=make, model=model,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Extraction – detail page
# ---------------------------------------------------------------------------

def fetch_ad_details(page: Page, item: Item) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        result = page.evaluate("""
() => {
  const data = {};
  document.querySelectorAll('.s-text-subtle').forEach(label => {
    const val = label.nextElementSibling;
    if (val) data[label.innerText.trim()] = val.innerText.trim();
  });
  return data;
}
""")
        mapping = {
            "Märke": "make", "Modell": "model", "Modellår": "year",
            "Biltyp": "body_type", "Drivmedel": "fuel", "Effekt": "engine_power",
            "Motorvolym": "engine_volume", "Miltal": "mileage",
            "Växellåda": "gearbox", "Max trailervikt": "max_trailer",
            "Drivhjul": "wheel_drive", "Säten": "seats", "Färg": "color",
            "Avgiftsklass": "tax_class", "Registreringsnummer": "regnr",
            "Chassinummer": "chassis_nr", "Registreringsdatum": "reg_date",
        }
        for label, field in mapping.items():
            if result.get(label):
                setattr(item, field, result[label])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------

def scrape_blocket(
    url: str,
    headless: bool = True,
    target: int = 15,
    fetch_details: bool = True,
) -> RunResult:
    items: List[Item] = []
    run_at = datetime.now(ZoneInfo("Europe/Stockholm")).isoformat()
    seen_urls: set = set()
    page_num = 1
    current_url = url

    with sync_playwright() as p:
        print("Startar webbläsare...", flush=True)
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(locale="sv-SE", viewport={"width": 1280, "height": 900})
        page = context.new_page()

        while len(items) < target:
            print(f"\n--- Sida {page_num} (har {len(items)}/{target} annonser) ---", flush=True)
            page.goto(current_url, wait_until="domcontentloaded")

            # Cookie consent
            try:
                consent = page.locator(
                    "button:has-text('Godkänn alla'), button:has-text('Acceptera'), button:has-text('OK')"
                )
                if consent.count() > 0:
                    consent.first.click()
                    page.wait_for_timeout(800)
            except Exception:
                pass

            try:
                page.wait_for_selector('a[href*="/mobility/item/"]', timeout=15000)
            except Exception:
                print("  Inga annonser hittades. Avslutar.", flush=True)
                break

            articles = page.query_selector_all("article")
            if not articles:
                links = page.query_selector_all('a[href*="/mobility/item/"]')
                seen_hrefs: set = set()
                articles = []
                for lnk in links:
                    href = lnk.get_attribute("href") or ""
                    if href in seen_hrefs:
                        continue
                    seen_hrefs.add(href)
                    parent = lnk.evaluate_handle(
                        "el => el.closest('article') || el.parentElement.parentElement"
                    )
                    articles.append(parent)

            new_count = 0
            for article in articles:
                if len(items) >= target:
                    break
                item = extract_card(article)
                if item and item.url and item.url not in seen_urls:
                    seen_urls.add(item.url)
                    items.append(item)
                    new_count += 1

            print(f"  +{new_count} nya annonser (totalt: {len(items)})", flush=True)

            if new_count == 0:
                print("  Inga nya annonser på sidan. Avslutar.", flush=True)
                break

            if len(items) >= target:
                break

            # Paginate
            if re.search(r"[?&]page=(\d+)", current_url):
                current_url = re.sub(
                    r"([?&]page=)(\d+)",
                    lambda m: m.group(1) + str(int(m.group(2)) + 1),
                    current_url,
                )
            else:
                sep = "&" if "?" in current_url else "?"
                current_url = f"{current_url}{sep}page=2"
            page_num += 1

        # Trim to exact target
        items = items[:target]

        # Detail pages
        if fetch_details and items:
            print(f"\nHämtar detaljer för {len(items)} annonser...", flush=True)
            detail_page = context.new_page()
            for idx, item in enumerate(items, 1):
                print(f"  {idx}/{len(items)}: {item.title or 'N/A'}", flush=True)
                try:
                    detail_page.goto(item.url, wait_until="domcontentloaded")
                    try:
                        consent = detail_page.locator(
                            "button:has-text('Godkänn alla'), button:has-text('Acceptera')"
                        )
                        if consent.count() > 0:
                            consent.first.click()
                            detail_page.wait_for_timeout(500)
                    except Exception:
                        pass
                    fetch_ad_details(detail_page, item)
                except Exception as e:
                    print(f"    Fel: {e}", flush=True)
            detail_page.close()

        browser.close()

    return RunResult(
        run_at=run_at,
        source="blocket.se",
        query_url=url,
        total_scraped=len(items),
        items=items,
    )


# ---------------------------------------------------------------------------
# Wizard helpers
# ---------------------------------------------------------------------------

def _q(prompt: str) -> str:
    """Ask one question, return stripped answer or empty string."""
    try:
        return input(f"{prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _resolve(answer: str, table: Dict[str, str]) -> Optional[str]:
    """Case-insensitive lookup in a code table. Returns code or None."""
    return table.get(answer.lower())


def _resolve_multi(answer: str, table: Dict[str, str]) -> List[str]:
    """Comma-separated lookup, returns list of codes found."""
    codes = []
    for part in answer.split(","):
        code = _resolve(part.strip(), table)
        if code and code not in codes:
            codes.append(code)
    return codes


# ---------------------------------------------------------------------------
# Interactive wizard – straight Q&A
# ---------------------------------------------------------------------------

def build_url_interactive() -> tuple[str, int, bool]:
    """
    Returns (url, target_count, fetch_details).
    Every question: press Enter = no filter / use default.
    """
    BASE = "https://www.blocket.se/mobility/search/car"
    params: list[tuple[str, str]] = []

    print("\n" + "=" * 56)
    print("  Blocket Bil Scraper")
    print("  Tryck Enter på valfri fråga för att hoppa över.")
    print("=" * 56 + "\n")

    # --- Number of listings ---
    raw = _q("Hur många annonser vill du hämta? [standard: 15]")
    target = 15
    if raw.isdigit():
        target = max(1, int(raw))

    # --- Sort ---
    raw = _q(
        "Sortering? (pris/dyrast/datum/nyast/äldst/mil/mil_desc) [standard: pris]"
    )
    sort_code = SORT_OPTIONS.get(raw.lower(), "price") if raw else "price"
    params.append(("sort", sort_code))

    # --- Brand(s) ---
    raw = _q("Märke? (t.ex. Volvo  eller  Volvo, BMW, Audi)")
    if raw:
        codes = _resolve_multi(raw, BRANDS)
        if codes:
            for c in codes:
                params.append(("variant", c))
        else:
            print(f"  ⚠ Hittade inget märke för '{raw}' – hoppas över.")

    # --- Model / free text search ---
    raw = _q("Modell/sökord? (t.ex. V60  eller  xc90 drag)")
    if raw:
        params.append(("q", raw.replace(" ", "+")))

    # --- Body type(s) ---
    raw = _q("Biltyp? (t.ex. SUV  eller  kombi, sedan, suv)")
    if raw:
        codes = _resolve_multi(raw, BODY_TYPES)
        if codes:
            for c in codes:
                params.append(("body_type", c))
        else:
            print(f"  ⚠ Okänd biltyp '{raw}' – hoppas över.")

    # --- Fuel(s) ---
    raw = _q("Drivmedel? (bensin/diesel/el/hybrid/hybrid diesel/gas)")
    if raw:
        codes = _resolve_multi(raw, FUELS)
        if codes:
            for c in codes:
                params.append(("fuel", c))
        else:
            print(f"  ⚠ Okänt drivmedel '{raw}' – hoppas över.")

    # --- Gearbox ---
    raw = _q("Växellåda? (manuell/automat)")
    if raw:
        code = _resolve(raw, GEARBOXES)
        if code:
            params.append(("gearbox", code))
        else:
            print(f"  ⚠ Okänd växellåda '{raw}' – hoppas över.")

    # --- Wheel drive ---
    raw = _q("Drivhjul? (2wd/4wd/awd)")
    if raw:
        code = _resolve(raw, WHEEL_DRIVES)
        if code:
            params.append(("wheel_drive", code))
        else:
            print(f"  ⚠ Okänt drivhjul '{raw}' – hoppas över.")

    # --- Sales form ---
    raw = _q("Säljare? (privat/företag)")
    if raw:
        code = _resolve(raw, SALES_FORMS)
        if code:
            params.append(("sales_form", code))
        else:
            print(f"  ⚠ Okänd säljare '{raw}' – hoppas över.")

    # --- Colour(s) ---
    raw = _q("Färg? (vit/svart/grå/blå/silver)")
    if raw:
        codes = _resolve_multi(raw, COLOURS)
        if codes:
            for c in codes:
                params.append(("exterior_colour", c))
        else:
            print(f"  ⚠ Okänd färg '{raw}' – hoppas över.")

    # --- Location(s) ---
    raw = _q("Region/Stad? (t.ex. Stockholm  eller  Skåne, Göteborg)")
    if raw:
        codes = _resolve_multi(raw, LOCATIONS)
        if codes:
            for c in codes:
                params.append(("location", c))
        else:
            print(f"  ⚠ Okänd region '{raw}' – hoppas över.")

    # --- Price range ---
    raw = _q("Maxpris? (kr, t.ex. 200000)")
    if raw.isdigit():
        params.append(("price_to", raw))
    raw = _q("Minpris? (kr, t.ex. 50000)")
    if raw.isdigit():
        params.append(("price_from", raw))

    # --- Year range ---
    raw = _q("Senast årsmodell från? (t.ex. 2018)")
    if raw.isdigit():
        params.append(("year_from", raw))
    raw = _q("Årsmodell till? (t.ex. 2024)")
    if raw.isdigit():
        params.append(("year_to", raw))

    # --- Mileage ---
    raw = _q("Max miltal? (mil, t.ex. 15000)")
    if raw.isdigit():
        params.append(("mileage_to", raw))

    # --- Engine power ---
    raw = _q("Min hästkrafter? (hk, t.ex. 150)")
    if raw.isdigit():
        params.append(("engine_effect_from", raw))
    raw = _q("Max hästkrafter? (hk, t.ex. 300)")
    if raw.isdigit():
        params.append(("engine_effect_to", raw))

    # --- Detail pages ---
    raw = _q("Hämta detaljinfo per annons? (reg.nr, färg, chassi etc.) [J/n]")
    fetch_details = raw.lower() not in ("n", "nej", "no")

    # Build URL
    query_string = "&".join(f"{k}={v}" for k, v in params)
    url = f"{BASE}?{query_string}" if query_string else BASE

    print(f"\nURL: {url}")
    print(f"Mål: {target} annonser\n")
    return url, target, fetch_details


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Scrapa Blocket Bil med fullständigt filterstöd.")
    parser.add_argument("--headless", dest="headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                        help="Öppna synlig webbläsare (för felsökning).")
    parser.add_argument("--no-interactive", dest="interactive", action="store_false", default=True,
                        help="Hoppa över guiden och använd default-inställningar.")
    parser.add_argument("--url", dest="url", default=None,
                        help="Använd denna URL direkt (hoppar över guiden).")
    parser.add_argument("--no-details", dest="fetch_details", action="store_false", default=True,
                        help="Hoppa över detaljsidor (snabbare, mindre data).")
    args = parser.parse_args(argv)

    if args.url:
        query_url     = args.url
        target        = 15
        fetch_details = args.fetch_details
    elif args.interactive:
        query_url, target, fetch_details = build_url_interactive()
    else:
        query_url     = "https://www.blocket.se/mobility/search/car?sort=price"
        target        = 15
        fetch_details = args.fetch_details

    result = scrape_blocket(
        url=query_url,
        headless=args.headless,
        target=target,
        fetch_details=fetch_details,
    )

    output = {
        "run_at":        result.run_at,
        "source":        result.source,
        "query_url":     result.query_url,
        "total_scraped": result.total_scraped,
        "items":         [asdict(item) for item in result.items],
    }

    safe_ts = result.run_at.replace(":", "-")
    results_dir = Path(__file__).parent / Path(__file__).stem / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    filepath = results_dir / f"blocket_result_{safe_ts}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✓ {result.total_scraped} annonser sparade i: {filepath}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
