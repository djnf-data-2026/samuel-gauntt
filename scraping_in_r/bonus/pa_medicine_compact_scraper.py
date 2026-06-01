"""
PALS PA - State Board of Medicine full dataset scraper using recursive LastName prefix tree.

Strategy:
  - The API hard-caps results at 500 per query (50 records x 10 pages).
  - LastName filtering does subdivide results, but single letters can still
    exceed 500. Two-letter prefixes usually stay under, occasionally need three.
  - This script recurses: try a prefix, paginate fully. If we hit exactly 500
    records (cap reached), split into 26 child prefixes (prefix + A-Z) and
    repeat. Recurse until all branches are fully exhausted.
  - A prefix with 0 results is skipped. A prefix that returns < 500 is complete.
  - Results are deduplicated by PersonId before saving.

Usage:
  python3 pa_medicine_compact_scraper.py [profession_id] [license_type_id] [output_stem]

  e.g. python3 pa_medicine_compact_scraper.py 37 84 pa_medicine_compact

Outputs:
  <output_stem>.json  - full deduplicated record list
  <output_stem>.csv   - CSV with key fields
"""

import json
import time
import csv
import string
import sys
import os
import subprocess
import urllib.request
from collections import OrderedDict
from pathlib import Path

# Ensure stdout is unbuffered so progress is visible when backgrounded
sys.stdout.reconfigure(line_buffering=True)

SEARCH_URL = "https://www.pals.pa.gov/api/Search/SearchForPersonOrFacilty"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Referer": "https://www.pals.pa.gov/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

# Defaults: State Board of Medicine / Medical Physician and Surgeon
DEFAULT_PROFESSION_ID = 37
DEFAULT_LICENSE_TYPE_ID = 84
DEFAULT_OUTPUT_STEM = "pa_medicine_compact"

CAP = 500                  # server-side hard limit
PAGE_SIZE = 50             # records per page
SUBDIVISION_THRESHOLD = 490  # subdivide if a prefix returns this many or more records
DELAY = 0.25               # seconds between requests

# Set at runtime by main()
BASE_PAYLOAD: dict = {}


REPO_ROOT = Path(__file__).resolve().parents[3]
GIT_ENV = {
    **os.environ,
    "PATH": (
        "/Applications/GitHub Desktop.app/Contents/Resources/app/git/libexec/git-core"
        f":{os.environ.get('PATH', '')}"
    ),
}


def git_push_bucket(path: str, letter: str, count: int) -> None:
    try:
        subprocess.run(["git", "add", path], check=True, cwd=REPO_ROOT, env=GIT_ENV)
        subprocess.run(["git", "commit", "-m", f"Add medicine bucket {letter}: {count} records"], cwd=REPO_ROOT, env=GIT_ENV)
        subprocess.run(["git", "stash"], check=True, cwd=REPO_ROOT, env=GIT_ENV)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=True, cwd=REPO_ROOT, env=GIT_ENV)
        subprocess.run(["git", "push", "origin", "main"], check=True, cwd=REPO_ROOT, env=GIT_ENV)
        subprocess.run(["git", "stash", "pop"], cwd=REPO_ROOT, env=GIT_ENV)
        print(f"  Pushed bucket {letter} to GitHub.")
    except subprocess.CalledProcessError as e:
        print(f"  WARNING: git push failed for bucket {letter}: {e}")


