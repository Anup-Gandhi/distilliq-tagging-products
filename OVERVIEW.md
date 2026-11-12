# Overview: Liquor Product Tagger

This tool matches store item names (for example `1792 Full Proof Bourbon 750ml`) to catalogue names using LLMs.
Postgres stores every result, so people can review them and corrections are kept.

## Input

Each source row comes from BigQuery or from a CSV export. A row holds:

- `baseValue`: the store's item name
- `options`: candidate catalogue names, separated by `|`

`tagger/bigquery_source.py` cleans each row. It trims the options and removes empty and duplicate ones.

## The three passes

| Pass | Command | Table | What it does |
|---|---|---|---|
| 1 | `python -m tagger run` | `llm_tags` | Picks one option for each row: a catalogue name, `Misc - Not a Liquor`, or `None of the above` plus a new Brand + Label name |
| 2 | `python -m tagger retag` | `llm_retags` | A different provider reviews the pass 1 answer |
| 3 | `python -m tagger final` | `llm_final_tags` | A judge model decides the rows where passes 1 and 2 disagree |

The `llm_tag_votes` view takes the tag that at least 2 of the 3 passes agree on as the final tag.

## Key rules

- Matching ignores size, pack count and proof. "Variety Pack" must appear on both sides or on neither.
- If the model is still unsure after a web search, it skips the row. The tag stays empty and `skip_reason` explains why, so a person can review it.
- Each run stops at a spending cap (`--max-cost`, $15 by default). Running the command again continues from where it stopped.

## Code map

| File | Role |
|---|---|
| `tagger/__main__.py` | Command line (`run`, `retag`, `final`, `import`, `export`) and parallel tagging |
| `tagger/claude_tagger.py` | System prompt, decision tool schema and the Anthropic tagger |
| `tagger/openai_tagger.py`, `tagger/gemini_tagger.py` | The same tagger on OpenAI and Gemini (Gemini searches with Google) |
| `tagger/final_tagger.py` | Request builder for the pass 3 judge |
| `tagger/budget.py` | Model prices and the cost cap |
| `tagger/config.py` | Reads `.env` and the per-LLM JSON files in `secrets/` |
| `tagger/store.py` | Postgres tables, migrations and comparison views (human vs. LLM, three-way, votes) |
| `tagger/*.md` | System prompt files (`retag_system_prompt.md` is the pass 2 prompt) |

## More detail

- [README.md](README.md): setup and usage
- [COMMANDS.md](COMMANDS.md): every command
- [BUILD-GUIDE.md](BUILD-GUIDE.md): full design
