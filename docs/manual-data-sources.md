# Manually Provided Data Sources

Most backends fetch their data automatically during `aletheia-probe sync`.
Three backends instead read a **file that you download yourself**, because
their providers do not offer a stable, directly downloadable URL.

Each one looks for its file in a fixed directory under `.aletheia-probe/` in
your **current working directory**, and only accepts filenames matching a
fixed pattern. If no matching file is found, the backend is skipped during
sync with a message such as:

```
doaj: No DOAJ journal list found in /path/to/.aletheia-probe/doaj
```

| Backend | Place file in | Filename must match | Adds |
|---------|---------------|---------------------|------|
| `scopus` | `.aletheia-probe/scopus/` | `ext_list_*.xlsx` | ~30,000 indexed journals |
| `doaj` | `.aletheia-probe/doaj/` | `doaj_journalcsv_*.csv` | ~23,000 open-access journals |
| `dblp_venues` | `.aletheia-probe/dblp/` | `dblp.xml.gz` (exact name) | CS conference and journal venues |

If several matching files are present, the most recently modified one is used.

---

## Scopus journal list

**Adds** nearly 30,000 subscription journals from the major publishers
(Elsevier, Springer, Wiley, …) — the broadest source of `legitimate`
classifications for paywalled journals, which DOAJ does not cover.

1. Download the spreadsheet from
   [researchgate.net](https://www.researchgate.net/publication/384898389_Last_Update_of_Scopus_Indexed_Journal's_List_-_October_2024)
2. Place it in `.aletheia-probe/scopus/`, keeping the `ext_list_` prefix:

   ```bash
   mkdir -p .aletheia-probe/scopus/
   cp ~/Downloads/ext_list_October_2024.xlsx .aletheia-probe/scopus/
   ```

3. Run `aletheia-probe sync scopus`

---

## DOAJ journal list

**Adds** ~23,000 vetted open-access journals — the strongest single signal for
a `legitimate` classification.

1. Download the journals CSV from <https://doaj.org/csv>
2. Place it in `.aletheia-probe/doaj/` under its downloaded name:

   ```bash
   mkdir -p .aletheia-probe/doaj/
   cp ~/Downloads/doaj_journalcsv_20260810_2320_utf8.csv .aletheia-probe/doaj/
   ```

   Both of DOAJ's naming schemes are accepted — the current
   `doaj_journalcsv_*.csv` and the older `journalcsv__doaj_*.csv` — so no
   renaming is needed. Any other name (e.g. `doaj.csv`) is ignored.

3. Run `aletheia-probe sync doaj`

This file also enables fully offline DOAJ lookups — see
[Local DOAJ Backend](local-doaj-backend.md).

---

## DBLP venue dump

**Adds** computer-science conference and journal venue series, used for
acronym-based venue expansion (e.g. resolving `NeurIPS` or `ICSE` to a venue).

1. Open <https://dblp.org/xml/> **in a browser** and download `dblp.xml.gz`.
   Command-line downloads (`curl`, `wget`) do not work — dblp.org and its
   mirrors reject them.
2. Place it in `.aletheia-probe/dblp/` under its exact name:

   ```bash
   mkdir -p .aletheia-probe/dblp/
   cp ~/Downloads/dblp.xml.gz .aletheia-probe/dblp/
   ```

3. Run `aletheia-probe sync dblp_venues`

Parsing the dump takes several minutes and prints progress. If the file is
missing, sync will attempt a download and fail with
`Error - Not a gzipped file`.

If you host the dump internally, point the tool at your mirror instead and
sync will download it automatically — in `~/.config/aletheia-probe/config.yaml`:

```yaml
data_source_urls:
  dblp_xml_dump_url: "https://your-mirror.example.org/dblp.xml.gz"
```

---

## Verifying that a file was picked up

```bash
aletheia-probe sync doaj
```

A successful sync names the file it used:

```
doaj: Found journal list: doaj_journalcsv_20260810_2320_utf8.csv
doaj: Processed 23294 journals
```

In `aletheia-probe status`, note that `cached` is the backend *type*, not a
data state. A backend holds data only if its line also carries the
`📊 has data (N entries)` suffix:

```
✅ doaj        (enabled, cached, mode=remote) 📊 has data (23,072 entries) (updated: ...)
✅ dblp_venues (enabled, cached)          ← enabled, but no data
```

---

## Keeping the data fresh

Re-running sync within 30 days of the last successful update is a no-op.
After replacing a file with a newer download, force the update:

```bash
aletheia-probe sync doaj --force
```
