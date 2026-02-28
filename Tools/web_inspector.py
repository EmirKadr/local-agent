"""
web_inspector.py
----------------
Hämtar HTML från en given URL, analyserar sidans struktur och
returnerar en förklaring av hur sidan fungerar och vad som finns där.

Beroenden: requests, beautifulsoup4
Playwright används automatiskt som fallback om sidan kräver JavaScript.

Input:
  url        (str)  – Sidan att inspektera
  headless   (bool) – Playwright headless-läge (default True)
  use_ai     (bool) – Använd Claude för att analysera HTML (default True)
  write_file (bool) – Spara resultat till fil (default False)

Output:
  {
    "url": str,
    "fetched_at": str,
    "fetch_method": "requests" | "playwright",
    "structure": { title, meta, headings, links, forms, ... },
    "ai_summary": str | null,
    "out_file": str | null
  }
"""

import sys
import os
import json
import re
import time
import traceback
import argparse
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Konstanter
# --------------------------------------------------------------------------- #
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "web_inspector", "results")
MAX_HTML_CHARS = 40_000   # trunkera HTML innan AI-analys
REQUEST_TIMEOUT = 20


# --------------------------------------------------------------------------- #
# Hämtning
# --------------------------------------------------------------------------- #

def _fetch_requests(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
        ),
        "Accept-Language": "sv,en;q=0.9",
    }
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _fetch_playwright(url: str, headless: bool) -> tuple[str, list[dict]]:
    """Hämtar sida med Playwright och fångar XHR/fetch API-anrop via nätverksinterceptering."""
    from playwright.sync_api import sync_playwright

    captured: list[dict] = []
    response_bodies: dict[str, str] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
            )
        )
        page = context.new_page()

        def on_request(req):
            if req.resource_type in ("xhr", "fetch"):
                captured.append({
                    "method": req.method,
                    "url": req.url[:600],
                    "resource_type": req.resource_type,
                    "post_data": (req.post_data or "")[:400] or None,
                })

        def on_response(resp):
            if resp.request.resource_type in ("xhr", "fetch"):
                try:
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct or "graphql" in ct:
                        body = resp.body().decode("utf-8", errors="replace")[:2000]
                        response_bodies[resp.url] = body
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            page.goto(url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(2000)
        except Exception:
            pass

        html = page.content()
        browser.close()

    api_calls = []
    for call in captured[:30]:
        body = response_bodies.get(call["url"])
        if body:
            call["response_preview"] = body
        api_calls.append(call)

    return html, api_calls


def _scan_scripts_for_api_patterns(html: str) -> list[dict]:
    """Skannar inbäddade <script>-taggar efter API-URL-mönster (fetch, axios, REST-paths)."""
    soup = BeautifulSoup(html, "html.parser")
    patterns: list[dict] = []
    seen: set[str] = set()

    fetch_re   = re.compile(r'fetch\s*\(\s*[`"\']([^`"\']{5,300})[`"\']', re.I)
    axios_re   = re.compile(r'axios\.(get|post|put|delete|patch)\s*\(\s*[`"\']([^`"\']{5,300})[`"\']', re.I)
    api_url_re = re.compile(
        r'[`"\'](\/?(?:api|v\d+|graphql|rest|data|search|query|json)[^`"\']{0,200})[`"\']', re.I
    )

    for script in soup.find_all("script"):
        code = script.string or ""
        if len(code) < 20:
            continue
        for m in fetch_re.finditer(code):
            u = m.group(1).strip()
            if u not in seen and not u.startswith("//"):
                seen.add(u)
                patterns.append({"source": "fetch()", "url": u})
        for m in axios_re.finditer(code):
            method, u = m.group(1).upper(), m.group(2).strip()
            if u not in seen:
                seen.add(u)
                patterns.append({"source": "axios", "method": method, "url": u})
        for m in api_url_re.finditer(code):
            u = m.group(1).strip()
            if u not in seen and len(u) > 5:
                seen.add(u)
                patterns.append({"source": "js_pattern", "url": u})

    return patterns[:25]


def fetch_html(url: str, headless: bool = True) -> tuple[str, str, list]:
    """Returnerar (html, metod, api_calls). api_calls är tom lista om requests används."""
    try:
        html = _fetch_requests(url)
        # Om sidan verkar vara en tom SPA (litet body-innehåll), använd playwright
        soup = BeautifulSoup(html, "html.parser")
        body_text = soup.get_text(separator=" ").strip()
        if len(body_text) < 200:
            raise ValueError("Sidan verkar kräva JavaScript – byter till Playwright")
        return html, "requests", []
    except Exception as e:
        print(f"[requests] {e} → försöker med Playwright...", file=sys.stderr)
        html, api_calls = _fetch_playwright(url, headless)
        return html, "playwright", api_calls


# --------------------------------------------------------------------------- #
# HTML-analys
# --------------------------------------------------------------------------- #

def _text(el) -> str:
    return el.get_text(separator=" ", strip=True)[:200] if el else ""


def parse_structure(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # ----- Titel & meta -----
    title = _text(soup.find("title"))
    meta = {}
    for m in soup.find_all("meta"):
        name = m.get("name") or m.get("property") or m.get("http-equiv")
        content = m.get("content")
        if name and content:
            meta[name.lower()] = content[:300]

    # ----- Rubriker -----
    headings = {}
    for level in range(1, 4):
        headings[f"h{level}"] = [_text(h) for h in soup.find_all(f"h{level}")][:15]

    # ----- Navigering -----
    navs = []
    for nav in soup.find_all("nav"):
        links = [a.get_text(strip=True) for a in nav.find_all("a")][:20]
        navs.append({"links": links})

    # ----- Länkar -----
    all_links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("#") or href in seen:
            continue
        seen.add(href)
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        all_links.append({
            "text": a.get_text(strip=True)[:80],
            "href": absolute,
            "external": parsed.netloc != urlparse(base_url).netloc,
        })
    internal_links = [l for l in all_links if not l["external"]][:30]
    external_links = [l for l in all_links if l["external"]][:20]

    # ----- Formulär -----
    forms = []
    for form in soup.find_all("form"):
        action = form.get("action", "")
        method = form.get("method", "get").upper()
        fields = []
        for inp in form.find_all(["input", "textarea", "select", "button"]):
            t = inp.get("type", inp.name)
            n = inp.get("name") or inp.get("id") or inp.get("placeholder") or ""
            fields.append({"type": t, "name": n[:60]})
        forms.append({"action": action, "method": method, "fields": fields})

    # ----- Bilder -----
    images = []
    for img in soup.find_all("img")[:20]:
        images.append({
            "src": img.get("src", "")[:200],
            "alt": img.get("alt", "")[:100],
        })

    # ----- Scripts & styles -----
    scripts = [s.get("src", "inline")[:150] for s in soup.find_all("script") if s.get("src")][:15]
    stylesheets = [l["href"][:150] for l in soup.find_all("link", rel="stylesheet") if l.get("href")][:10]

    # ----- Övrigt innehåll -----
    main_text_blocks = []
    for tag in ["main", "article", "section", "div"]:
        for el in soup.find_all(tag)[:5]:
            t = el.get_text(separator=" ", strip=True)
            if len(t) > 100:
                main_text_blocks.append(t[:500])
        if main_text_blocks:
            break

    return {
        "title": title,
        "meta": meta,
        "headings": headings,
        "navigation": navs,
        "internal_links": internal_links,
        "external_links": external_links,
        "forms": forms,
        "images": images,
        "scripts_external": scripts,
        "stylesheets": stylesheets,
        "main_text_sample": main_text_blocks[:5],
        "total_links": len(all_links),
        "total_images": len(soup.find_all("img")),
        "total_scripts": len(soup.find_all("script")),
        "total_forms": len(soup.find_all("form")),
    }


# --------------------------------------------------------------------------- #
# AI-analys med Claude
# --------------------------------------------------------------------------- #

def _lm_chat(messages: list[dict], max_tokens: int = 2048) -> str:
    """Anropar lokal LLM via OpenAI-kompatibelt API."""
    lm_base  = os.environ.get("OPENAI_API_BASE", "http://127.0.0.1:1234/v1")
    lm_model = os.environ.get("LM_MODEL", "meta-llama/llama-3.3-70b-instruct")
    resp = requests.post(
        f"{lm_base}/chat/completions",
        json={"model": lm_model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def ai_analyze(
    url: str,
    structure: dict,
    html: str,
    api_calls: list | None = None,
    api_patterns: list | None = None,
) -> str:
    # Kondensera HTML för kontext
    html_preview = html[:MAX_HTML_CHARS]
    if len(html) > MAX_HTML_CHARS:
        html_preview += f"\n... [trunkerad, {len(html):,} tecken totalt]"

    structure_json = json.dumps(structure, ensure_ascii=False, indent=2)

    # Bygg API-sektion om endpoints hittats
    api_section = ""
    if api_calls:
        api_json = json.dumps(api_calls[:15], ensure_ascii=False, indent=2)
        api_section += f"\n\n--- DETEKTERADE API-ANROP (XHR/fetch, live-fångade) ---\n{api_json}"
    if api_patterns:
        pat_json = json.dumps(api_patterns[:15], ensure_ascii=False, indent=2)
        api_section += f"\n\n--- API-MÖNSTER (statisk JS-analys) ---\n{pat_json}"

    messages = [
        {
            "role": "system",
            "content": "Du är en webbutvecklare och UX-analytiker. Svara på svenska med tydliga rubriker.",
        },
        {
            "role": "user",
            "content": f"""Analysera nedanstående webbsida och förklara:

1. **Vad sidan handlar om** – syfte och målgrupp
2. **Hur sidan är uppbyggd** – layout, sektioner, navigering
3. **Vilka funktioner finns** – formulär, sökfält, knappar, inloggning, etc.
4. **Teknisk stack (om möjligt)** – ramverk, CMS, tredjepartstjänster
5. **Innehåll** – vad för information/produkter/tjänster erbjuds
6. **API-endpoints** – om API-anrop hittats, förklara vad de gör och hur de kan användas direkt
7. **Intressanta observationer** – t.ex. cookiebanner, tracking, SEO-signaler

URL: {url}

--- PARSAD STRUKTUR ---
{structure_json}{api_section}

--- HTML-FÖRHANDSVISNING ---
{html_preview}""",
        },
    ]
    return _lm_chat(messages, max_tokens=2048)


# --------------------------------------------------------------------------- #
# Spara resultat
# --------------------------------------------------------------------------- #

def save_result(result: dict) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    domain = urlparse(result["url"]).netloc.replace(".", "_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(RESULTS_DIR, f"{domain}_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return path


# --------------------------------------------------------------------------- #
# Huvudfunktion
# --------------------------------------------------------------------------- #

def run(
    url: str,
    headless: bool = True,
    use_ai: bool = True,
    write_file: bool = False,
) -> dict:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    print(f"[web_inspector] Hämtar {url} ...", file=sys.stderr)
    html, method, api_calls = fetch_html(url, headless=headless)
    print(
        f"[web_inspector] Hämtad via {method} ({len(html):,} tecken HTML, "
        f"{len(api_calls)} API-anrop fångade)",
        file=sys.stderr,
    )

    structure = parse_structure(html, base_url=url)

    # Statisk JS-scanning (körs alltid, oavsett hämtmetod)
    api_patterns = _scan_scripts_for_api_patterns(html)
    if api_patterns:
        print(f"[web_inspector] {len(api_patterns)} API-mönster hittade via JS-scanning", file=sys.stderr)

    ai_summary = None
    if use_ai:
        print("[web_inspector] Analyserar med Claude ...", file=sys.stderr)
        try:
            ai_summary = ai_analyze(url, structure, html, api_calls=api_calls, api_patterns=api_patterns)
        except Exception as e:
            ai_summary = f"[AI-analys misslyckades: {e}]"
            print(f"[web_inspector] {ai_summary}", file=sys.stderr)

    result = {
        "url": url,
        "fetched_at": datetime.now().isoformat(),
        "fetch_method": method,
        "structure": structure,
        "api_calls": api_calls,
        "api_patterns": api_patterns,
        "ai_summary": ai_summary,
        "out_file": None,
    }

    if write_file:
        path = save_result(result)
        result["out_file"] = path
        print(f"[web_inspector] Sparad till {path}", file=sys.stderr)

    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Inspekterar en webbsidas HTML och förklarar hur den fungerar."
    )
    parser.add_argument("url", help="URL att inspektera (t.ex. https://example.com)")
    parser.add_argument("--no-ai", action="store_true", help="Hoppa över AI-analys")
    parser.add_argument("--no-headless", action="store_true", help="Visa webbläsarfönster (Playwright)")
    parser.add_argument("--write-file", action="store_true", help="Spara resultat som JSON-fil")
    parser.add_argument("--json", action="store_true", help="Skriv ut hela JSON-resultatet")
    args = parser.parse_args()

    result = run(
        url=args.url,
        headless=not args.no_headless,
        use_ai=not args.no_ai,
        write_file=args.write_file,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        s = result["structure"]
        print(f"\n{'='*60}")
        print(f"  {result['url']}")
        print(f"  Hämtad: {result['fetched_at']}  |  Metod: {result['fetch_method']}")
        print(f"{'='*60}")
        print(f"Titel      : {s['title']}")
        print(f"Beskrivning: {s['meta'].get('description', '–')[:120]}")
        print(f"Totalt     : {s['total_links']} länkar, {s['total_images']} bilder, "
              f"{s['total_scripts']} scripts, {s['total_forms']} formulär")

        if s["headings"]["h1"]:
            print(f"\nH1-rubriker:")
            for h in s["headings"]["h1"][:5]:
                print(f"  • {h}")

        if s["forms"]:
            print(f"\nFormulär ({len(s['forms'])} st):")
            for form in s["forms"]:
                fields = ", ".join(f["type"] for f in form["fields"])
                print(f"  • {form['method']} {form['action'] or '/'} [{fields}]")

        api_calls = result.get("api_calls", [])
        api_patterns = result.get("api_patterns", [])
        if api_calls:
            print(f"\nAPI-anrop (live-fångade, {len(api_calls)} st):")
            for call in api_calls[:10]:
                preview = f"  [{call['method']}] {call['url']}"
                if call.get("post_data"):
                    preview += f"  body={call['post_data'][:60]}"
                if call.get("response_preview"):
                    preview += f"  → {call['response_preview'][:80]}"
                print(preview)
        if api_patterns:
            print(f"\nAPI-mönster (JS-analys, {len(api_patterns)} st):")
            for p in api_patterns[:10]:
                method = p.get("method", "GET") if p["source"] != "js_pattern" else ""
                tag = f"[{method}] " if method else ""
                print(f"  {tag}{p['url']}  ({p['source']})")

        if result["ai_summary"]:
            print(f"\n{'─'*60}")
            print("AI-ANALYS:")
            print(f"{'─'*60}")
            print(result["ai_summary"])

        if result["out_file"]:
            print(f"\n[Resultat sparat: {result['out_file']}]")


if __name__ == "__main__":
    if os.environ.get("LOCAL_AGENT_TOOL_MODE") == "1":
        # Runner-läge: läs JSON från stdin, skriv JSON till stdout
        import json as _json
        data = _json.loads(sys.stdin.read())
        result = run(
            url=data["url"],
            headless=bool(data.get("headless", True)),
            use_ai=bool(data.get("use_ai", True)),
            write_file=bool(data.get("write_file", False)),
        )
        print(_json.dumps(result, ensure_ascii=False))
    else:
        main()
