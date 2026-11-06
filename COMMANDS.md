# Commands

Every command for running the liquor tagging pipeline, in the order you'd normally use them. Run them from the project
folder in PowerShell. See [README.md](README.md) for what each pass does.

| Step | Command | Saves to |
|---|---|---|
| Pass 1: tag items | `run` | `llm_tags` |
| Pass 2: review pass 1 | `retag` | `llm_retags` |
| Pass 3: judge the rows that didn't match | `final` | `llm_final_tags` |
| Results | view `final_tag_summary` | |

## 1. Setup

First time only:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Then:

- Set `DATABASE_URL` in `.env`.
- Add one LLM file per model to `secrets/`, for example `secrets/gemini.json`:

  ```json
  {"provider": "gemini", "model": "gemini-3.1-flash-lite", "effort": "medium", "api_key": "YOUR_KEY"}
  ```

- Pass 3 uses `secrets/final-tagger.json` unless you pass `--llm`.

A model not listed in `tagger/budget.py` also needs `input_price` and `output_price` (USD per 1M tokens) in its file.

Every new session:

```powershell
.venv\Scripts\activate
```

## 2. Pass 1: tag items

Preview first. This prints JSON and saves nothing:

```powershell
python -m tagger run --llm gemini --dry-run --limit 20
```

Then choose where the rows come from:

```powershell
python -m tagger run --llm gemini                   # new rows from BigQuery
python -m tagger run --llm gemini --full            # the whole BigQuery table, skipping rows already tagged
python -m tagger run --llm gemini --csv topk.csv    # a CSV export (sourceRowId, baseValue, options)
```

Re-run rows that failed:

```powershell
python -m tagger run --llm gemini --retry-errors
```

## 3. Pass 2: review pass 1

Preview, then run:

```powershell
python -m tagger retag --llm gemini --llm claude --dry-run
python -m tagger retag --llm gemini --llm claude
```

Re-run rows that failed:

```powershell
python -m tagger retag --llm gemini --llm claude --retry-errors
```

Each row goes to a provider other than the one that tagged it. `--llm` is needed while `secrets/` has more than one file
for a provider.

## 4. Pass 3: judge the rows that didn't match

See which rows will be judged, in Postgres. This also lists rows already judged, which `final` skips:

```sql
SELECT base_value, tag_1, tag_2, is_match
FROM first_second_tag_comparison
WHERE needs_third_tag;
```

Test one row first. Check that `sources` has links, which shows the model is searching the web:

```powershell
python -m tagger final --dry-run --limit 1
```

Then run:

```powershell
python -m tagger final --max-cost 5
```

To use a different LLM file, or re-run rows that failed:

```powershell
python -m tagger final --llm claude
python -m tagger final --retry-errors
```

## 5. See the results

Run these in Postgres.

All rows, with the reason for each final tag and the sources:

```sql
SELECT * FROM final_tag_summary ORDER BY first_tagged_at;
```

How many rows reached each outcome:

```sql
SELECT decision, count(*) AS rows, count(final_tag) AS with_final_tag
FROM llm_tag_votes
GROUP BY decision
ORDER BY rows DESC;
```

The judge's decisions:

```sql
SELECT base_value, tag_1, tag_2, tag, decision, dispute_type, disputed_point, reason
FROM llm_final_tags
ORDER BY decision;
```

Rows sent to a person:

```sql
SELECT base_value, tag_1, tag_2, skip_reason
FROM llm_final_tags
WHERE decision = 'HUMAN';
```

LLM tags next to human tags, for uploaded CSVs:

```sql
SELECT comparison, count(*) AS rows
FROM human_tag_comparison
GROUP BY comparison
ORDER BY rows DESC;
```

## 6. Export and import

```powershell
mkdir final-tags                                             # first time only
python -m tagger export final-tags/llm_tags.csv              # pass 1
python -m tagger export final-tags/retags.csv --retags       # pass 2
python -m tagger import records-tina-100.csv                 # upload a tagged CSV without tagging it
```

`export` only covers passes 1 and 2. To export `final_tag_summary`, use your database tool's export option, or `psql` if
you have it installed. Change the connection details if yours differ:

```powershell
psql -h localhost -U postgres -d tagging-2 -c "\copy (SELECT * FROM final_tag_summary ORDER BY first_tagged_at) TO 'final-tags/final_tag_summary.csv' CSV HEADER"
```

## 7. Run rows through pass 3 again

`final` skips any row already saved, and `--retry-errors` only redoes failures. To judge a row again, delete its saved
result first. **This can't be undone**, so look at the rows before you delete them.

One item:

```sql
SELECT base_value, tag, decision FROM llm_final_tags WHERE base_value = 'Don Ramon - Plata Tequila';
DELETE FROM llm_final_tags WHERE base_value = 'Don Ramon - Plata Tequila';
```

Then run `final` again:

```powershell
python -m tagger final --max-cost 5
```

### Rows whose pass 2 review failed

Delete the pass 3 results first, because the retry clears the errors this query looks for:

```sql
DELETE FROM llm_final_tags
WHERE source_row_id IN (SELECT source_row_id FROM llm_retags WHERE error IS NOT NULL);
```

Then re-run the reviews and judge whatever still doesn't match:

```powershell
python -m tagger retag --llm gemini --llm claude --retry-errors
python -m tagger final --max-cost 5
```

## 8. Options

These work with `run`, `retag` and `final`.

| Option | What it does | Default |
|---|---|---|
| `--dry-run` | Prints results as JSON and saves nothing. It still calls the model, so it costs money | Off |
| `--limit N` | Tags at most N rows | All rows, or 5 with `--dry-run` |
| `--max-cost USD` | Stops at this spend. Run the same command again to continue | 15 |
| `--retry-errors` | Also re-runs rows that failed | Off |
| `--workers N` | Number of parallel requests | `TAG_WORKERS` in `.env`, else 4 |
| `--effort low\|medium\|high` | Overrides the LLM file's effort | The file's, else medium |
| `--no-web-search` | Turns off web search | Search on |

If `run` stops at the cost limit, continue with `--full`. Otherwise the next run only reads rows newer than the last one tagged.

For the full list on any command:

```powershell
python -m tagger final --help
```

## 9. Tests

```powershell
python -m unittest discover -s tests      # test_clean_row.py only
```

`tests/test_retag.py` needs pytest, which isn't in `requirements.txt`:

```powershell
pip install pytest
python -m pytest tests
```
