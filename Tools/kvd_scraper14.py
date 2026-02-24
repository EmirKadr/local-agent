"""Scrape Kvdbil auctions with brand and model extraction.

Version 13 – adds an interactive startup wizard where the user can choose
which deadline window to collect (yesterday / today / tomorrow / all) and
apply any URL filter documented in kvd_url_filter_guide.md.

Running without any flags launches the wizard. All previous CLI flags
(--headless / --no-headless) still work. A new --no-interactive flag skips
the wizard and behaves exactly like version 12.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Dict
from urllib.parse import urlencode

from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, Page, Locator


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Item:
    url: str
    deadline_text: str
    title: str
    subtitle: str
    year: Optional[str]
    mileage: Optional[str]
    fuel: Optional[str]
    location: Optional[str]
    leading_bid: Optional[str]
    regnr: Optional[str] = None
    condition_status: Optional[str] = None
    price_in_dealership: Optional[str] = None
    gearbox: Optional[str] = None
    condition_report: Optional[Dict[str, dict]] = None
    make: Optional[str] = None
    model: Optional[str] = None


@dataclass
class RunResult:
    run_at: str
    source: str
    query_url: str
    items: List[Item]


# ---------------------------------------------------------------------------
# Deadline helpers
# ---------------------------------------------------------------------------

# Mapping from Swedish text on the site to a canonical key used for filtering
DEADLINE_KEYS = {
    "igår":    {"Igår"},
    "idag":    {"Idag", "Ikväll"},
    "imorgon": {"Imorgon"},
}


def parse_deadline_text(card_text: str) -> Optional[str]:
    match = re.search(r"\b(Idag|Ikväll)\s+(\d{1,2}:\d{2})\b", card_text)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    if re.search(r"\bImorgon\b", card_text):
        return "Imorgon"
    if re.search(r"\bIgår\b", card_text):
        return "Igår"
    return None


def deadline_matches(deadline: str, wanted: set[str]) -> bool:
    """Return True if the deadline text matches any of the wanted keywords."""
    if not wanted:          # empty = accept all
        return True
    for kw in wanted:
        if deadline.startswith(kw):
            return True
    return False


# ---------------------------------------------------------------------------
# Card extraction helpers (unchanged from v12)
# ---------------------------------------------------------------------------

def extract_properties(properties: Iterable[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    props = [p.strip() for p in properties if p.strip()]
    year    = props[0] if len(props) > 0 else None
    mileage = props[1] if len(props) > 1 else None
    fuel    = props[2] if len(props) > 2 else None
    return year, mileage, fuel


def extract_leading_bid(card: Locator) -> Optional[str]:
    try:
        label = card.locator("xpath=.//*[contains(normalize-space(), 'Ledande bud')]")
        if label.count() == 0:
            return None
        for idx in range(label.count()):
            element = label.nth(idx)
            row_text = element.locator("xpath=ancestor-or-self::*[1]").inner_text()
            m = re.search(r"(\d{1,3}(?:[\s\u00a0]\d{3})*\s*kr)", row_text)
            if m:
                return m.group(1).replace("\u00a0", " ")
    except Exception:
        pass
    return None


def extract_location(card: Locator) -> Optional[str]:
    try:
        loc_row = card.locator(
            "xpath=.//*[name()='svg' and (contains(@aria-label, 'location') or contains(@alt, 'Location'))]/../*"
        )
        if loc_row.count() > 0:
            text = loc_row.nth(0).inner_text().strip()
            if text:
                return text
    except Exception:
        pass
    try:
        full_text = card.inner_text()
        for line in full_text.splitlines():
            line = line.strip()
            if not line:
                continue
            if re.search(r"\d", line):
                continue
            if "(" in line or "," in line or re.search(r"[ÅÄÖåäö]", line):
                return line
    except Exception:
        pass
    return None


def extract_title_and_subtitle(card: Locator) -> tuple[str, str]:
    title = ""
    subtitle = ""
    try:
        title_el    = card.locator("p[class*='Title__Container']")
        subtitle_el = card.locator("p[class*='Subtitle__Container']")
        if title_el.count() > 0:
            title = title_el.nth(0).inner_text().strip()
        if subtitle_el.count() > 0:
            subtitle = subtitle_el.nth(0).inner_text().strip()
        if title or subtitle:
            return title, subtitle
    except Exception:
        pass
    try:
        lines = [ln.strip() for ln in card.inner_text().splitlines() if ln.strip()]
        if lines:
            if re.match(r"^(Idag|Ikväll)\s+\d{1,2}:\d{2}$", lines[0]) or lines[0] in ("Imorgon", "Igår"):
                lines = lines[1:]
        skip_phrases = {"Reparationsobjekt", "Testbil", "Reservdelsbil"}
        while lines and lines[0] in skip_phrases:
            lines = lines[1:]
        if lines:
            title    = lines[0]
            subtitle = lines[1] if len(lines) > 1 else ""
    except Exception:
        pass
    return title, subtitle


def extract_condition_status(card: Locator) -> Optional[str]:
    try:
        span = card.locator("xpath=.//div[starts-with(@class, 'ConditionStatus__')]//span")
        if span.count() > 0:
            return span.nth(0).inner_text().strip()
    except Exception:
        pass
    try:
        text = card.inner_text()
        for keyword in ["Reparationsobjekt", "Testbil", "Reservdelsbil"]:
            if keyword in text:
                return keyword
    except Exception:
        pass
    return None


def fetch_ad_details(page: Page) -> tuple[Optional[str], Optional[str], Optional[str], Dict[str, dict]]:
    regnr: Optional[str] = None
    price: Optional[str] = None
    gearbox: Optional[str] = None
    report: Dict[str, dict] = {}
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        result = page.evaluate(
            """
