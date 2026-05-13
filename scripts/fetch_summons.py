#!/usr/bin/env python3
"""
Fetch NYC summons data from three NYC Open Data sources and produce a single
summons_data.json for the dashboard.

Datasets:
  1. NYPD Criminal Court Summons:
       Historic: sv2w-rv3k  (2006-prior year)
       YTD:      mv4k-y93f  (current year)
       Fields: SUMMONS_DATE, OFFENSE_DESCRIPTION, SUMMONS_CATEGORY_TYPE, BORO, PRECINCT_OF_OCCUR

  2. NYPD OATH Summons:
       hxbk-grd3  (incident-level, combined history; NYPD-issued only)
       Fields: OCCUR_DATE, LAW_DESC, LAW_TYPE, CITY_NM, PRECINCT
       LAW_TYPE values (e.g., ADMINCODE, HEALTHCODE, PARKSRULE) serve as categories.

  3. NYPD Moving Violation (B) Summons:
       Historic: bme5-7ty4
       YTD:      57p3-pdcj
       Fields: VIOLATION_DATE, CHG_LAW_CD, VIOLATION_CODE, CITY_NM, RPT_OWNING_CMD
       No offense description in the dataset; we apply a lookup table.

Strategy: use SoQL server-side aggregation (date_extract_y, date_extract_m, count(*))
so we never download raw rows. This keeps the pull to a few thousand rows per dataset.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

OUT_DIR = Path(__file__).resolve().parent.parent
OUT_FILE = OUT_DIR / "summons_data.json"

# Optional Socrata app token (much higher rate limits if set in env)
import os
APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN", "")

SOCRATA_BASE = "https://data.cityofnewyork.us/resource/{}.json"

# Lookback start year. Criminal court summons starts 2006 in open data.
# B summons historic starts 2018. OATH starts ~2018 as well.
START_YEAR = 2018
CURRENT_YEAR = datetime.now().year

# ──────────────────────────────────────────────────────────────────────────────
# B-summons violation code lookup (most common codes).
# Source: NYS VTL and NYC Admin Code. Codes are normalized to uppercase, no spaces.
# Anything not in this map falls into "Other Moving Violations".
# ──────────────────────────────────────────────────────────────────────────────
B_CODE_DESCRIPTIONS = {
    # Speeding
    "1180A": ("Speeding", "Speeding (basic rule)"),
    "1180B": ("Speeding", "Speeding (state highway)"),
    "1180C": ("Speeding", "Speeding (school zone)"),
    "1180D": ("Speeding", "Speeding (posted limit)"),
    "1180D1": ("Speeding", "Speeding 1-10 over"),
    "1180D2": ("Speeding", "Speeding 11-30 over"),
    "1180D3": ("Speeding", "Speeding 30+ over"),
    "1180E": ("Speeding", "Speeding (work zone)"),
    "1180F": ("Speeding", "Speeding (school zone)"),
    "1180G": ("Speeding", "Speeding (>55 zone)"),
    # Traffic signals
    "1110A": ("Disobey traffic device", "Failure to obey traffic-control device"),
    "1110B": ("Disobey traffic device", "Failure to obey traffic-control device"),
    "1111A1": ("Red light", "Failure to stop at red signal"),
    "1111D1": ("Red light", "Failure to stop at red signal"),
    "1111D2A": ("Red light", "Failure to stop at red signal"),
    # Cell phone / electronic device
    "1225C": ("Cell phone / device", "Use of mobile phone"),
    "1225C2A": ("Cell phone / device", "Use of mobile phone"),
    "1225D": ("Cell phone / device", "Use of portable electronic device"),
    "1225D1": ("Cell phone / device", "Use of portable electronic device"),
    # Seatbelt / child restraint
    "1229C": ("Seatbelt / child restraint", "Seatbelt violation"),
    "1229C1": ("Seatbelt / child restraint", "Driver seatbelt"),
    "1229C2": ("Seatbelt / child restraint", "Passenger seatbelt"),
    "1229C3": ("Seatbelt / child restraint", "Child restraint"),
    "1229C3A": ("Seatbelt / child restraint", "Child restraint"),
    # Stop signs
    "1172A": ("Stop sign", "Failure to stop at stop sign"),
    # Turning / signals
    "1163A": ("Improper turn / signal", "Improper signal"),
    "1163B": ("Improper turn / signal", "Improper signal"),
    "1163D": ("Improper turn / signal", "Improper signal"),
    "1160A": ("Improper turn / signal", "Improper turn"),
    "1160B": ("Improper turn / signal", "Improper turn"),
    "1160C": ("Improper turn / signal", "Improper turn"),
    "1160D": ("Improper turn / signal", "Improper turn"),
    # Following / passing
    "1129A": ("Following / passing", "Following too closely"),
    "1122": ("Following / passing", "Passing"),
    "1122A": ("Following / passing", "Passing"),
    # Lane use
    "1128A": ("Lane use", "Moved from lane unsafely"),
    "1128B": ("Lane use", "Moved from lane unsafely"),
    "1128D": ("Lane use", "Moved from lane unsafely"),
    # Reckless / aggressive
    "1212": ("Reckless driving", "Reckless driving"),
    # Unlicensed / suspended
    "509": ("License violation", "Operating without a license"),
    "5091": ("License violation", "Unlicensed operator"),
    "511": ("License violation", "Aggravated unlicensed operation"),
    "5111": ("License violation", "AUO 3rd degree"),
    "512": ("License violation", "Operating with suspended registration"),
    # Registration / inspection
    "401": ("Registration / inspection", "Unregistered vehicle"),
    "4011": ("Registration / inspection", "Unregistered vehicle"),
    "4011A": ("Registration / inspection", "Unregistered vehicle"),
    "402": ("Registration / inspection", "Plate violation"),
    "306B": ("Registration / inspection", "Uninspected vehicle"),
    # Pedestrian / crosswalk
    "1146A": ("Failure to yield to pedestrian", "Failure to yield right-of-way to pedestrian"),
    "1151A": ("Failure to yield to pedestrian", "Failure to yield right-of-way in crosswalk"),
    # Insurance
    "319": ("No insurance", "Operating without insurance"),
    "3191": ("No insurance", "Operating without insurance"),
    # NYC Admin / Traffic Rules (NYC codes)
    "405B1": ("NYC: Parking / standing", "Parking / standing rule"),
    "406A1": ("NYC: Parking / standing", "Parking / standing rule"),
    "37512A1": ("NYC: Truck route", "Truck route violation"),
    "37512A2": ("NYC: Truck route", "Truck route violation"),
    "4012": ("NYC: Vehicle equipment", "Vehicle equipment"),
}

def b_category(law_cd, code):
    """Return (category, description) for a B-summons row."""
    if not code:
        return ("Other moving violations", "Unknown")
    key = str(code).strip().upper().replace(" ", "").replace(".", "")
    if key in B_CODE_DESCRIPTIONS:
        return B_CODE_DESCRIPTIONS[key]
    # Try without trailing letter
    if len(key) > 1 and key[-1].isalpha() and key[:-1] in B_CODE_DESCRIPTIONS:
        return B_CODE_DESCRIPTIONS[key[:-1]]
    return ("Other moving violations", f"{law_cd or '?'} {code}")


# ──────────────────────────────────────────────────────────────────────────────
# OATH Admin Code conduct buckets.
# OATH categorizes by law source (ADMINCODE, HEALTHCODE, etc.), which lumps very
# different offenses under ADMINCODE. We split ADMINCODE into conduct-based
# sub-buckets by matching keywords in the offense description (LAW_DESC).
# Order matters — first match wins.
# ──────────────────────────────────────────────────────────────────────────────
ADMIN_CODE_BUCKETS = [
    # (bucket_label, list of keyword fragments to match in offense description)
    ("ADMIN: OPEN CONTAINER",      ["ALCOHOLIC BEVERAG", "OPEN CONTAINER", "UNLAWFUL CONSUMPTION"]),
    ("ADMIN: PUBLIC URINATION",    ["URINAT"]),
    ("ADMIN: UNREASONABLE NOISE",  ["NOISE", "UNREASONABLE"]),
    ("ADMIN: UNLICENSED VENDING",  ["VENDING", "VENDOR", "PEDDLER"]),
    ("ADMIN: SMOKING",             ["SMOK"]),
    ("ADMIN: BICYCLE ON SIDEWALK", ["BICYCLE", "BIKE"]),
    ("ADMIN: LITTER",              ["LITTER", "LITTERING", "DEBRIS"]),
    ("ADMIN: SPITTING",            ["SPIT"]),
]

def split_admin_code(category, offense):
    """If category is ADMINCODE, return a more specific bucket based on offense text."""
    if not category or category.upper() != "ADMINCODE":
        return category
    off = (offense or "").upper()
    for bucket, kws in ADMIN_CODE_BUCKETS:
        for kw in kws:
            if kw in off:
                return bucket
    return "ADMIN: OTHER"


# ──────────────────────────────────────────────────────────────────────────────
# Socrata helpers
# ──────────────────────────────────────────────────────────────────────────────
def soda_get(dataset_id, params, retries=4):
    """Run a SoQL query against Socrata. Returns list of dicts."""
    url = SOCRATA_BASE.format(dataset_id) + "?" + urlencode(params)
    headers = {"User-Agent": "vital-city-summons-tracker"}
    if APP_TOKEN:
        headers["X-App-Token"] = APP_TOKEN
    for attempt in range(retries):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except (HTTPError, URLError) as e:
            wait = 2 ** attempt
            print(f"  [retry {attempt+1}/{retries}] {e} — sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url}")


def norm_boro(s):
    """Normalize borough strings to Title Case."""
    if not s:
        return "Unknown"
    s = s.strip().upper()
    if s in ("MANHATTAN", "MAN", "M", "NEW YORK"):
        return "Manhattan"
    if s in ("BRONX", "BX", "B"):
        return "Bronx"
    if s in ("BROOKLYN", "BK", "K", "KINGS"):
        return "Brooklyn"
    if s in ("QUEENS", "Q"):
        return "Queens"
    if s in ("STATEN ISLAND", "SI", "S", "RICHMOND"):
        return "Staten Island"
    return "Unknown"


def precinct_to_boro(p):
    """Fallback: NYC precinct number to borough."""
    if p is None or p == "":
        return "Unknown"
    try:
        n = int(p)
    except (ValueError, TypeError):
        return "Unknown"
    # Standard NYPD precinct ranges
    if 1 <= n <= 34:
        return "Manhattan"
    if 40 <= n <= 52:
        return "Bronx"
    if 60 <= n <= 94:
        return "Brooklyn"
    if 100 <= n <= 115:
        return "Queens"
    if n in (120, 121, 122, 123):
        return "Staten Island"
    return "Unknown"


# ──────────────────────────────────────────────────────────────────────────────
# Dataset 1: NYPD Criminal Court Summons (historic + YTD)
# ──────────────────────────────────────────────────────────────────────────────
def fetch_criminal_court():
    print("\n=== NYPD Criminal Court Summons ===")
    out = []
    # Historic (sv2w-rv3k): 2006 through last full year. SUMMONS_DATE format.
    # We use floating_timestamp filtering by year extraction.
    for ds_id, label in [("sv2w-rv3k", "historic"), ("mv4k-y93f", "ytd")]:
        print(f"  Fetching {label} ({ds_id})...")
        params = {
            "$select": (
                "date_extract_y(summons_date) AS year,"
                "date_extract_m(summons_date) AS month,"
                "BORO,"
                "PRECINCT_OF_OCCUR AS precinct,"
                "SUMMONS_CATEGORY_TYPE AS category,"
                "OFFENSE_DESCRIPTION AS offense,"
                "count(*) AS n"
            ),
            "$where": f"summons_date >= '{START_YEAR}-01-01T00:00:00'",
            "$group": "year, month, BORO, precinct, category, offense",
            "$limit": "200000",
        }
        rows = soda_get(ds_id, params)
        print(f"    got {len(rows):,} grouped rows")
        for r in rows:
            try:
                yr = int(float(r["year"]))
                mo = int(float(r["month"]))
                n = int(r["n"])
            except (KeyError, ValueError, TypeError):
                continue
            boro = norm_boro(r.get("BORO"))
            pct = r.get("precinct") or ""
            try:
                pct = int(pct)
            except (ValueError, TypeError):
                pct = None
            # Fallback boro from precinct
            if boro == "Unknown" and pct is not None:
                boro = precinct_to_boro(pct)
            cat = (r.get("category") or "").strip().upper()
            if cat in ("", "NULL", "(NULL)", "NONE"):
                cat = "UNCATEGORIZED"
            offense = (r.get("offense") or "").strip().upper()
            if offense in ("", "NULL", "(NULL)", "NONE"):
                offense = "UNKNOWN"
            out.append({
                "year": yr, "month": mo, "boro": boro, "precinct": pct,
                "category": cat, "offense": offense, "n": n,
            })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Dataset 2: NYPD OATH Summons
# ──────────────────────────────────────────────────────────────────────────────
def fetch_oath():
    print("\n=== NYPD OATH Summons ===")
    ds_id = "hxbk-grd3"
    print(f"  Fetching {ds_id}...")
    params = {
        "$select": (
            "date_extract_y(occur_date) AS year,"
            "date_extract_m(occur_date) AS month,"
            "city_nm AS boro,"
            "precinct,"
            "law_type AS category,"
            "law_desc AS offense,"
            "count(*) AS n"
        ),
        "$where": f"occur_date >= '{START_YEAR}-01-01T00:00:00'",
        "$group": "year, month, city_nm, precinct, law_type, law_desc",
        "$limit": "200000",
    }
    rows = soda_get(ds_id, params)
    print(f"    got {len(rows):,} grouped rows")
    out = []
    for r in rows:
        try:
            yr = int(float(r["year"]))
            mo = int(float(r["month"]))
            n = int(r["n"])
        except (KeyError, ValueError, TypeError):
            continue
        boro = norm_boro(r.get("boro"))
        pct = r.get("precinct") or ""
        try:
            pct = int(pct)
        except (ValueError, TypeError):
            pct = None
        if boro == "Unknown" and pct is not None:
            boro = precinct_to_boro(pct)
        cat = (r.get("category") or "").strip().upper()
        if cat in ("", "NULL", "(NULL)", "NONE"):
            cat = "UNCATEGORIZED"
        offense = (r.get("offense") or "").strip().upper()
        if offense in ("", "NULL", "(NULL)", "NONE"):
            offense = "UNKNOWN"
        # Split ADMINCODE into conduct-based buckets for clearer dashboard display
        cat = split_admin_code(cat, offense)
        out.append({
            "year": yr, "month": mo, "boro": boro, "precinct": pct,
            "category": cat, "offense": offense, "n": n,
        })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Dataset 3: NYPD B Summons (Moving Violations)
# ──────────────────────────────────────────────────────────────────────────────
def fetch_b_summons():
    print("\n=== NYPD B Summons (Moving Violations) ===")
    out = []
    for ds_id, label in [("bme5-7ty4", "historic"), ("57p3-pdcj", "ytd")]:
        print(f"  Fetching {label} ({ds_id})...")
        params = {
            "$select": (
                "date_extract_y(violation_date) AS year,"
                "date_extract_m(violation_date) AS month,"
                "city_nm AS boro,"
                "rpt_owning_cmd AS precinct,"
                "chg_law_cd AS law,"
                "violation_code AS code,"
                "count(*) AS n"
            ),
            "$where": f"violation_date >= '{START_YEAR}-01-01T00:00:00'",
            "$group": "year, month, city_nm, rpt_owning_cmd, chg_law_cd, violation_code",
            "$limit": "200000",
        }
        rows = soda_get(ds_id, params)
        print(f"    got {len(rows):,} grouped rows")
        for r in rows:
            try:
                yr = int(float(r["year"]))
                mo = int(float(r["month"]))
                n = int(r["n"])
            except (KeyError, ValueError, TypeError):
                continue
            boro = norm_boro(r.get("boro"))
            pct = r.get("precinct") or ""
            try:
                pct = int(pct)
            except (ValueError, TypeError):
                pct = None
            if boro == "Unknown" and pct is not None:
                boro = precinct_to_boro(pct)
            cat, offense = b_category(r.get("law"), r.get("code"))
            out.append({
                "year": yr, "month": mo, "boro": boro, "precinct": pct,
                "category": cat.upper(), "offense": offense.upper(), "n": n,
            })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Compaction: collapse the long lists into nested counts for the dashboard
# ──────────────────────────────────────────────────────────────────────────────
def compact(rows):
    """
    Produce a structure the dashboard can slice quickly:
      counts[year][month][boro][category][offense] = n
      precinct_counts[year][month][precinct][category][offense] = n
    Plus pre-computed totals at each level.
    """
    counts = {}             # by boro
    pct_counts = {}         # by precinct
    categories = {}         # category -> set of offenses
    boros = set()
    precincts = set()
    years = set()
    months_seen = {}        # year -> set(months)

    for r in rows:
        y, m = r["year"], r["month"]
        boro = r["boro"]
        pct = r["precinct"]
        cat = r["category"]
        off = r["offense"]
        n = r["n"]

        years.add(y)
        months_seen.setdefault(y, set()).add(m)
        boros.add(boro)
        if pct is not None:
            precincts.add(pct)
        categories.setdefault(cat, set()).add(off)

        # By boro
        d = counts.setdefault(y, {}).setdefault(m, {}).setdefault(boro, {}).setdefault(cat, {})
        d[off] = d.get(off, 0) + n
        # By precinct
        if pct is not None:
            d2 = pct_counts.setdefault(y, {}).setdefault(m, {}).setdefault(pct, {}).setdefault(cat, {})
            d2[off] = d2.get(off, 0) + n

    return {
        "counts": counts,
        "pct_counts": pct_counts,
        "categories": {c: sorted(offs) for c, offs in categories.items()},
        "boros": sorted(b for b in boros if b != "Unknown") + (["Unknown"] if "Unknown" in boros else []),
        "precincts": sorted(precincts),
        "years": sorted(years),
        "months_by_year": {str(y): sorted(months_seen[y]) for y in months_seen},
    }


def main():
    print(f"Fetching summons data from {START_YEAR} through {CURRENT_YEAR}")
    print(f"App token: {'set' if APP_TOKEN else 'NOT SET (lower rate limits apply)'}")

    criminal = fetch_criminal_court()
    oath     = fetch_oath()
    b        = fetch_b_summons()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start_year": START_YEAR,
        "datasets": {
            "criminal_court": {
                "label": "NYPD Criminal Court Summons",
                "blurb": "Criminal summonses issued by the NYPD for low-level offenses such as disorderly conduct, public urination, and trespass.",
                "source": "NYC Open Data: sv2w-rv3k (historic) + mv4k-y93f (year-to-date)",
                **compact(criminal),
            },
            "oath": {
                "label": "NYPD OATH Summons",
                "blurb": "Civil summonses issued by the NYPD for administrative-code violations such as open container, public urination, and parks rules. Adjudicated at OATH, not criminal court.",
                "source": "NYC Open Data: hxbk-grd3",
                **compact(oath),
            },
            "b_summons": {
                "label": "NYPD Moving Violations (B Summons)",
                "blurb": "Moving-violation summonses issued by the NYPD for traffic infractions such as speeding, red-light running, and cell-phone use while driving.",
                "source": "NYC Open Data: bme5-7ty4 (historic) + 57p3-pdcj (year-to-date)",
                **compact(b),
            },
        },
    }

    OUT_FILE.write_text(json.dumps(payload, separators=(",", ":")))
    size_mb = OUT_FILE.stat().st_size / 1024 / 1024
    print(f"\nWrote {OUT_FILE} ({size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
