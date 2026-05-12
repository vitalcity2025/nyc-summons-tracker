# NYC Summons Tracker

Vital City's quarterly tracker for NYPD-issued summonses in New York City. Covers three streams:

1. **NYPD Criminal Court Summons** — low-level criminal offenses such as disorderly conduct, trespass, and public urination. Adjudicated in criminal court.
2. **NYPD OATH Summons** — civil/administrative-code summonses such as open container, parks rules, and unlicensed vending. Adjudicated at the Office of Administrative Trials and Hearings.
3. **NYPD Moving Violations (B Summons)** — traffic enforcement: speeding, red light, cell phone, seatbelt, etc.

A single dashboard, with a top toggle that switches between the three datasets. Each view shows year-to-date totals, a trend chart (monthly / quarterly / annual), a borough breakdown (count or per-100k), and the top 10 movers up and down — at category or specific-offense level.

## How it works

- `scripts/fetch_summons.py` pulls aggregated counts from NYC Open Data via the Socrata SoQL API. We aggregate server-side (`date_extract_y`, `date_extract_m`, `count(*)`, `GROUP BY`) so we never download raw incident rows — the resulting `summons_data.json` is small enough to ship to GitHub Pages.
- `.github/workflows/refresh-data.yml` runs the fetch nightly and commits any changes.
- `index.html` is a single static file. No build step. GitHub Pages serves it directly.

## Data sources

| Dataset | Socrata ID | Date field |
|---|---|---|
| NYPD Criminal Court Summons (historic) | `sv2w-rv3k` | `summons_date` |
| NYPD Criminal Court Summons (year to date) | `mv4k-y93f` | `summons_date` |
| NYPD OATH Summons | `hxbk-grd3` | `occur_date` |
| NYPD B Summons (historic) | `bme5-7ty4` | `violation_date` |
| NYPD B Summons (year to date) | `57p3-pdcj` | `violation_date` |

Lookback starts at 2018 — that's where the B-summons historic begins and OATH coverage gets reliable. Criminal court summons reach back further but we cap at 2018 for cross-dataset consistency.

## Setup

> **Heads up:** the zip ships with a *synthetic* `summons_data.json` so you can open `index.html` locally and see the dashboard immediately. **Regenerate it with real data before pushing** — see step 4 below.

1. Create the GitHub repo, push these files.
2. Settings → Pages → Source: `main` branch, `/` root. Note the URL.
3. (Optional but recommended) Get a free [Socrata app token](https://data.cityofnewyork.us/profile/app_tokens) and add it as a repo secret named `SOCRATA_APP_TOKEN`. Without it, requests are subject to lower throttling.
4. Either:
   - Actions → run "Refresh summons data" once manually. The workflow generates a real `summons_data.json` and commits it back. After that it runs nightly.
   - Or locally: `python scripts/fetch_summons.py`, then commit the resulting file.

## Local development

```bash
python scripts/fetch_summons.py    # builds summons_data.json
python -m http.server 8000         # serve the directory
# open http://localhost:8000
```

## Methodology notes

- **Borough resolution.** Where a row has no borough field (some older B-summons rows), we infer borough from the NYPD precinct number using the standard precinct ranges (Manhattan 1–34, Bronx 40–52, Brooklyn 60–94, Queens 100–115, Staten Island 120–123).
- **B-summons offense descriptions.** The B-summons dataset stores only the violation code (e.g., `1180D`), not a description. We map the most common codes to plain-English buckets in `scripts/fetch_summons.py` (B_CODE_DESCRIPTIONS); unmapped codes fall under "Other moving violations."
- **Per-capita rates.** Use 2020 Census borough populations.
- **YTD comparison.** Always compares like for like: if data through May is available, both years are summed January–May.
- **Top movers minimum.** Categories or offenses must have at least 10 summonses in either year to appear in the movers list, to suppress noise from very rare offenses.

## License

Code: MIT. Data: per the NYC Open Data terms.
