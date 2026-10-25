"""
Apple Store inventory checker (iPhone Duo and friends).

Finds same-day / next-day in-store pickup availability at Apple Stores near a
postal code, in the spirit of appleinventorycheck.com.

How it works:
  1. The SKU list for a model is scraped from Apple's own buy page
     (https://www.apple.com/{cc}/shop/buy-iphone/{slug}), which embeds every
     part number with its name and price. This keeps part numbers correct for
     every country without a hand-maintained table.
  2. Availability comes from Apple's retail pickup endpoint
     (/shop/retail/pickup-message), which takes up to 20 parts per request and
     returns the ~12 nearest stores with a pickup quote per part.

Routes:
  GET /inventory                      the web UI
  GET /api/inventory/options          countries + models
  GET /api/inventory/skus             SKUs for a model in a country
  GET /api/inventory/check            store availability near a postal code
"""

import asyncio
import json
import re
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

router = APIRouter()

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)

# key -> (label, apple.com path prefix, example postal code)
COUNTRIES: Dict[str, Tuple[str, str, str]] = {
    "us": ("United States", "", "10001"),
    "ca": ("Canada", "/ca", "M5V 2T6"),
    "uk": ("United Kingdom", "/uk", "SW1A 1AA"),
    "au": ("Australia", "/au", "2000"),
    "de": ("Germany", "/de", "10115"),
    "fr": ("France", "/fr", "75001"),
    "it": ("Italy", "/it", "20121"),
    "es": ("Spain", "/es", "28013"),
    "nl": ("Netherlands", "/nl", "1012 AB"),
    "be": ("Belgium", "/be-fr", "1000"),
    "at": ("Austria", "/at", "1010"),
    "ch": ("Switzerland", "/ch-de", "8001"),
    "se": ("Sweden", "/se", "11157"),
    "tr": ("Turkey", "/tr", "34367"),
    "ae": ("UAE", "/ae", "Dubai"),
    "in": ("India", "/in", "400051"),
    "jp": ("Japan", "/jp", "100-0005"),
    "kr": ("South Korea", "/kr", "06035"),
    "cn": ("China", "/cn", "200000"),
    "hk": ("Hong Kong", "/hk", "Central"),
    "tw": ("Taiwan", "/tw", "110"),
    "sg": ("Singapore", "/sg", "238859"),
    "th": ("Thailand", "/th-en", "10330"),
    "my": ("Malaysia", "/my", "50088"),
    "mx": ("Mexico", "/mx", "06600"),
}

# key -> (label, buy page slug). A buy page can cover several variants
# (e.g. the 18 Pro page also lists the Pro Max); the UI filters by variant.
MODELS: Dict[str, Tuple[str, str]] = {
    "iphone-duo": ("iPhone Duo", "iphone-duo"),
    "iphone-18-pro": ("iPhone 18 Pro / Pro Max", "iphone-18-pro"),
    "iphone-air": ("iPhone Air", "iphone-air"),
    "iphone-17": ("iPhone 17", "iphone-17"),
    "iphone-17e": ("iPhone 17e", "iphone-17e"),
    "iphone-16": ("iPhone 16", "iphone-16"),
}

PARTS_PER_REQUEST = 20  # Apple silently drops parts beyond this
SKU_TTL = 6 * 3600
CHECK_TTL = 30  # never hit Apple more than twice a minute for the same search

_PRODUCTS_RE = re.compile(r'"products":(\[\{"sku".*?\}\])')
_CURRENCY_RE = re.compile(r'"currencyCode":"([A-Z]{3})"')
_NAME_RE = re.compile(r"^(?P<model>.+?)\s+(?P<storage>\d+\s?(?:GB|TB))\s+(?P<color>.+)$")

_sku_cache: Dict[Tuple[str, str], Tuple[float, List[dict]]] = {}
_check_cache: Dict[Tuple[str, str, str], Tuple[float, dict]] = {}


def _http_get(url: str, accept: str = "*/*", attempts: int = 4) -> str:
    """GET with a browser UA, retrying Apple's transient throttling errors (541/503/429)."""
    last: Optional[Exception] = None
    for i in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 541):
                raise
        except urllib.error.URLError as e:
            last = e
        time.sleep(0.7 * (2 ** i))
    raise last  # type: ignore[misc]