(() => {
  const data = { regnr: null, price: null, gearbox: null, report: {} };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT, null);
  while (walker.nextNode()) {
    const el = walker.currentNode;
    const text = el && el.textContent ? el.textContent.trim() : '';
    if (!data.regnr && text === 'Registreringsnummer') {
      let container = el.parentElement;
      for (let i = 0; i < 3 && container && container.children.length < 2; i++) {
        container = container.parentElement;
      }
      if (container && container.children.length >= 2) {
        const children = Array.from(container.children);
        const idx = children.indexOf(el);
        if (idx >= 0 && idx + 1 < children.length) {
          data.regnr = children[idx + 1].textContent.trim();
        }
      }
    }
    if (!data.price && text.startsWith('Pris i bilhandeln')) {
      let container = el.parentElement;
      for (let i = 0; i < 3 && container && container.children.length < 2; i++) {
        container = container.parentElement;
      }
      if (container && container.children.length >= 2) {
        const children = Array.from(container.children);
        const idx = children.indexOf(el);
        if (idx >= 0 && idx + 1 < children.length) {
          data.price = children[idx + 1].textContent.trim();
        }
      }
    }
    if (!data.gearbox && text === 'Växellåda') {
      let container = el.parentElement;
      for (let i = 0; i < 3 && container && container.children.length < 2; i++) {
        container = container.parentElement;
      }
      if (container && container.children.length >= 2) {
        const children = Array.from(container.children);
        const idx = children.indexOf(el);
        if (idx >= 0 && idx + 1 < children.length) {
          data.gearbox = children[idx + 1].textContent.trim();
        }
      }
    }
  }
  const boxes = document.querySelectorAll('[data-testid="testGrades-car"] div[class^="TestContentBox__Container"]');
  boxes.forEach(box => {
    let sectionName = '';
    let status = '';
    let comment = '';
    const titleEl = box.querySelector('div[class*="IconAndTitle"] p');
    if (titleEl) sectionName = titleEl.textContent.trim();
    const gradeEl = box.querySelector('div[class*="GradeValue"] p');
    if (gradeEl) status = gradeEl.textContent.trim();
    const remarkContainer = box.querySelector('div[class*="TestRemarkAndQuestion__Remark"]');
    if (remarkContainer) {
      const ps = remarkContainer.querySelectorAll('p');
      if (ps.length > 1) comment = ps[1].textContent.trim();
    }
    if (sectionName) {
      data.report[sectionName] = { status: status || null, comment: comment || '' };
    }
  });
  return data;
})()
"""
        )
        regnr   = result.get("regnr")
        price   = result.get("price")
        gearbox = result.get("gearbox")
        report  = result.get("report") or {}
    except Exception:
        pass
    return regnr, price, gearbox, report


# ---------------------------------------------------------------------------
# Core scrape function
# ---------------------------------------------------------------------------

def scrape_kvd(url: str, headless: bool = True, wanted_deadlines: set[str] | None = None) -> RunResult:
    """
    Scrape KVD listings.

    Parameters
    ----------
    url:
        Full query URL including any filter parameters.
    headless:
        Whether to run Chromium in headless mode.
    wanted_deadlines:
        Set of Swedish deadline keywords to keep, e.g. {"Idag", "Ikväll"}.
        Pass None or an empty set to keep everything.
    """
    if wanted_deadlines is None:
        wanted_deadlines = set()

    items: List[Item] = []
    run_at = datetime.now(ZoneInfo("Europe/Stockholm")).isoformat()
    with sync_playwright() as p:
        print("Opening browser and navigating to the listing page...", flush=True)
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(locale="sv-SE")
        page = context.new_page()
        page.goto(url)
        try:
            consent_btn = page.locator("text=Tillåt alla")
            if consent_btn.is_visible():
                consent_btn.click()
        except Exception:
            pass
        processed = 0
        should_stop = False
        while not should_stop:
            page.wait_for_selector("a[data-testid='product-card']", timeout=20000)
            cards = page.locator("a[data-testid='product-card']")
            total_cards = cards.count()
            print(f"Found {total_cards} cards loaded. Processing new ones...", flush=True)
            for idx in range(processed, total_cards):
                card = cards.nth(idx)
                try:
                    card_text = card.inner_text()
                except Exception:
                    continue
                deadline = parse_deadline_text(card_text)
                # Cards without a recognised deadline (e.g. far future) signal end of window
                if not deadline:
                    should_stop = True
                    break
                # Skip cards that don't match the user's chosen time window
                if wanted_deadlines and not deadline_matches(deadline, wanted_deadlines):
                    processed += 1
                    continue
                try:
                    href     = card.get_attribute("href") or ""
                    full_url = href if href.startswith("http") else f"https://www.kvd.se{href}"
                except Exception:
                    full_url = ""
                title, subtitle = extract_title_and_subtitle(card)
                try:
                    props = [span.inner_text() for span in card.locator("div[data-testid='properties'] span").all()]
                    year, mileage, fuel = extract_properties(props)
                except Exception:
                    year = mileage = fuel = None
                location  = extract_location(card)
                bid       = extract_leading_bid(card)
                condition = extract_condition_status(card)
                make: Optional[str] = None
                model: Optional[str] = None
                if title:
                    parts = title.split(" ", 1)
                    if parts:
                        make  = parts[0]
                        model = parts[1] if len(parts) > 1 else ""
                processed += 1
                print(f"Processing ad {processed}: {title or 'N/A'} (deadline: {deadline})", flush=True)
                items.append(
                    Item(
                        url=full_url,
                        deadline_text=deadline,
                        title=title.strip(),
                        subtitle=subtitle.strip(),
                        year=year,
                        mileage=mileage,
                        fuel=fuel,
                        location=location,
                        leading_bid=bid,
                        regnr=None,
                        condition_status=condition,
                        price_in_dealership=None,
                        gearbox=None,
                        condition_report=None,
                        make=make,
                        model=model,
                    )
                )
            if should_stop:
                break
            # Scroll to load more cards
            attempts = 0
            loaded_more = False
            while attempts < 5 and not loaded_more:
                previous_count = cards.count()
                if previous_count > 0:
                    last_card = cards.nth(previous_count - 1)
                    try:
                        last_card.scroll_into_view_if_needed()
                    except Exception:
                        page.evaluate("el => el.scrollIntoView()", last_card)
                else:
                    page.evaluate("window.scrollBy(0, window.innerHeight)")
                page.wait_for_timeout(2000)
                cards = page.locator("a[data-testid='product-card']")
                new_count = cards.count()
                if new_count > previous_count:
                    print(f"Loaded more cards ({previous_count} -> {new_count}). Continuing...", flush=True)
                    loaded_more = True
                    break
                attempts += 1
            if not loaded_more:
                print("No more cards loaded after scrolling. Stopping.", flush=True)
                break
        # Fetch details for every collected item
        for index, item in enumerate(items, start=1):
            if not item.url:
                continue
            print(f"Fetching additional details for item {index}/{len(items)}...", flush=True)
            try:
                ad_page = context.new_page()
                ad_page.goto(item.url)
                try:
                    consent_btn = ad_page.locator("text=Tillåt alla")
                    if consent_btn.is_visible():
                        consent_btn.click()
                except Exception:
                    pass
                reg, price, gear, report_details = fetch_ad_details(ad_page)
                item.regnr               = reg
                item.price_in_dealership = price
                item.gearbox             = gear
                item.condition_report    = report_details
                ad_page.close()
            except Exception:
                pass
        browser.close()
    return RunResult(run_at=run_at, source="kvd.se", query_url=url, items=items)


# ---------------------------------------------------------------------------
# Interactive wizard
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: str = "") -> str:
    """Print a prompt and read a line, returning *default* if the user presses Enter."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return answer if answer else default