def fetch_page(last_name_prefix: str, page_no: int) -> list:
    payload = {**BASE_PAYLOAD, "LastName": last_name_prefix, "PageNo": page_no}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(SEARCH_URL, data=data, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_prefix(prefix: str) -> tuple[list, bool]:
    """
    Fetch all pages for a given LastName prefix.
    Returns (records, hit_cap) where hit_cap=True means the API may have more
    data beyond what was returned, requiring further subdivision.
    """
    records = []
    for page in range(1, (CAP // PAGE_SIZE) + 2):
        results = fetch_page(prefix, page)
        records.extend(results)
        time.sleep(DELAY)
        if len(results) < PAGE_SIZE:
            break  # last page
    return records, len(records) >= SUBDIVISION_THRESHOLD


def collect(prefix: str, seen_ids: dict, depth: int = 0) -> int:
    """
    Recursively collect all records for a prefix.
    Adds new records to seen_ids (keyed by PersonId, falling back to LicenseNumber).
    Returns count of new records added.
    """
    indent = "  " * depth
    records, capped = fetch_prefix(prefix)

    new_count = 0
    for r in records:
        key = r.get("PersonId") or r.get("LicenseNumber")
        if key and key not in seen_ids:
            seen_ids[key] = r
            new_count += 1

    if not capped:
        print(f"{indent}[{prefix!r}] {len(records)} records ({new_count} new)")
        return new_count

    # Hit the cap — subdivide into next-letter children
    print(f"{indent}[{prefix!r}] {len(records)} records ({new_count} new) — CAP HIT, subdividing...")
    for letter in string.ascii_uppercase:
        child_prefix = prefix + letter
        collect(child_prefix, seen_ids, depth + 1)

    return new_count


def main():
    global BASE_PAYLOAD

    profession_id   = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PROFESSION_ID
    license_type_id = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_LICENSE_TYPE_ID
    output_stem     = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_OUTPUT_STEM

    BASE_PAYLOAD = {
        "OptPersonFacility": "Person",
        "ProfessionID": profession_id,
        "LicenseTypeId": license_type_id,
        "State": "",
        "Country": "ALL",
        "County": None,
        "IsFacility": 0,
        "PersonId": None,
    }

    print(f"=== PALS PA scraper ===")
    print(f"Board:          State Board of Medicine (ProfessionID={profession_id})")
    print(f"License type:   Medical Physician and Surgeon (LicenseTypeId={license_type_id})")
    print(f"Output stem:    {output_stem}")
    print("Strategy: recursive LastName prefix tree\n")

    base_path = f"/Users/athughes/Documents/GitHub/nomad_doctors_hcij_umd/data/compact_data/pennsylvania/{output_stem}"
    seen_ids: dict = OrderedDict()

    fieldnames = [
        "LicenseNumber", "FirstName", "MiddleName", "LastName",
        "LicenceType", "Status", "City", "State", "County",
        "Country", "zipcode", "PersonId"
    ]

    log_path = base_path + "_prefix_counts.csv"

    with open(log_path, "w", newline="") as log_f:
        log_writer = csv.writer(log_f)
        log_writer.writerow(["prefix", "records"])

        for letter in string.ascii_uppercase:
            print(f"\n=== Letter group: {letter} ===")
            before_letter = set(seen_ids.keys())

            for second in string.ascii_uppercase:
                prefix = letter + second
                before = len(seen_ids)
                collect(prefix, seen_ids, depth=0)
                count = len(seen_ids) - before
                log_writer.writerow([prefix, count])
                log_f.flush()
                print(f"  [{prefix}] {count} new records (running total: {len(seen_ids)})")

            bucket_records = [seen_ids[k] for k in seen_ids if k not in before_letter]
            bucket_path = base_path + f"_{letter}.csv"
            with open(bucket_path, "w", newline="") as bf:
                bwriter = csv.DictWriter(bf, fieldnames=fieldnames, extrasaction="ignore")
                bwriter.writeheader()
                bwriter.writerows(bucket_records)
            print(f"  Saved bucket: {bucket_path} ({len(bucket_records)} records)")
            git_push_bucket(bucket_path, letter, len(bucket_records))

    print(f"Saved prefix counts: {log_path}")

    all_records = list(seen_ids.values())
    print(f"\n=== Scrape complete. Total unique records: {len(all_records)} ===")

    json_path = base_path + ".json"
    with open(json_path, "w") as f:
        json.dump(all_records, f, indent=2)
    print(f"Saved JSON: {json_path}")

    csv_path = base_path + ".csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_records)
    print(f"Saved CSV:  {csv_path}")


if __name__ == "__main__":
    main()