def _parse_name(name: str) -> dict:
    m = _NAME_RE.match(name.strip())
    if not m:
        return {"variant": name, "storage": "", "color": ""}
    return {"variant": m["model"], "storage": m["storage"].replace(" ", ""), "color": m["color"]}


def _storage_rank(storage: str) -> int:
    m = re.match(r"(\d+)(GB|TB)", storage)
    if not m:
        return 0
    return int(m[1]) * (1024 if m[2] == "TB" else 1)


def _fetch_skus(country: str, model: str) -> List[dict]:
    key = (country, model)
    hit = _sku_cache.get(key)
    if hit and time.time() - hit[0] < SKU_TTL:
        return hit[1]

    prefix = COUNTRIES[country][1]
    slug = MODELS[model][1]
    html = _http_get(f"https://www.apple.com{prefix}/shop/buy-iphone/{slug}", accept="text/html")
    m = _PRODUCTS_RE.search(html)
    if not m:
        raise HTTPException(502, f"Couldn't find {MODELS[model][0]} on Apple's {COUNTRIES[country][0]} store")

    skus = []
    seen = set()
    for p in json.loads(m.group(1)):
        part = p.get("partNumber")
        if not part or part in seen:
            continue
        seen.add(part)
        name = " ".join((p.get("name") or part).split())  # Apple uses non-breaking spaces
        skus.append({
            "part": part,
            "name": name,
            "price": (p.get("price") or {}).get("fullPrice"),
            **_parse_name(name),
        })
    currency = _CURRENCY_RE.search(html)
    for s in skus:
        s["currency"] = currency.group(1) if currency else None
    skus.sort(key=lambda s: (s["variant"], _storage_rank(s["storage"]), s["color"]))
    _sku_cache[key] = (time.time(), skus)
    return skus


def _fetch_pickup(country: str, parts: List[str], location: str) -> dict:
    prefix = COUNTRIES[country][1]
    params = {"pl": "true", "location": location}
    for i, part in enumerate(parts):
        params[f"parts.{i}"] = part
    url = f"https://www.apple.com{prefix}/shop/retail/pickup-message?{urlencode(params)}"
    return json.loads(_http_get(url, accept="application/json"))


async def _check(country: str, model: str, location: str) -> dict:
    skus = await asyncio.to_thread(_fetch_skus, country, model)
    parts = [s["part"] for s in skus]
    batches = [parts[i:i + PARTS_PER_REQUEST] for i in range(0, len(parts), PARTS_PER_REQUEST)]
    # Sequential on purpose: parallel requests trip Apple's throttling (HTTP 541).
    responses = []
    for i, batch in enumerate(batches):
        if i:
            await asyncio.sleep(0.4)
        responses.append(await asyncio.to_thread(_fetch_pickup, country, batch, location))

    stores: Dict[str, dict] = {}
    error = None
    for data in responses:
        body = data.get("body") or {}
        if not body.get("stores"):
            error = error or body.get("errorMessage")
        for s in body.get("stores") or []:
            num = s.get("storeNumber") or s.get("storeName")
            store = stores.setdefault(num, {
                "number": num,
                "name": s.get("storeName"),
                "city": s.get("city"),
                "state": s.get("state"),
                "address": (s.get("address") or {}).get("address2"),
                "distance": s.get("storedistance"),
                "distanceText": s.get("storeDistanceWithUnit"),
                "phone": s.get("phoneNumber"),
                "url": s.get("reservationUrl"),
                "lat": s.get("storelatitude"),
                "lng": s.get("storelongitude"),
                "parts": {},
            })
            for part, pa in (s.get("partsAvailability") or {}).items():
                store["parts"][part] = {
                    "available": pa.get("pickupDisplay") == "available",
                    "quote": pa.get("pickupSearchQuote") or "",
                }

    if not stores:
        raise HTTPException(400, error or "No Apple Stores found near that location. Check the postal code.")

    prefix = COUNTRIES[country][1]
    return {
        "country": country,
        "model": model,
        "modelLabel": MODELS[model][0],
        "location": location,
        "buyUrl": f"https://www.apple.com{prefix}/shop/buy-iphone/{MODELS[model][1]}",
        "checkedAt": int(time.time()),
        "skus": skus,
        "stores": sorted(stores.values(), key=lambda s: (s["distance"] is None, s["distance"] or 0)),
    }