def _ask_choice(prompt: str, choices: list[str], default: str) -> str:
    """Ask the user to pick one item from *choices*. Numbers or names accepted."""
    print(f"\n{prompt}")
    for i, c in enumerate(choices, 1):
        marker = " (standard)" if c == default else ""
        print(f"  {i}. {c}{marker}")
    while True:
        raw = _ask("Välj", default)
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(choices):
                return choices[idx]
        if raw in choices:
            return raw
        # try case-insensitive match
        lower = {c.lower(): c for c in choices}
        if raw.lower() in lower:
            return lower[raw.lower()]
        print(f"  Ogiltigt val. Ange ett nummer (1-{len(choices)}) eller exakt text.")


def _ask_multi(prompt: str, choices: list[str]) -> list[str]:
    """Ask the user to pick one or more items (comma-separated). Empty = all."""
    print(f"\n{prompt}")
    for i, c in enumerate(choices, 1):
        print(f"  {i}. {c}")
    print("  (Tryck Enter utan val för att välja ALLA, kommaseparera för flera)")
    raw = _ask("Välj (t.ex. 1,3)", "")
    if not raw:
        return []
    selected: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(choices):
                selected.append(choices[idx])
        elif part in choices:
            selected.append(part)
    return selected


def build_url_interactive() -> tuple[str, set[str]]:
    """
    Guide the user through filter selection and return
    (full_url, wanted_deadline_keywords).
    """
    BASE = "https://www.kvd.se/begagnade-bilar"

    print("\n" + "=" * 60)
    print("  KVD Scraper – Filterguiden")
    print("=" * 60)
    print("Tryck Enter på valfri fråga för att hoppa över filtret.")

    params: list[tuple[str, str]] = []  # list of (key, value) to support duplicates

    # --- Time window ---
    time_choice = _ask_choice(
        "Vilken tidsperiod vill du hämta?",
        ["Idag/Ikväll", "Imorgon", "Igår", "Alla (ingen begränsning)"],
        default="Idag/Ikväll",
    )
    wanted: set[str] = set()
    if time_choice == "Idag/Ikväll":
        wanted = {"Idag", "Ikväll"}
        # Keep default sort so today's auctions appear first
        params.append(("orderBy", "countdown_start_at"))
    elif time_choice == "Imorgon":
        wanted = {"Imorgon"}
        params.append(("orderBy", "countdown_start_at"))
    elif time_choice == "Igår":
        wanted = {"Igår"}
    # else: empty = all

    # --- Brand(s) ---
    BRANDS = [
        "Audi", "BMW", "Ford", "Hyundai", "Mazda", "Mercedes",
        "Nissan", "Opel", "Peugeot", "Porsche", "Renault", "Seat",
        "Skoda", "Subaru", "Tesla", "Toyota", "Volkswagen", "Volvo",
    ]
    brands = _ask_multi("Märke (tomt = alla märken):", BRANDS)
    for b in brands:
        params.append(("brand", b))

    # --- Model ---
    model_input = _ask("\nModell (t.ex. V60, Golf, X5) – lämna tomt för alla", "")
    if model_input:
        params.append(("familyName", model_input))

    # --- Fuel ---
    fuel_choice = _ask_choice(
        "Drivmedel:",
        ["Diesel", "Bensin", "El", "Hybrid", "Alla"],
        default="Alla",
    )
    if fuel_choice != "Alla":
        params.append(("fuel", fuel_choice))

    # --- Auction type ---
    auction_choice = _ask_choice(
        "Köpmetod:",
        ["BUY_NOW (fast pris)", "BIDDING (budgivning)", "Alla"],
        default="Alla",
    )
    if auction_choice.startswith("BUY_NOW"):
        params.append(("auctionType", "BUY_NOW"))
    elif auction_choice.startswith("BIDDING"):
        params.append(("auctionType", "BIDDING"))

    # --- Gearbox ---
    gear_choice = _ask_choice(
        "Växellåda:",
        ["Manuell", "Automat", "Alla"],
        default="Alla",
    )
    if gear_choice != "Alla":
        params.append(("gearbox", gear_choice))

    # --- Sort ---
    sort_choice = _ask_choice(
        "Sortera efter:",
        ["countdown_start_at (auktionsslut, standard)", "price (pris)", "year (årsmodell)", "mileage (miltal)", "published (publiceringsdatum)"],
        default="countdown_start_at (auktionsslut, standard)",
    )
    sort_key = sort_choice.split(" ")[0]
    # Only add if not already added above
    existing_sort = [v for k, v in params if k == "orderBy"]
    if not existing_sort:
        params.append(("orderBy", sort_key))
    else:
        # Replace the already appended default
        params = [(k, v) if k != "orderBy" else ("orderBy", sort_key) for k, v in params]

    sort_order_choice = _ask_choice(
        "Sorteringsordning:",
        ["asc (stigande)", "desc (fallande)"],
        default="asc (stigande)",
    )
    sort_order = sort_order_choice.split(" ")[0]
    if sort_order == "desc":
        params.append(("sortOrder", "desc"))

    # Build URL
    if params:
        query_string = "&".join(f"{k}={v}" for k, v in params)
        url = f"{BASE}?{query_string}"
    else:
        url = BASE

    print(f"\nKonstruerad URL:\n  {url}")
    if wanted:
        print(f"Tidsfilter (på klientsidan): {', '.join(sorted(wanted))}")
    print()
    return url, wanted


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scrape Kvdbil auctions with automatic detail extraction and brand/model parsing."
    )
    parser.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=True,
        help="Run the browser in headless mode (default).",
    )
    parser.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="Run the browser with a visible UI (for debugging).",
    )
    parser.add_argument(
        "--no-interactive",
        dest="interactive",
        action="store_false",
        default=True,
        help="Skip the interactive wizard and use default settings (same as v12).",
    )
    # Allow passing a custom URL directly
    parser.add_argument(
        "--url",
        dest="url",
        default=None,
        help="Use this URL directly (implies --no-interactive).",
    )
    args = parser.parse_args(argv)

    if args.url:
        # Explicit URL – skip wizard, collect everything
        query_url        = args.url
        wanted_deadlines: set[str] = set()
    elif args.interactive:
        query_url, wanted_deadlines = build_url_interactive()
    else:
        # Legacy default behaviour
        query_url        = "https://www.kvd.se/begagnade-bilar?orderBy=countdown_start_at"
        wanted_deadlines = set()

    result = scrape_kvd(query_url, headless=args.headless, wanted_deadlines=wanted_deadlines)
    output = {
        "run_at":    result.run_at,
        "source":    result.source,
        "query_url": result.query_url,
        "items":     [asdict(item) for item in result.items],
    }
    safe_timestamp = result.run_at.replace(":", "-")
    results_dir = Path(__file__).parent / Path(__file__).stem / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    filepath = results_dir / f"kvd_result_{safe_timestamp}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\nResultat sparat i fil: {filepath}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
