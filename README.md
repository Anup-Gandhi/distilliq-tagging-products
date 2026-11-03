# Liquor Product Tagger

Tags store item names (e.g. `1792 Full Proof Bourbon 750ml`) to catalogue names with LLMs, in three passes
whose results live in Postgres. See [BUILD-GUIDE.md](BUILD-GUIDE.md) for the full design.

| Pass | Command | Table | Who |
|---|---|---|---|
| 1 | `run` | `claude_tags` | the LLM you pick |
| 2 | `retag` | `claude_retags` | a different provider, without seeing pass 1's answer |
| 3 | `final` | `claude_final_tags` | settles rows where 1 and 2 disagree |
| vote | view `claude_tag_votes` | | `final_tag` by 2 of 3 |

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # then fill in DATABASE_URL
```

Put one JSON file per LLM in `secrets/` (never committed):

```json
{"provider": "anthropic", "model": "claude-haiku-5-5", "api_key": "sk-ant-..."}
{"provider": "openai",    "model": "gpt-4.1-mini",     "api_key": "sk-..."}
{"provider": "gemini",    "model": "gemini-3-pro-preview", "api_key": "AIza..."}
```

Optional keys: `effort` (`low`, `medium` or `high`; default `medium`, and `--effort` overrides it), and
`input_price`, `output_price` (USD per 1M tokens) and `search_price` (USD per search), for a model not listed
in `tagger/budget.py`. Pass 3 uses `secrets/final-tagger.json` unless you pass `--llm`.

## The question

Every record is asked the way taggers used to ask Google's AI Overview, with every option joined by ` OR `:

```
Which name most properly identifies -196 Lemon Vodka Seltzer - Carbliss Lemon Vodka OR Carbliss Lemon Vodka Pack OR Misc - Not a Liquor OR None of the above ? Is -196 Lemon Vodka Seltzer an alcoholic or non-alcoholic item ? If none of the names identify -196 Lemon Vodka Seltzer, what is the most proper Brand + Label name for -196 Lemon Vodka Seltzer ?
```

The system prompt (`tagger/claude_tagger.py`) explains what "most properly identifies" means and how to
write a Brand + Label name. Choosing `None of the above` makes the Brand + Label name the tag.

Matching ignores sizes, pack counts and proof, but "Variety Pack" must appear on both sides or neither. An
item that names a liquor type is decided from its name; one that doesn't is searched first. When a record is
still unclear after searching, it is **skipped**: `tag` stays empty and `skip_reason` says why, so a person
can review it. Skipped rows aren't errors, so `--retry-errors` leaves them alone. In `claude_tag_votes` they
show as `decision = 'skipped'` with the reason in `skip_reason`.

Gemini answers with Grounding with Google Search, the same search behind AI Overviews.

## Usage

```bash
python -m tagger run --llm gemini --dry-run --limit 20   # try it; prints JSON, writes nothing
python -m tagger run --llm gemini                        # pass 1 on new BigQuery rows
python -m tagger run --llm gemini --csv topk.csv         # pass 1 from a CSV export (sourceRowId, baseValue, options)
python -m tagger retag                                   # pass 2, using the other LLM files in secrets/
python -m tagger final --llm claude                      # pass 3
python -m tagger import records-tina-100.csv             # upload an already-tagged CSV without tagging it
python -m tagger export final-tags/claude_tags.csv       # add --retags for claude_retags
```

Shared options: `--max-cost USD` (default 15), `--limit N`, `--retry-errors`, `--workers N`,
`--effort low|medium|high` (default medium), `--no-web-search`, `--dry-run`.

`retag` sends each row to a provider other than the one that tagged it in pass 1, preferring Gemini, then
Anthropic, then OpenAI among the LLM files available (or those given with `--llm`, which can be repeated).

Every CSV given to `run --csv` or `import` is stored row by row in `csv_records` (the whole row is in
`data`, including human tags). The view `human_tag_comparison` puts the human tag (`final_tags`, else
`tag_1`) next to the LLM's tag, with `comparison` = `same`, `different`, `both new name` (check by eye),
`human skipped`, `llm skipped`, `not tagged by llm` or `no human tag`.

Rows already in Postgres are never re-read from BigQuery, so corrections made in Postgres are kept. If a run
stops at `--max-cost`, run it again (with `--full` for `run`) to continue.