@router.get("/api/inventory/options")
async def inventory_options():
    return {
        "countries": [{"key": k, "label": v[0], "example": v[2]} for k, v in COUNTRIES.items()],
        "models": [{"key": k, "label": v[0]} for k, v in MODELS.items()],
    }


def _validate(country: str, model: str) -> None:
    if country not in COUNTRIES:
        raise HTTPException(400, f"Unsupported country: {country}")
    if model not in MODELS:
        raise HTTPException(400, f"Unsupported model: {model}")


@router.get("/api/inventory/skus")
async def inventory_skus(country: str = "us", model: str = "iphone-duo"):
    _validate(country, model)
    try:
        return {"skus": await asyncio.to_thread(_fetch_skus, country, model)}
    except (urllib.error.URLError, TimeoutError) as e:
        raise HTTPException(502, f"Apple's store didn't respond: {e}")


@router.get("/api/inventory/check")
async def inventory_check(location: str, country: str = "us", model: str = "iphone-duo"):
    _validate(country, model)
    location = location.strip()
    if not location or len(location) > 64:
        raise HTTPException(400, "Enter a postal code or city")

    key = (country, model, location.upper())
    hit = _check_cache.get(key)
    if hit and time.time() - hit[0] < CHECK_TTL:
        return {**hit[1], "cached": True}

    try:
        result = await _check(country, model, location)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise HTTPException(502, f"Apple's store didn't respond, try again in a moment ({e})")
    _check_cache[key] = (time.time(), result)
    return result


@router.get("/inventory", response_class=HTMLResponse)
@router.get("/duo", response_class=HTMLResponse)
async def inventory_page():
    return INVENTORY_PAGE


