"""One-time import: load a CoSpatia client CSV into the accounting module's
Customers list. The CSV is the export produced from the CoSpatia CRM dump
(columns: Client Name, Email, Address, Address 2, City, State, ZIP, ...).

Usage:  python import_clients.py [path-to-csv]

Re-running is safe — a client whose name is already in Customers is skipped,
not duplicated.
"""
import csv
import sys

from app import ar, db, ledger

DEFAULT_CSV = "S:/k12Dashboard/cospatia_clients.csv"


def compose_address(row):
    street = ", ".join(
        p for p in [(row.get("Address") or "").strip(),
                    (row.get("Address 2") or "").strip()] if p)
    city = (row.get("City") or "").strip()
    state = (row.get("State") or "").strip()
    zip_code = (row.get("ZIP") or "").strip()
    region = " ".join(p for p in [city + "," if city else "", state] if p)
    tail = " ".join(p for p in [region, zip_code] if p).strip()
    return ", ".join(p for p in [street, tail] if p) or None


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV
    conn = db.get_conn()
    try:
        company = ledger.current_company(conn)
        if company is None:
            print("No company is set up yet — create one before importing.")
            return
        imported = skipped = 0
        with open(csv_path, encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                name = (row.get("Client Name") or "").strip()
                if not name:
                    continue
                try:
                    ar.create_customer(
                        conn, company["id"], name,
                        email=(row.get("Email")
                               or row.get("Billing Email") or None),
                        billing_address=compose_address(row),
                        terms_days=30)
                    imported += 1
                except ledger.PostingError as exc:
                    skipped += 1
                    print(f"  skipped {name} — {exc}")
        print(f"\nImported {imported} client(s) into Customers for "
              f"{company['legal_name']}; skipped {skipped}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
