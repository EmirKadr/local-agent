"""Scrape Kvdbil auctions with brand and model extraction.

This script builds on kvd_scraper10.py and includes two new fields in
the output: `make` and `model`. Each car's title is assumed to
follow the pattern "MÄRKE MODELL" (e.g. "Porsche Taycan"), where
the first word represents the brand and the remainder represents
the model. These fields are derived automatically from the title
and included in the JSON output for convenience.

As before, the scraper collects all auctions with deadlines of
today/tonight or tomorrow, then visits each ad to fetch the
registration number, dealer price, gearbox, and a detailed
condition report.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Iterable, List, Optional, Dict

from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, Page, Locator


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


def parse_deadline_text(card_text: str) -> Optional[str]:
    match = re.search(r"\b(Idag|Ikväll)\s+(\d{1,2}:\d{2})\b", card_text)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    if re.search(r"\bImorgon\b", card_text):
        return "Imorgon"
    return None


def extract_properties(properties: Iterable[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    props = [p.strip() for p in properties if p.strip()]
    year = props[0] if len(props) > 0 else None
    mileage = props[1] if len(props) > 1 else None
    fuel = props[2] if len(props) > 2 else None
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
    """
    Extract the main title and subtitle from an auction card. This function
    first attempts to locate title and subtitle using stable class name
    prefixes ("Title__Container" and "Subtitle__Container"). If that fails,
    it falls back to text-based heuristics while skipping overlay tags
    such as the condition status (e.g., "Reparationsobjekt").
    """
    title = ""
    subtitle = ""
    # Try robust selection based on class prefix
    try:
        title_el = card.locator("p[class*='Title__Container']")
        subtitle_el = card.locator("p[class*='Subtitle__Container']")
        if title_el.count() > 0:
            title = title_el.nth(0).inner_text().strip()
        if subtitle_el.count() > 0:
            subtitle = subtitle_el.nth(0).inner_text().strip()
        if title or subtitle:
            return title, subtitle
    except Exception:
        pass
    # Fallback to previous heuristic but skip known overlay/status lines
    try:
        lines = [ln.strip() for ln in card.inner_text().splitlines() if ln.strip()]
        # Remove initial deadline line if present
        if lines:
            if re.match(r"^(Idag|Ikväll)\s+\d{1,2}:\d{2}$", lines[0]) or lines[0] == "Imorgon":
                lines = lines[1:]
        # Skip common status indicators at the start
        skip_phrases = {"Reparationsobjekt", "Testbil", "Reservdelsbil"}
        while lines and lines[0] in skip_phrases:
            lines = lines[1:]
        if lines:
            title = lines[0]
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
    """
    Extract details from an individual auction page: registration number,
    dealership price, gearbox and the full condition report with
    status and comments for each inspection section.
    """
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
        for (const child of children) {
          if (child !== el && child.textContent) {
            const value = child.textContent.trim();
            if (/kr$/.test(value)) {
              data.price = value;
              break;
            }
          }
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
        regnr = result.get("regnr")
        price = result.get("price")
        gearbox = result.get("gearbox")
        report = result.get("report") or {}
    except Exception:
        pass
    return regnr, price, gearbox, report


def scrape_kvd(url: str, headless: bool = True) -> RunResult:
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
                if not deadline:
                    should_stop = True
                    break
                try:
                    href = card.get_attribute("href") or ""
                    full_url = href if href.startswith("http") else f"https://www.kvd.se{href}"
                except Exception:
                    full_url = ""
                title, subtitle = extract_title_and_subtitle(card)
                try:
                    props = [span.inner_text() for span in card.locator("div[data-testid='properties'] span").all()]
                    year, mileage, fuel = extract_properties(props)
                except Exception:
                    year = mileage = fuel = None
                location = extract_location(card)
                bid = extract_leading_bid(card)
                condition = extract_condition_status(card)
                # Derive make and model from title
                make: Optional[str] = None
                model: Optional[str] = None
                if title:
                    parts = title.split(" ", 1)
                    if parts:
                        make = parts[0]
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
        # Always fetch details for each item
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
                item.regnr = reg
                item.price_in_dealership = price
                item.gearbox = gear
                item.condition_report = report_details
                ad_page.close()
            except Exception:
                pass
        browser.close()
    return RunResult(run_at=run_at, source="kvd.se", query_url=url, items=items)


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
    args = parser.parse_args(argv)
    query_url = "https://www.kvd.se/begagnade-bilar?orderBy=countdown_start_at"
    result = scrape_kvd(query_url, headless=args.headless)
    output = {
        "run_at": result.run_at,
        "source": result.source,
        "query_url": result.query_url,
        "items": [asdict(item) for item in result.items],
    }
    safe_timestamp = result.run_at.replace(":", "-")
    filename = f"kvd_result_{safe_timestamp}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\nResultat sparat i fil: {filename}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())