# Event Import Script — README

## Overview

`import_events.py` extracts future events from the Czech Red Cross "Dozory" Google Sheet
(downloaded as `.xlsx`) and converts them to a JSON file that can be pasted into the
MedCover import page (`/import/events/`).

---

## Prerequisites

Python 3.10+ with **openpyxl** installed:

```bash
pip install openpyxl
```

---

## How to Download the Source Sheet

1. Open the Google Sheet "Dozory YYYY" in Google Drive.
2. **File → Download → Microsoft Excel (.xlsx)**.
3. Save the file (e.g. `Dozory 2026.xlsx`) in the same directory where you will run the script.

> ⚠️ Do **not** use CSV export — the CSV format from this sheet has inconsistent line endings.

---

## Usage

```bash
# Basic usage — reads "Dozory 2026.xlsx" in the current directory,
# writes "import_events.json"
python scripts/import_events.py

# Custom input/output paths
python scripts/import_events.py --input /path/to/Dozory2026.xlsx --output /tmp/out.json

# Only include events from a specific date onwards
python scripts/import_events.py --cutoff 2026-06-01

# Print JSON to stdout (for piping)
python scripts/import_events.py --output -
```

### Arguments

| Argument | Default | Description |
|---|---|---|
| `--input` | `Dozory 2026.xlsx` | Path to the `.xlsx` file |
| `--output` | `import_events.json` | Output JSON path. Use `-` for stdout |
| `--cutoff` | today | Exclude events before this date (ISO format `YYYY-MM-DD`) |

---

## Column Mapping

| GS Column | Letter | Maps to |
|---|---|---|
| Akce | A | `name` |
| Datum | B | `date` |
| Místo konání | C | `location` |
| Vozidlo/stan | D | `description` (text snippet) |
| Osvěta pp / zdravotní dozor | E | `description` (text snippet) |
| kontakt pořadatel | F | `contact_person` + `description` |
| Začátek | G | `start_time` |
| Konec | H | `end_time` |
| Hlídky zdravotníků | I | *(ignored — spots created automatically)* |
| Počet ošetření | J | *(ignored)* |
| Placený | K | `paid` |
| Doba trvání | L | *(ignored — computed)* |
| Zodpovědný zdravotník | M | `responsible_person` |
| Přihlášení (col N+) | N… | `signups` (list of names — imported as Assignments) |

---

## Output JSON Format (v2)

The script outputs a **version 2** JSON envelope with two arrays: `users` and `events`.

```json
{
  "version": 2,
  "users": [
    {
      "gs_name":    "Testovací Rudolf",
      "name":       "Rudolf Testovací",
      "email":      "rudolf.testovaci@example.com",
      "phone":      "+420 777 123 456",
      "zdravotnik": true
    }
  ],
  "events": [
    {
      "name":               "Extraliga házená",
      "date":               "2026-02-08",
      "start_time":         "16:00",
      "end_time":           "18:00",
      "location":           "Sportovní hala FM",
      "paid":               true,
      "responsible_person": "Rudolf Testovací",
      "contact_person":     "Ukázkový Zdenko 603 123 456",
      "description":        "Typ: zdravotní dozor | Kontakt pořadatel: Ukázkový Zdenko",
      "time_missing":       false,
      "signups":            ["Alfons Zkušební", "Viktor Fiktivní"]
    }
  ]
}
```

`start_time` / `end_time` are `"HH:MM"` strings or `null`.

### User fields

| Field | Description |
|---|---|
| `gs_name` | Original name from the GS (e.g. `"Testovací Rudolf"`) |
| `name` | Converted to *Firstname Lastname* order |
| `email` | From the "Lidi" sheet (may be `null` if not found) |
| `phone` | From the "Lidi" sheet (may be `null`) |
| `zdravotnik` | `true` if column D in Lidi is `True` → gets Zdravotník qualification |

Names are **unique** within the `users` array (deduplication done by the script).

### Event `signups` field

List of already-assigned people (from GS columns N+), converted to *Firstname Lastname* order.
Used by the web app to create `Assignment` records automatically.

---

## Automatic Adjustments

| Situation | Behaviour |
|---|---|
| Event date before today | **Filtered out** |
| Duplicate event names (same name, different dates) | Date appended: *"Slavnostní otevření hřiště 22.1."* |
| Missing start time | `start_time: null`, `time_missing: true`, warning added to `description` |
| Midnight end time (00:00) | Treated as "end not specified" → `end_time: null` |
| Signups (col N+) | Extracted as list in `signups` — imported as `Assignment` records |
| Invalid / junk names (e.g. `.`) | Filtered out by name validation |
| Person in Dozory not found in Lidi | Included in `users` with `email: null`, `phone: null` |

---

## What Happens Next

Once you have `import_events.json`:

1. Open MedCover and go to **Import → Akce** (`/import/events/`).
2. Open the JSON file in any text editor, select all, and paste into the text box.
3. Click **Zkontrolovat** — the app will validate the data, check for duplicates,
   attempt to match responsible persons to known users, and preview new user accounts.
4. Review the editable preview table. A **Users** section at the top shows people
   who will be created (or already exist). Correct any issues (RP assignment, master event, etc.).
5. Click **Importovat** to run the import in one transaction:
   - New user accounts are created (Member role, qualification from Lidi data).
   - Events and spots are created.
   - Existing signups are attached as `Assignment` records.

---

## Spot Creation Rules

The number of spots created per event depends on how many people were signed up in the GS:

| Signups + RP | Spot pattern |
|---|---|
| ≤ 3 people total | 1 mandatory Zdravotník + 1 mandatory Zelenáč + 1 optional Zelenáč |
| > 3 people total | 1 mandatory Zdravotník + N mandatory Zelenáč (one per signup) |
| `time_missing: true` | No spots created |

If the event has no responsible person, the Zdravotník spot is still created but left unassigned.

Qualifications for these spots are configured on the import preview page before confirming.
If none are chosen, spots are created without qualification requirements.

---

## Notes

- The script is **idempotent** — running it multiple times on the same file produces
  the same output.
- Duplicate detection (same name + date already in the DB) is handled by the web app,
  not this script. Similarly, user creation is idempotent — if a user with the same name
  or email already exists, no duplicate is created.
- The `.xlsx` file should **not** be committed to the repository (it contains
  personal data — names, phone numbers). Add it to `.gitignore` if necessary.

## Lidi Sheet Requirements

The script reads both the **Dozory** sheet (events) and the **Lidi** sheet (people directory).

Expected Lidi columns:

| Column | Content |
|---|---|
| A | Last name |
| B | First name (or full name) |
| C | Email |
| D | `True`/`False` — zdravotník qualification |
| E | Phone number |

Names in the Lidi sheet are expected in *Lastname Firstname* order (matching the GS convention).
The script converts all names to *Firstname Lastname* for MedCover.