INVENTORY_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iPhone Duo Stock Finder</title>
<style>
  :root {
    --bg: #0b0b0c; --panel: #161618; --panel2: #1e1e21; --line: #2a2a2e;
    --text: #f2f2f4; --muted: #9a9aa2; --accent: #2997ff;
    --ok: #30d158; --ok-bg: rgba(48,209,88,.12); --no: #6e6e76;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 15px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif; }
  main { max-width: 980px; margin: 0 auto; padding: 28px 16px 64px; }
  h1 { font-size: 30px; margin: 0 0 4px; letter-spacing: -.02em; }
  .sub { color: var(--muted); margin: 0 0 22px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 16px; }
  form.search { display: grid; grid-template-columns: 1.2fr 1fr 1fr auto; gap: 10px; align-items: end; }
  label { display: block; font-size: 12px; color: var(--muted); margin: 0 0 5px; text-transform: uppercase; letter-spacing: .05em; }
  select, input { width: 100%; background: var(--panel2); color: var(--text); border: 1px solid var(--line);
                  border-radius: 10px; padding: 10px 12px; font: inherit; }
  select:focus, input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  button { background: var(--accent); color: #fff; border: 0; border-radius: 10px; padding: 11px 18px;
           font: inherit; font-weight: 600; cursor: pointer; white-space: nowrap; }
  button:disabled { opacity: .5; cursor: default; }
  button.ghost { background: transparent; border: 1px solid var(--line); color: var(--text); font-weight: 500; }
  .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .filters { margin-top: 14px; display: flex; flex-direction: column; gap: 10px; }
  .chip { border: 1px solid var(--line); background: var(--panel2); color: var(--text); border-radius: 999px;
          padding: 6px 12px; font-size: 13px; font-weight: 500; }
  .chip.on { background: var(--text); color: #000; border-color: var(--text); }
  .flabel { font-size: 12px; color: var(--muted); width: 64px; }
  .watch { margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--line); gap: 12px; }
  .watch label { display: inline; text-transform: none; letter-spacing: 0; font-size: 14px; color: var(--text); margin: 0; }
  .watch input[type=checkbox] { width: auto; }
  .watch select { width: auto; padding: 6px 8px; }
  .status { color: var(--muted); font-size: 13px; margin-left: auto; }
  .banner { margin: 18px 0 12px; padding: 14px 16px; border-radius: 14px; font-weight: 600; }
  .banner.ok { background: var(--ok-bg); color: var(--ok); border: 1px solid rgba(48,209,88,.35); }
  .banner.no { background: var(--panel); color: var(--muted); border: 1px solid var(--line); }
  .banner.err { background: rgba(255,69,58,.12); color: #ff6961; border: 1px solid rgba(255,69,58,.35); }
  .banner a { color: inherit; }
  h2 { font-size: 17px; margin: 26px 0 10px; }
  .matrix { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-size: 14px; }
  th, td { padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; white-space: nowrap; }
  th { color: var(--muted); font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
  td.n { text-align: right; font-variant-numeric: tabular-nums; }
  .pill { display: inline-block; border-radius: 999px; padding: 2px 9px; font-size: 12px; font-weight: 600; }
  .pill.ok { background: var(--ok-bg); color: var(--ok); }
  .pill.no { background: var(--panel2); color: var(--no); }
  .stores { display: grid; grid-template-columns: repeat(auto-fill, minmax(290px, 1fr)); gap: 12px; }
  .store h3 { margin: 0; font-size: 16px; }
  .store .meta { color: var(--muted); font-size: 13px; margin: 2px 0 10px; }
  .store ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  .store li { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; }
  .store li .q { color: var(--ok); font-weight: 600; text-align: right; }
  .store.none { opacity: .55; }
  .store .links { margin-top: 12px; display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 13px; white-space: nowrap; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .hidden { display: none !important; }
  footer { color: var(--muted); font-size: 12px; margin-top: 40px; }
  @media (max-width: 720px) {
    form.search { grid-template-columns: 1fr 1fr; }
    form.search .loc, form.search button { grid-column: span 2; }
    .status { margin-left: 0; width: 100%; }
  }
</style>
</head>
<body>
<main>
  <h1>iPhone Duo Stock Finder</h1>
  <p class="sub">Live in-store pickup availability at Apple Stores near you, checked directly against Apple.</p>

  <div class="card">
    <form class="search" id="search">
      <div><label for="model">Model</label><select id="model"></select></div>
      <div><label for="country">Country</label><select id="country"></select></div>
      <div class="loc"><label for="location">Postal code</label><input id="location" autocomplete="postal-code" required></div>
      <button id="go" type="submit">Check stock</button>
    </form>

    <div class="filters hidden" id="filters">
      <div class="row" id="f-variant"></div>
      <div class="row" id="f-color"></div>
      <div class="row" id="f-storage"></div>
    </div>

    <div class="row watch">
      <input type="checkbox" id="auto"><label for="auto">Auto-refresh every</label>
      <select id="interval">
        <option value="30">30 sec</option><option value="60" selected>1 min</option>
        <option value="120">2 min</option><option value="300">5 min</option>
      </select>
      <label><input type="checkbox" id="alert"> Alert me when it's in stock</label>
      <span class="status" id="status"></span>
    </div>
  </div>

  <div id="banner"></div>

  <section id="results" class="hidden">
    <h2>By configuration</h2>
    <div class="card matrix"><table id="matrix"></table></div>
    <h2>By store</h2>
    <div class="row" style="margin-bottom:10px">
      <label style="display:inline;text-transform:none;letter-spacing:0;font-size:14px;color:var(--text)">
        <input type="checkbox" id="onlyStock" style="width:auto"> Only show stores with stock
      </label>
    </div>
    <div class="stores" id="stores"></div>
  </section>

  <footer>
    Data comes straight from Apple's public store pickup service and can change minute to minute.
    Reserve or buy for pickup on apple.com to lock one in. Not affiliated with Apple Inc.
  </footer>
</main>

<script>
const $ = (id) => document.getElementById(id);
const store = {
  get(k, d) { try { const v = localStorage.getItem("duo:" + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("duo:" + k, JSON.stringify(v)); } catch {} },
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let options = null, data = null, timer = null, lastAvailable = null;  // null = no baseline yet, so the first result never alerts
const filters = { variant: new Set(), color: new Set(), storage: new Set() };

async function init() {
  options = await (await fetch("/api/inventory/options")).json();
  $("model").innerHTML = options.models.map((m) => `<option value="${m.key}">${esc(m.label)}</option>`).join("");
  $("country").innerHTML = options.countries.map((c) => `<option value="${c.key}">${esc(c.label)}</option>`).join("");

  const q = new URLSearchParams(location.search);
  $("model").value = q.get("model") || store.get("model", "iphone-duo");
  $("country").value = q.get("country") || store.get("country", "us");
  $("location").value = q.get("zip") || store.get("zip", "");
  $("interval").value = String(store.get("interval", 60));
  $("onlyStock").checked = store.get("onlyStock", false);
  updatePlaceholder();

  $("country").addEventListener("change", updatePlaceholder);
  $("search").addEventListener("submit", (e) => { e.preventDefault(); resetFilters(); lastAvailable = null; check(); });
  $("auto").addEventListener("change", schedule);
  $("interval").addEventListener("change", () => { store.set("interval", +$("interval").value); schedule(); });
  $("alert").addEventListener("change", async () => {
    if ($("alert").checked && "Notification" in window && Notification.permission === "default") {
      await Notification.requestPermission();
    }
  });
  $("onlyStock").addEventListener("change", () => { store.set("onlyStock", $("onlyStock").checked); render(); });

  if ($("location").value) check();
}

function updatePlaceholder() {
  const c = options.countries.find((c) => c.key === $("country").value);
  $("location").placeholder = c ? "e.g. " + c.example : "";
}

function resetFilters() { for (const k in filters) filters[k].clear(); }

async function check() {
  const params = { model: $("model").value, country: $("country").value, zip: $("location").value.trim() };
  if (!params.zip) return;
  store.set("model", params.model); store.set("country", params.country); store.set("zip", params.zip);
  history.replaceState(null, "", "?" + new URLSearchParams(params));

  $("go").disabled = true;
  $("status").textContent = "Checking Apple…";
  try {
    const res = await fetch("/api/inventory/check?" + new URLSearchParams({ model: params.model, country: params.country, location: params.zip }));
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "Something went wrong");
    data = body;
    buildFilters();
    render();
    notifyIfNew();
    $("status").textContent = "Updated " + new Date(body.checkedAt * 1000).toLocaleTimeString();
  } catch (e) {
    $("banner").innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
    $("results").classList.add("hidden");
    $("status").textContent = "";
  } finally {
    $("go").disabled = false;
    schedule();
  }
}

function schedule() {
  clearTimeout(timer);
  if ($("auto").checked && $("location").value.trim()) {
    timer = setTimeout(check, +$("interval").value * 1000);
  }
}

function uniq(key) {
  const out = [];
  for (const s of data.skus) if (s[key] && !out.includes(s[key])) out.push(s[key]);
  return out;
}

function buildFilters() {
  const groups = [["variant", "Model"], ["color", "Color"], ["storage", "Storage"]];
  let any = false;
  for (const [key, label] of groups) {
    const values = uniq(key);
    const el = $("f-" + key);
    if (values.length < 2) { el.innerHTML = ""; el.classList.add("hidden"); continue; }
    any = true;
    el.classList.remove("hidden");
    el.innerHTML = `<span class="flabel">${label}</span>` + values.map((v) =>
      `<button type="button" class="chip ${filters[key].has(v) ? "on" : ""}" data-k="${key}" data-v="${esc(v)}">${esc(v)}</button>`).join("");
  }
  $("filters").classList.toggle("hidden", !any);
  document.querySelectorAll("#filters .chip").forEach((b) => b.addEventListener("click", () => {
    const set = filters[b.dataset.k];
    set.has(b.dataset.v) ? set.delete(b.dataset.v) : set.add(b.dataset.v);
    b.classList.toggle("on");
    render();
  }));
}

function selectedSkus() {
  return data.skus.filter((s) =>
    (!filters.variant.size || filters.variant.has(s.variant)) &&
    (!filters.color.size || filters.color.has(s.color)) &&
    (!filters.storage.size || filters.storage.has(s.storage)));
}

function money(v, currency) {
  if (v == null) return "";
  try { return v.toLocaleString(undefined, { style: "currency", currency, maximumFractionDigits: 0 }); }
  catch { return v.toLocaleString(undefined, { maximumFractionDigits: 0 }); }
}

function render() {
  if (!data) return;
  const skus = selectedSkus();
  const label = (s) => s.storage ? `${s.variant} · ${s.storage} · ${s.color}` : s.name;

  const counts = skus.map((s) => data.stores.filter((st) => st.parts[s.part]?.available).length);
  const totalStores = data.stores.filter((st) => skus.some((s) => st.parts[s.part]?.available)).length;
  const nearest = data.stores.find((st) => skus.some((s) => st.parts[s.part]?.available));

  $("banner").innerHTML = totalStores
    ? `<div class="banner ok">✓ ${esc(data.modelLabel)} is available for pickup at ${totalStores} of ${data.stores.length} nearby stores.
         Nearest: ${esc(nearest.name)} (${esc(nearest.distanceText || "")}). <a href="${esc(data.buyUrl)}" target="_blank" rel="noopener">Buy for pickup →</a></div>`
    : `<div class="banner no">No matching ${esc(data.modelLabel)} configurations are in stock at the ${data.stores.length} stores nearest ${esc(data.location)} right now.
         Turn on auto-refresh and alerts to get pinged when one shows up.</div>`;

  $("matrix").innerHTML = `<tr><th>Configuration</th><th>Price</th><th>Part</th><th style="text-align:right">Stores in stock</th></tr>` +
    skus.map((s, i) => `<tr><td>${esc(label(s))}</td><td>${money(s.price, s.currency)}</td><td style="color:var(--muted)">${esc(s.part)}</td>
      <td class="n"><span class="pill ${counts[i] ? "ok" : "no"}">${counts[i]} / ${data.stores.length}</span></td></tr>`).join("");

  const only = $("onlyStock").checked;
  $("stores").innerHTML = data.stores.map((st) => {
    const avail = skus.filter((s) => st.parts[s.part]?.available);
    if (only && !avail.length) return "";
    const items = avail.length
      ? avail.map((s) => `<li><span>${esc(s.storage ? `${s.storage} ${s.color}` : s.name)}${filters.variant.size === 1 || uniq("variant").length < 2 ? "" : ` <span style="color:var(--muted)">${esc(s.variant)}</span>`}</span><span class="q">${esc(st.parts[s.part].quote)}</span></li>`).join("")
      : `<li style="color:var(--muted)">None of the selected configurations</li>`;
    const maps = st.lat != null ? `https://maps.apple.com/?q=${encodeURIComponent("Apple " + st.name)}&ll=${st.lat},${st.lng}` : null;
    return `<div class="card store ${avail.length ? "" : "none"}">
      <h3>${esc(st.name)}</h3>
      <div class="meta">${esc([st.city, st.state].filter(Boolean).join(", "))}${st.distanceText ? " · " + esc(st.distanceText) : ""}</div>
      <ul>${items}</ul>
      <div class="links">
        ${avail.length ? `<a href="${esc(data.buyUrl)}" target="_blank" rel="noopener">Buy for pickup</a>` : ""}
        ${st.url ? `<a href="${esc(st.url)}" target="_blank" rel="noopener">Store info</a>` : ""}
        ${maps ? `<a href="${esc(maps)}" target="_blank" rel="noopener">Directions</a>` : ""}
        ${st.phone ? `<a href="tel:${esc(st.phone.replace(/\s/g, ""))}">${esc(st.phone)}</a>` : ""}
      </div></div>`;
  }).join("") || `<div class="card" style="color:var(--muted)">No stores with stock.</div>`;

  $("results").classList.remove("hidden");
}

function notifyIfNew() {
  const skus = selectedSkus();
  const now = new Set();
  for (const st of data.stores) for (const s of skus) if (st.parts[s.part]?.available) now.add(st.number + "|" + s.part);
  const fresh = lastAvailable ? [...now].filter((k) => !lastAvailable.has(k)) : [];
  lastAvailable = now;
  if (!$("alert").checked || !fresh.length) return;

  const [num, part] = fresh[0].split("|");
  const st = data.stores.find((x) => x.number === num);
  const sku = data.skus.find((x) => x.part === part);
  const msg = `${sku.name} at Apple ${st.name}: ${st.parts[part].quote}` + (fresh.length > 1 ? ` (+${fresh.length - 1} more)` : "");
  beep();
  document.title = "🟢 In stock! · iPhone Duo Stock Finder";
  if ("Notification" in window && Notification.permission === "granted") {
    const n = new Notification("iPhone in stock", { body: msg });
    n.onclick = () => { window.focus(); window.open(data.buyUrl, "_blank"); };
  }
}

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [0, 0.25, 0.5].forEach((t) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = 880; o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0.2, ctx.currentTime + t);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + t + 0.2);
      o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + 0.2);
    });
  } catch {}
}

window.addEventListener("focus", () => { document.title = "iPhone Duo Stock Finder"; });
init();
</script>
</body>
</html>
"""
