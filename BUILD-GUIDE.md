# Build Guide: Liquor Product Tagger

Everything about this code, for someone who has to rebuild it, extend it or fix it. It covers what the product does,
every file, every command and option, every table and view, how each LLM provider is called, how a model's answer
becomes a stored tag, the money controls, the tests, and the things that look odd but are on purpose.

Four files must be copied exactly rather than rewritten from this guide: the prompts `tagger/system_prompt.md`,
`tagger/system_prompt_v2.md`, `tagger/retag_system_prompt.md` and `tagger/third_tag_system_prompt.md`, and the view
SQL in `tagger/store.py`. This guide explains them; it does not repeat them line for line.

---

## 1. What the product does

Liquor stores send item names in their own formats (`1792 Full Proof Bourbon 750ml`, `WTRK 101 BRBN`, `Titos 1.75L`).
Another system has already matched each item to a short list of **candidate catalogue names**. Those candidates sit
in a BigQuery table (`topk_options`) or in a CSV export of it.

Human taggers used to look at each item, ask Google's AI Overview a fixed question, and pick the right name. This
product asks LLMs the same question, three times over, and keeps everything in Postgres:

| Pass | Command | Writes to | Who answers | What it does |
|---|---|---|---|---|
| 1 | `run` | `llm_tags` | the LLM you pick | picks a catalogue name, or `Misc - Not a Liquor`, or `None of the above` plus a new Brand + Label name, or **skips** |
| 1 again | `rerun` | a table you name (e.g. `llm_tags_v2`) | the LLM you pick | pass 1 on the same rows with a different prompt, for prompt experiments |
| 2 | `retag` | `llm_retags` (or `--table`) | a **different provider** from pass 1 | reviews pass 1's answer: AGREE, DISAGREE (with the correct answer) or IGNORED |
| 3 | `final` | `llm_final_tags` | one judge LLM | for rows where 1 and 2 disagree: names the dispute, checks it, picks MODEL_1, MODEL_2 or HUMAN |
| vote | view `llm_tag_votes` | | SQL | `final_tag` by 2 of 3, with a `decision` label for every row |

Three outcomes exist for every record: a **tag**, a **skip** (`tag` NULL, `skip_reason` set, meant for a human) or
an **error** (`tag` NULL, `error` set, retried with `--retry-errors`). Skips are not errors and are never retried
automatically.

```
BigQuery topk_options ──or── CSV export
        │ fetch + clean (bigquery_source.py)          CSV rows also go to csv_records as uploaded
        ▼
   SourceRow ──► run ──────► llm_tags ◄──── rerun --table llm_tags_v2 (same rows, other prompt)
                                │
                                ▼
                 retag ──► llm_retags          (other provider reviews pass 1)
                                │
                                ▼
          view first_second_tag_comparison      is_match / needs_third_tag
                                │ rows where needs_third_tag
                                ▼
                 final ──► llm_final_tags       (judge: MODEL_1 | MODEL_2 | HUMAN)
                                │
                                ▼
          view llm_tag_votes ──► view final_tag_summary      (the answer, with a reason per row)
          view human_tag_comparison                          (LLM vs the human tag in an uploaded CSV)
```

Postgres is the source of truth. People correct rows there, and the tagger never overwrites a stored row unless you
ask it to retry errors.

---

## 2. Repository map

```
distilliq-tagging-products/
├── .env                         DATABASE_URL, GOOGLE_APPLICATION_CREDENTIALS, optional BIGQUERY_TABLE, TAG_WORKERS
├── .env.example                 the template for .env
├── .vscode/settings.json        {"python.terminal.useEnvFile": true}
├── .venv/                       virtual environment (generated)
├── requirements.txt             pinned dependencies (section 3)
├── README.md                    quick start for humans
├── COMMANDS.md                  every command in the order you'd run them, plus result SQL
├── OVERVIEW.md                  one-page summary
├── BUILD-GUIDE.md               this file
├── records-tina.csv             a human-tagged export (columns in 2.3), used with `import` / `run --csv`
├── records-tina-100.csv         the first 100 rows of the same
├── tags_export_first_100.csv    early `export` outputs, reference only
├── tags_export_last_100.csv
├── taggings/                    CSVs exported from the views for review; nothing reads them back
├── secrets/                     never committed (section 2.2)
│   ├── claude.json              anthropic, claude-haiku-5-5
│   ├── chatgpt.json             openai, gpt-4.1-mini
│   ├── gemini.json              gemini, gemini-3.1-flash-lite, effort low
│   ├── final-tagger.json        gemini, gemini-3.5-flash-lite: the default LLM for pass 3
│   ├── gemini-web.json          gemini through a local proxy (base_url), all prices 0
│   └── distilliq-dataset-read.json   Google service account for BigQuery (no api_key, so not an LLM file)
├── tagger/                      the package; run with `python -m tagger`
│   ├── __init__.py              empty
│   ├── __main__.py              CLI: run, rerun, retag, final, import, export; the parallel loop
│   ├── config.py                .env settings and the secrets/*.json LLM files
│   ├── budget.py                model prices and the spending cap
│   ├── bigquery_source.py       SourceRow, row cleaning, BigQuery and CSV readers
│   ├── claude_tagger.py         the question, the three tool schemas, result handling shared by every provider, the Claude caller
│   ├── openai_tagger.py         OpenAI Responses API caller
│   ├── gemini_tagger.py         Gemini API caller (Grounding with Google Search)
│   ├── gemini_web_tagger.py     Gemini through a local OpenAI-style proxy
│   ├── final_tagger.py          pass 3 request builder
│   ├── store.py                 Postgres tables, migrations, views, reads and upserts
│   ├── system_prompt.md         pass 1 prompt (238 lines)
│   ├── system_prompt_v2.md      a shorter pass 1 prompt that relies on model knowledge instead of search; used with `rerun`
│   ├── retag_system_prompt.md   pass 2 prompt
│   └── third_tag_system_prompt.md   pass 3 prompt
└── tests/
    ├── test_clean_row.py        unittest: read_csv and clean_row
    ├── test_retag.py            pytest: request builders, result mapping, the parallel loop, Ctrl+C, table-name check
    └── test_gemini_web.py       pytest: the proxy tagger with a faked client
```

### 2.1 Root settings

`.env` (loaded by `python-dotenv` with `override=True`, so it beats stale values in the terminal):

```
DATABASE_URL=postgresql+psycopg://user:password@host:5432/dbname
GOOGLE_APPLICATION_CREDENTIALS=secrets/distilliq-dataset-read.json
# optional
BIGQUERY_TABLE=internal-apps-465610.dev_proximo_cantag.topk_options
TAG_WORKERS=4
```

A relative `GOOGLE_APPLICATION_CREDENTIALS` is resolved from the project root and written back to `os.environ` as an
absolute path, because the BigQuery client reads the variable itself.

### 2.2 `secrets/`: one JSON file per LLM

Picked on the command line by name: `--llm claude` → `secrets/claude.json`. A path works too.

```json
{"provider": "anthropic", "model": "claude-haiku-5-5", "effort": "medium", "api_key": "sk-ant-..."}
{"provider": "openai",    "model": "gpt-4.1-mini",     "effort": "medium", "api_key": "sk-..."}
{"provider": "gemini",    "model": "gemini-3.1-flash-lite", "effort": "low", "api_key": "AIza..."}
{"provider": "gemini",    "model": "gemini-auto", "base_url": "http://localhost:4981/openai/v1",
 "api_key": "anything", "input_price": 0, "output_price": 0, "search_price": 0}
```

| Key | Required | Meaning |
|---|---|---|
| `api_key` | yes | A file without one is not an LLM file (that's how the service account is skipped). `REPLACE_ME` in it is rejected. |
| `provider` | if the key prefix doesn't tell | `anthropic`, `openai` or `gemini`. Must agree with the key prefix when the prefix is known. |
| `model` | no | default per provider: `claude-opus-5-5`, `gpt-5.5`, `gemini-3.8-flash` |
| `effort` | no | `low`, `medium` (default) or `high`; `--effort` overrides it |
| `input_price`, `output_price` | if the model isn't in `budget.PRICES` | USD per 1M tokens |
| `search_price` | no | USD per web search; defaults in section 5.2 |
| `base_url` | no | set only for the Gemini-web proxy; selects `GeminiWebTagger` |

### 2.3 Data files

`records-tina*.csv` are exports of the human tagging tool. Columns: `project, source_row_id, base_value, options,
record_status, final_tags, resolution, vote_count, voters, resolved_at, tag_1`. `options` is `a | b | c | None`.
Human tags come in `final_tags` (preferred) or `tag_1`, and a made-up name is written `Others: <name> <description>`.
Both `run --csv` and `import` store every column of every row in `csv_records.data`, which is what
`human_tag_comparison` reads.

`taggings/*.csv` are views exported for review (`final_tag_summary` columns, comparison runs, retag batches). They
are outputs only.

---

## 3. Setup

```powershell
python -m venv .venv
.venv\Scripts\activate                  # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env                  # fill in DATABASE_URL
pip install pytest                      # for tests only; not in requirements.txt
```

`requirements.txt`:

```
anthropic==2.1.0
google-cloud-bigquery==3.42.1
google-genai==2.34.0
sqlalchemy==2.0.51
psycopg[binary]==3.3.4
python-dotenv==1.0.1
openai==3.40.0
```

Postgres: the first connection creates the tables, runs the migrations and creates the views. If the database named
in `DATABASE_URL` doesn't exist, the Store connects to the server's `postgres` database and runs
`CREATE DATABASE <name> TEMPLATE template0` (template0 because it allows no connections, so the create can't fail
with "template1 is being accessed by other users").

BigQuery: only `run` without `--csv` needs it. Everything else works from Postgres alone.

---

## 4. Core data types

```python
MISC_OPTION = "Misc - Not a Liquor"      # bigquery_source.py; the "not alcoholic" tag
NONE_OPTION = "None of the above"        # claude_tagger.py; the "no name fits" answer

@dataclass(frozen=True)
class SourceRow:                         # one record to tag
    source_row_id: str
    base_value: str                      # the store's item name
    options: list[str]                   # candidate catalogue names, cleaned
    insert_dttm: datetime | None         # BigQuery insert time; the watermark

@dataclass
class TagResult:                         # one LLM decision, any pass
    tag: str | None                      # a catalogue name, MISC_OPTION, a Brand + Label name, or None
    selected_option: str | None = None   # the catalogue name chosen, if one was
    is_alcoholic: bool | None = None
    suggested_name: str | None = None    # the Brand + Label name
    confidence: str | None = None        # high | medium | low
    reason: str | None = None
    judgements: list[dict]               # one per candidate: {"option", "matches", "reason"}
    sources: list[dict]                  # up to 5 web pages: {"title", "url"}
    error: str | None = None             # set when the call or the answer failed
    skip_reason: str | None = None       # set when the model declined to decide; tag is then None
    decision: str | None = None          # pass 3 only: MODEL_1 | MODEL_2 | HUMAN
    dispute_type: str | None = None      # pass 3 only
    disputed_point: str | None = None    # pass 3 only
```

`FetchStats` counts `fetched`, `invalid` and `invalid_ids` while reading a source. `LLMConfig` and `Settings` are in
section 5.1. `TaggingError` is the exception a provider raises for "the model answered, but not usably"; every
provider catches it and returns a `TagResult` with `error` set.

---

## 5. Module by module

### 5.1 `config.py`

- `load_settings()` → `Settings(database_url, google_credentials, bigquery_table, tag_workers)`. Defaults: table
  `internal-apps-465610.dev_proximo_cantag.topk_options`, 4 workers.
- `EFFORTS = ("low", "medium", "high")`, `DEFAULT_EFFORT = "medium"`.
- `DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "openai": "gpt-5.5", "gemini": "gemini-3.8-flash"}`.
- `provider_of_key(key)`: `sk-ant-` → anthropic, other `sk-` → openai, `AIza` or `AQ.` → gemini, else `None`.
- `provider_of_model(model)`: starts with `claude` → anthropic, `gemini` → gemini, else openai. Used by `retag` to
  know who tagged pass 1 from the stored `model` column.
- `load_llm_config(name)` → `LLMConfig(name, provider, model, api_key, input_price, output_price, search_price,
  effort, base_url)`. Exits with a plain message on: file not found (lists the available LLM files), invalid JSON,
  no `api_key`, placeholder key, unknown provider, provider/key mismatch, unknown effort, or no price for the model.
- `_default_search_price(provider, model)`: openai `gpt-4*`/`gpt-3*` → `0.025`; gemini-2* → `0.035`; other gemini
  → `0.014`; else `0.01`.
- `find_llm_configs(provider)`: the LLM files in `secrets/` for that provider. `find_llm_config(provider)` exits
  if there are none or several.

### 5.2 `budget.py`

`PRICES` is `{model: (input, output)}` in USD per 1M tokens, for the Claude 4.8–5.x, GPT-4.1/5.5 and Gemini 2.5–4
models the team uses (see the file; Gemini prices are the post-promotion ones, and a note says which promotions end
when). Search prices: `SEARCH_PRICE = 0.01`, `NON_REASONING_SEARCH_PRICE = 0.025` (OpenAI gpt-4.x),
`GEMINI_SEARCH_PRICE = 0.014` (per query), `GEMINI_2_SEARCH_PRICE = 0.035` (per grounded prompt).
`CACHE_WRITE, CACHE_READ = 1.25, 0.1` are multiples of the input price.

`Budget(limit)`: `check()` raises `BudgetExceeded` once `spent >= limit`; every provider calls it before each
request. `charge(cost)` adds under a lock. Requests already in flight finish, so a run can end a little over the
limit.

### 5.3 `bigquery_source.py`

**`parse_options(raw)`**: takes a list or an `a | b | c` string; trims, drops empties and duplicates (first wins).

**`clean_row(id, base_value, raw_options, insert_dttm)`** → `SourceRow` or `None`:

1. Trim the id and the item name.
2. If there are no options, or every option is `None` / `Misc - Not a Liquor` (case-insensitive), the row is a
   **Misc row**: options become `[MISC_OPTION]`, and the model gets no candidates and names the product itself.
3. Invalid (→ `None`, counted in `stats.invalid`): empty id, empty item name, or fewer than 2 options when it isn't a
   Misc row. (`None` is always one of the options in the source, so a real row has at least one name plus `None`.)

**`fetch_rows(table, watermark, stats)`**: runs

```sql
SELECT CAST(sourceRowId AS STRING) AS source_row_id, baseValue AS base_value, options AS options,
       TIMESTAMP(insert_dttm) AS insert_dttm
FROM `<table>`
WHERE @wm IS NULL OR TIMESTAMP(insert_dttm) >= @wm
ORDER BY insert_dttm
```

with `page_size=5000`, cleaning each row.

**`read_csv(path)`**: opens with `utf-8-sig` (Excel BOM), finds the id/item/options columns case-insensitively under
either name (`sourceRowId`/`source_row_id`, `baseValue`/`base_value`, `options`; optional
`insert_dttm`/`source_insert_dttm`), renames them, keeps every other column as is, and drops rows where every cell is
blank (spreadsheet exports end with those). Exits if a required column is missing.

**`fetch_csv(path, stats)`**: `read_csv` → `clean_row`. `insert_dttm` is parsed with `fromisoformat` after replacing
` UTC` with `+00:00`; unparseable → `None`.

### 5.4 `claude_tagger.py`: the shared core

#### The question

Every pass asks the item the way the human taggers asked AI Overview. `options` are the candidates (section 5.4
"candidates"), then `Misc - Not a Liquor`, then `None of the above`, joined with ` OR `:

```
Which name most properly identifies {item} - {names} ? Is {item} an alcoholic or non-alcoholic item ? If none of the names identify {item}, what is the most proper Brand + Label name for {item} ?
```

`item` is `base_value` with whitespace runs collapsed (`clean_item`).

**`candidates(row)`** drops `None`, `Misc - Not a Liquor` (also spelled `<Misc - not a Liquor>`, see `is_misc`) and
`None of the above` from `row.options`. So a Misc row has no candidates and the names list is just
`Misc - Not a Liquor OR None of the above`.

**`user_message(item, options, note)`**: the question, then the note. When the note starts with `<model_1_output>`
(passes 2 and 3), the question is wrapped in `<question>...</question>` first, because that is the input format the
review and judge prompts describe.

#### The three tools

All are strict JSON schemas (`additionalProperties: false`, every field required). `DECISION_TOOLS` is the tuple of
their names; a provider accepts a call to any of them.

| Tool | Pass | Fields |
|---|---|---|
| `record_tag` | 1 | `answer` (copied exactly from the question), `name_reasons[{name, reason}]`, `is_alcoholic`, `brand_label_name`, `skip_reason`, `confidence` (high/medium/low), `reason` |
| `record_review` | 2 | `verdict` (AGREE/DISAGREE/IGNORED), `correct_answer`, `correct_brand_label_name`, `correct_is_alcoholic` (bool or null), `explanation` |
| `record_decision` | 3 | `dispute_type` (SPELLING/EXPRESSION/PRODUCT/MATCH/ALCOHOLIC/SKIP/FORMAT), `disputed_point`, `decision` (MODEL_1/MODEL_2/HUMAN), `final_tag`, `final_brand_label_name`, `final_is_alcoholic` (bool or null), `explanation` |

#### Parsing an answer that came back as text

`parse_json(text)` finds a JSON object in a reply: inside a ```` ```json ```` fence, or from the first `{` to the last
`}`. Every provider falls back to it when no tool call is present, because "Return only this JSON" prompts
sometimes get the JSON written out instead of sent to the tool. Gemini and the proxy rely on it entirely.

#### Turning any answer into a `TagResult`: `_result(options, decision, sources)`

This one function serves all three passes and all four providers.

1. If the decision has `dispute_type`, it is a judge answer → `_from_judge`. Else if it has `verdict`, it is a review
   → `_from_review`. Both produce `record_tag`-shaped fields (`answer`, `brand_label_name`, `is_alcoholic`, `reason`,
   `skip_reason`).
2. Names are compared with `_key()`: lowercase, every non-alphanumeric run → one space, trimmed.
3. `chosen` = the first option whose key equals the answer's key. `judgements` gets one entry per option:
   `matches = (option == chosen)`, `reason` from `name_reasons` by name key, else by position, else `""`.
4. Then, in order:
   - `skip_reason` non-empty → a **skip**: `tag`, `selected_option` and `suggested_name` are `None`.
   - `chosen` found → `tag = selected_option = chosen`.
   - answer is Misc, or answer is `None of the above` with `is_alcoholic is False` → `tag = MISC_OPTION`.
   - answer is `None of the above`, or answer equals the Brand + Label name → `tag = brand_label_name`; with no
     name, `error = "No name fit and no Brand + Label name was suggested"`.
   - anything else → `error = "Chose '<answer>', which wasn't one of the names offered"`.
5. `sources` is cut to 5. `decision`, `dispute_type`, `disputed_point` are filled only for a judge answer.

**`_from_review(review)`**: `answer = correct_answer`, `brand_label_name = correct_brand_label_name`, `reason =
explanation`. It is a skip (`skip_reason = explanation or "unclear"`) when the verdict is IGNORED, or when there is no
name and the answer is empty or is `None of the above` on an item not known to be non-alcoholic. So a reviewer who
says "pass 1 guessed" without naming the product produces a skip, not an error.

**`_from_judge(judge)`**: `decision == "HUMAN"` → a skip with `skip_reason = explanation or "needs human review"`.
Otherwise it is treated as a DISAGREE review built from `final_tag`, `final_brand_label_name`, `final_is_alcoholic`.
A win for a side that itself skipped (empty `final_tag`, no name) therefore stays a skip.

**`first_pass_output(first)`** rebuilds pass 1's answer from its stored row for passes 2 and 3 to read:
`(options, {"answer", "is_alcoholic", "brand_label_name", "skip_reason", "name_reasons", "reason"})`.
- A tag starting with `Others:` (old rows) has the prefix removed.
- A Misc tag in either spelling → answer `Misc - Not a Liquor`.
- If `selected_option` is empty and the tag is a made-up name (it differed from the raw tag, or equals
  `suggested_name`) → answer `None of the above`, `brand_label_name = name`, and the name is **removed from
  `options`**, because `save()` appended it there and it isn't a real catalogue name.
- A skipped or failed pass 1 row gives `answer ""` with its `skip_reason` (or `""`).

#### `ClaudeTagger`

- `anthropic.Anthropic(api_key, timeout=180.0, max_retries=4)`.
- Tools: a web search tool plus the decision tool, or only the decision tool with `--no-web-search`. The search tool
  is `web_search_20250305` on Haiku (`"haiku"` in the model name) and `web_search_20260209` otherwise, `max_uses: 2`.
- `client.beta.messages.create(model, max_tokens=16000, system, messages, tools, output_config={"effort": effort})`,
  plus `betas=["server-side-fallback-2026-07-01"], fallbacks="default"` except on Haiku, which doesn't support it.
- **No prompt caching**: no `cache_control`. The owner chose this.
- `pause_turn` loop: a long server-side search can pause the turn; the assistant content is appended and the request
  sent again unchanged, at most 3 more times. Budget is checked before and charged after every send.
- `_decision`: `stop_reason == "refusal"` → `TaggingError("Claude declined to tag this record")`; else the first
  `tool_use` block named in `DECISION_TOOLS`; else `parse_json` of the text blocks; else
  `TaggingError("No decision recorded (stop_reason=...)")`.
- `_sources`: from `web_search_tool_result` blocks whose content is a list (an error result is a single object),
  unique by URL, title or URL.
- Cost: `(input + cache_write×1.25 + cache_read×0.1) × in_price + output × out_price` per 1M, plus
  `server_tool_use.web_search_requests × search_price`.
- `APIStatusError` → `error = "API error <status>: <message>"`; `APIConnectionError` → `"Connection error: ..."`.

### 5.5 `openai_tagger.py`

Same constructor signature `(llm, budget, effort, web_search, system_prompt, tool)` as every tagger. Differences:

- `openai.OpenAI(api_key, timeout=180.0, max_retries=4)`; `client.responses.create(model, max_output_tokens=16000,
  instructions=system_prompt, input=message, tools, include=["web_search_call.action.sources"])`.
- The decision tool becomes `{"type": "function", "name", "description", "strict": True, "parameters": input_schema}`;
  web search is `{"type": "web_search"}`.
- `reasoning={"effort": effort}` except for models starting with `gpt-4` or `gpt-3`, which reject it.
- `_decision`: the first `function_call` item named in `DECISION_TOOLS` (`json.loads(arguments)`; bad JSON is a
  `TaggingError`), else `parse_json(output_text)`, else `TaggingError("No decision recorded (status=...)")`.
- Cost: `(input − cached) × in + cached × in × 0.1 + output × out` per 1M (output includes reasoning tokens), plus one
  `search_price` per `web_search_call` item.
- Sources: `web_search_call` items with `action.type == "search"`, from `action.sources`.

### 5.6 `gemini_tagger.py`

Gemini can't combine Google Search grounding with function calling, so the decision comes back as JSON text:

- `genai.Client(api_key, http_options=HttpOptions(timeout=180_000, retry_options=HttpRetryOptions(attempts=5)))`.
- Config: `system_instruction`, `max_output_tokens=16000`, `thinking_config`, and `tools=[Tool(google_search=...)]`
  unless `--no-web-search`.
- Gemini 3+ (or any model without search): `response_mime_type="application/json"` and
  `response_json_schema=_schema(tool["input_schema"])`, where `_schema` strips `additionalProperties` and `strict`.
- Gemini 2.x **with** search can't take a schema, so for `record_tag` the prompt gets `JSON_ANSWER` appended, which
  spells out the JSON shape. The review and judge prompts already spell out their JSON, so they get nothing.
- Thinking: Gemini 2.x uses `thinking_budget` {low: 1024, medium: 4096, high: 16384}; Gemini 3 uses
  `thinking_level`, and Pro only accepts LOW or HIGH, so `medium` becomes HIGH on non-Flash models.
- `_decision`: no candidates → declined with `prompt_feedback.block_reason`; `finish_reason` in
  {SAFETY, BLOCKLIST, PROHIBITED_CONTENT, SPII, RECITATION} → declined; else `parse_json` of the non-thought text
  parts.
- Sources: `grounding_metadata.grounding_chunks[].web.{uri,title}`.
- Cost: `(prompt + tool_use_prompt − cached) × in + cached × in × 0.1 + (candidates + thoughts) × out` per 1M, plus
  `len(web_search_queries) × search_price`, capped at 1 on Gemini 2.x (billed per grounded prompt).
- Errors: `genai.errors.APIError` → `"API error <code>: <message>"`; `httpx.TransportError` → connection error.

### 5.7 `gemini_web_tagger.py`

For a local proxy that signs into Gemini's web app with a browser cookie and exposes an OpenAI-style chat API
(`base_url` in the LLM file, e.g. `http://localhost:4981/openai/v1`).

- No function calling, no usage, no sources, no effort or search control. The decision tool's JSON schema is appended
  to the system prompt; the reply is parsed with `parse_json`; cost is 0; `sources` is `[]`.
- `openai.OpenAI(base_url, api_key, timeout=300.0, max_retries=6)` (the proxy 429s at ~30 requests/minute).
- `_wait_turn()` spaces call starts 4–9 s apart (random) across all workers, under a lock, so the session looks less
  like a bot.
- A connection error names the proxy URL in the message.

### 5.8 `final_tagger.py`: pass 3 request

`disputed_request(row)` takes a `first_second_tag_comparison` row and returns `(SourceRow, note)`:
- `options` and `<model_1_output>` come from `first_pass_output` (so a made-up pass 1 name is removed from the
  options and shown as `None of the above` + `brand_label_name`).
- `<model_2_output>` comes from `_second_pass_output`: `correct_answer = tag_2` (Misc normalised; a tag equal to
  `suggested_name_2` becomes `None of the above` + name), `correct_is_alcoholic`, `skip_reason` (pass 2's, or
  `"the second pass gave no result"` when pass 2 has no tag and no skip reason, i.e. it errored), `explanation =
  reason_2`.
- `insert_dttm` is `None`; the SourceRow's `source_row_id` is stringified.

### 5.9 `store.py`: Postgres

SQLAlchemy Core, `psycopg` driver, `pool_pre_ping=True`, `connect_timeout=10`. `Store.__init__`:
`metadata.create_all` (creating the database first if it doesn't exist) → `MIGRATIONS` → the five views →
`three_way_tag_comparison` only if the old `claude_tags` table exists.

Every write is `_upsert`: `INSERT ... ON CONFLICT (source_row_id) DO UPDATE` of every column except the key, with
`tagged_at = now()`.

#### Tables

**`llm_tags`** (pass 1; `rerun` tables are clones of it):

| Column | Type | Notes |
|---|---|---|
| `source_row_id` | String PK | |
| `base_value` | Text not null | |
| `options` | String[] not null | the cleaned candidates; **a Brand + Label name the model made up is appended** so the tag always reads like one of the options |
| `tag` | Text | catalogue name, `Misc - Not a Liquor`, Brand + Label name, or NULL (skip or error) |
| `selected_option` | Text | the catalogue name chosen, else NULL |
| `is_alcoholic` | Boolean | |
| `suggested_name` | Text | the Brand + Label name |
| `confidence` | String(10) | |
| `reason` | Text | |
| `judgements` | JSONB default `[]` | one per option |
| `sources` | JSONB default `[]` | |
| `error` | Text | |
| `skip_reason` | Text | |
| `model` | String(64) not null | |
| `source_insert_dttm` | timestamptz | the watermark |
| `tagged_at` | timestamptz default now() | |

**`llm_retags`** (pass 2; `--table` clones it): `source_row_id` PK, `base_value`, `first_tag` (pass 1's tag then;
NULL when pass 1 skipped or failed), `first_model`, `tag`, `is_alcoholic`, `suggested_name`, `confidence`, `reason`,
`sources`, `error`, `skip_reason`, `model`, `tagged_at`. No `options`, `selected_option` or `judgements`.

**`llm_final_tags`** (pass 3): `source_row_id` PK, `base_value`, `options` (exactly as shown to the judge), `tag_1`,
`tag_2`, `tag_1_model`, `tag_2_model`, `tag`, `selected_option`, `is_alcoholic`, `suggested_name`, `confidence`,
`reason`, `judgements`, `sources`, `error`, `skip_reason`, `model`, `tagged_at`, `decision` (String(10):
MODEL_1 | MODEL_2 | HUMAN), `dispute_type` (String(20)), `disputed_point`.

**`csv_records`** (every row of every CSV given to `run --csv` or `import`): `source_row_id` PK, `base_value`,
`options` (the raw `a | b | c` string), `source_file` (basename), `data` (JSONB: the whole row, human tags included),
`tagged_at` (upload time).

**`claude_tags`**: an old pass 1 table from before the `llm_*` rename. Not in `metadata`, never written; only read by
`three_way_tag_comparison` when present.

**Migrations** (`ADD COLUMN IF NOT EXISTS` / `DROP NOT NULL`, safe to re-run): `skip_reason` on `llm_retags` and
`llm_final_tags`; `decision`, `dispute_type`, `disputed_point` on `llm_final_tags`; `llm_retags.first_tag` nullable.

**`tags_table(name, like=llm_tags)`**: a table shaped like `like`, created if missing. The name must match
`[a-z_][a-z0-9_]{0,62}` and must not be one of the declared tables (`llm_tags`, `llm_retags`, ...), so `rerun --table
llm_tags` is refused before touching the database. `retag` maps the default names to the declared tables itself.

#### Views

The exact SQL is in `store.py`; copy it. Column order matters: `CREATE OR REPLACE VIEW` can only append columns, so
new ones go last.

**`llm_tag_comparison`**: `llm_tags` LEFT JOIN `llm_retags` LEFT JOIN `llm_final_tags`. `tag_1` and `tag_2` are the
stored tags with a leading `Others: ` removed and `<Misc - not a Liquor>` turned into the plain form. `tags_match` is
NULL until a retag row exists, then `lower(trim(tag_1)) = lower(trim(tag_2))` with NULLs → false. Also `tag_3`,
every pass's model, `is_alcoholic_1/2/3`, `is_alcoholic_any`, `skip_reason_1/2/3`.

**`first_second_tag_comparison`** (the input to pass 3): everything from both passes side by side, plus
- `is_match`: NULL when `tag_1` is NULL; else the comparison when `tag_2` is set; else `false` when pass 2 skipped;
  else NULL (pass 2 not run yet, or errored).
- `needs_third_tag = is_match IS NOT TRUE AND tagged_at_2 IS NOT NULL`: every row pass 2 has tried (even a failed
  try) whose tags are not a confirmed match. A pass 1 skip therefore always reaches the judge once pass 2 has run.

**`llm_tag_votes`**: `final_tag` and `decision` for every pass 1 row, evaluated top to bottom:

| `decision` | when | `final_tag` |
|---|---|---|
| `agreed` | `tags_match` | `tag_1` |
| `final agrees with 1` / `final agrees with 2` | `tag_3` equals `tag_1` / `tag_2` (case- and space-insensitive) | `tag_3` |
| `skipped` | pass 3 skipped (`skip_reason_3`) | NULL |
| `no majority` | pass 3 answered but agrees with neither | NULL |
| `both not alcoholic` | `NOT is_alcoholic_any` (both passes say false) | `Misc - Not a Liquor` |
| `needs retag` | `tags_match IS NULL` (no pass 2 row) | NULL |
| `needs final` | otherwise | NULL |

Also `is_alcoholic` (passes 1 and 2 when they agree, else pass 3), the three models, and `skip_reason`
(`skip_reason_3`, else `skip_reason_1`, for rows without a tag).

**`final_tag_summary`**: one row per item for people: `first_tag`, `second_tag`, `third_tag`, `final_tag`,
`final_tag_reason` (a sentence per `decision`, followed by the relevant model's reason or error),
`first_tag_sources`, `second_tag_sources`, `first_tagged_at`.

**`human_tag_comparison`**: `csv_records` LEFT JOIN `llm_tag_votes`. `human_tag` = `final_tags` else `tag_1` from the
uploaded row; `llm_tag` = `final_tag` else pass 1's `tag_1`. Keys ignore `Others: `, case, punctuation and spacing.
`comparison` ∈ `no human tag`, `llm skipped`, `not tagged by llm`, `same`, `human skipped` (human wrote `None`),
`both new name` (human wrote `Others:` and the LLM's tag isn't one of the options or Misc; check by eye, because the
human's name usually has a description after it), `different`.

**`three_way_tag_comparison`**: `csv_records` ∪ `claude_tags` ∪ `llm_tags` by id, with `claude_vs_llm`,
`human_vs_claude`, `human_vs_llm` and `claude_vs_llm_vs_human` labels. Only for the old-vs-new comparison.

#### Store methods

| Method | Does |
|---|---|
| `watermark()` | `MAX(llm_tags.source_insert_dttm)` |
| `tagged_ids(include_errors, table=llm_tags)` | ids in the table; `include_errors=False` leaves out rows with `error` |
| `tags_table(name, like)` | clone table, see above |
| `save(row, result, model, table=llm_tags)` | pass 1 upsert; appends the made-up name to `options` when the tag is that name |
| `error_rows()` | pass 1 rows with an error, oldest first, as stored (human edits included) |
| `first_pass_rows(table)` / `first_pass_models(table)` | every row / the distinct models in a pass 1 table |
| `retagged_ids(include_errors, table)` / `save_retag(first, result, model, table)` | pass 2 |
| `disputed_rows()` | `SELECT * FROM first_second_tag_comparison WHERE needs_third_tag ORDER BY source_row_id` |
| `final_tagged_ids(include_errors)` / `save_final(disputed, row, result, model)` | pass 3; stores `tag_1/2`, their models, and the judge's `decision`, `dispute_type`, `disputed_point` |
| `save_csv(path, rows)` | upserts `csv_records`; rows with a blank id or item are dropped; a duplicate id in one file keeps the later row (one statement can't upsert the same id twice) |
| `all_rows()` / `all_retags()` | for `export` |

### 5.10 `__main__.py`: the command line

`python -m tagger <command>`. `main()` installs the Ctrl+C handler, loads settings, parses, and sets `--limit 5`
when `--dry-run` is given without a limit.

**Shared options** on `run`, `rerun`, `retag`, `final`:

| Option | Default | Meaning |
|---|---|---|
| `--max-cost USD` | 15 | spending cap; the run stops when reached and the unreached rows are left for the next run |
| `--limit N` | all (5 with `--dry-run`) | tag at most N rows |
| `--retry-errors` | off | also redo rows whose stored result has `error` set (never skips) |
| `--workers N` | `TAG_WORKERS` or 4 | parallel requests |
| `--effort low\|medium\|high` | the LLM file's, else medium | reasoning effort |
| `--no-web-search` | off | send only the decision tool |
| `--dry-run` | off | print one JSON line per row, save nothing; **still calls the model and costs money** |

**Provider dispatch** `_tagger(llm, budget, **kw)`: `base_url` set → `GeminiWebTagger`; provider `openai` →
`OpenAITagger`; `gemini` → `GeminiTagger`; else `ClaudeTagger`. Every tagger has `.model`, `.tag(row, note=None)`.

**`run --llm NAME [--full] [--csv PATH]`** (pass 1)
1. Store is opened unless `--dry-run`, so a dry run needs no Postgres.
2. `skip` = every id already in `llm_tags`, errors included.
3. Source: with `--csv`, the file is first uploaded to `csv_records` (when there's a Store), then read; otherwise
   BigQuery from the watermark (`None` with `--full` or on a dry run).
4. With `--retry-errors`, the stored error rows go first, rebuilt from Postgres (`options` as stored).
5. Rows in `skip` are counted as `already_tagged` and not sent.
6. Summary: fetched, invalid, already tagged, tagged, skipped, failed, spent. After a budget stop: "add `--full`"
   (BigQuery) or "same command" (CSV).

**`rerun --llm NAME --prompt FILE --table NAME`** (pass 1 again)
- `--prompt` is a file in `tagger/`, e.g. `system_prompt_v2.md`. `--table` is created with `tags_table` (not on a dry
  run). Rows come from `llm_tags`, with `first_pass_output` so the model sees the original catalogue options without
  any name pass 1 invented. Rows already in the target table are skipped (errors too, unless `--retry-errors`).
  Needs Postgres even on a dry run.

**`retag [--llm NAME ...] [--from TABLE] [--table TABLE]`** (pass 2)
1. `--from` (default `llm_tags`) is the pass 1 table; `--table` (default `llm_retags`) is where reviews go, created
   with the `llm_retags` shape if missing. So `retag --from llm_tags_v2 --table llm_retags_v2` reviews a `rerun`
   experiment without touching the main tables.
2. Configs: `--llm` files (one per provider, else exit), or without `--llm` the single file per provider found in
   `secrets/` in `RETAG_PREFERENCE = ("gemini", "anthropic", "openai")` order (several files for one provider → exit,
   pick one with `--llm`).
3. Routing: for each distinct `model` in the pass 1 table, `_other_provider` picks the first provider in
   `RETAG_PREFERENCE` that is not the one that tagged it and has a config. No such provider → exit with a message
   naming the alternatives. One tagger per provider is built with `RETAG_SYSTEM_PROMPT` and `RECORD_REVIEW_TOOL`, and
   the routes are printed.
4. Every pass 1 row not yet in the target (errors included unless `--retry-errors`) is sent as
   `_retag_request(first)`: the SourceRow with the real options, and a note `<model_1_output>{json}</model_1_output>`
   built by `first_pass_output` plus pass 1's `confidence`. Skipped and failed pass 1 rows are reviewed too.
5. Saved with `save_retag`, which records `first_tag` and `first_model` as they were.

**`final [--llm NAME]`** (pass 3)
- Default `--llm final-tagger`. Tagger built with `FINAL_SYSTEM_PROMPT` and `RECORD_DECISION_TOOL`.
- Rows: `disputed_rows()` minus `final_tagged_ids` (errors kept unless `--retry-errors`). Each is sent as
  `disputed_request(row)` and saved with `save_final`. Summary: disputed, already final, tagged, skipped, failed.

**`import PATH...`**: `save_csv(read_csv(path))` for each file, no tagging.

**`export PATH [--retags]`**: writes `llm_tags` (or `llm_retags`) to CSV, every column; `options` joined with ` | `,
`judgements` and `sources` as JSON. Only these two tables; for views use your database tool (COMMANDS.md shows
`psql \copy`).

**The parallel loop**

```python
def _tag_rows(rows, tag, workers):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while not over_budget and (window := list(islice(rows, workers * 4))):
            futures = {pool.submit(tag, row): row for row in window}
            pending = set(futures)
            while pending:
                done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)  # timeout: Windows Ctrl+C
                for future in done:
                    try: result = future.result()
                    except BudgetExceeded: over_budget, result = True, None
                    yield futures[future], result
```

Rows are pulled lazily in windows of `workers × 4`, so a big table is never fully in memory and a budget stop wastes
at most one window. A `None` result means "not attempted, budget gone": it is **not saved**, so the next run picks it
up.

`_run_loop(rows, tag, save, args, budget, model)` drives it: dry run → `_print_dry` (JSON line with the row, model
and the `TagResult` as a dict); else `save(row, result)`. It counts `tagged` / `skipped` (`skip_reason`) / `failed`
(`error`), prints each failure and skip to stderr, prints progress and spend every 50 rows, and returns
`(tagged, skipped, failed, stopped)`.

`_stop_on_ctrl_c()`: SIGINT prints "Stopped. Saved rows are kept..." and calls `os._exit(130)`. A normal exit would
wait for every queued LLM call and then throw the results away; Postgres rolls back any save in progress.

`_store(settings)`: exits with a plain message when `DATABASE_URL` is unset or the connection fails.

---

## 6. The prompts

All four live in `tagger/` and are read at import time. Copy them verbatim; the wording has been tuned against real
data. What each one does:

**`system_prompt.md`** (pass 1, default): a procedure the model must follow in order.
- Step 0, the **brand gate**: from the item text alone, before any search. Obviously not a drink → Misc. No brand,
  only generic words (`Vodka 750ml`) → skip with `skip_reason "item text has no brand"`, no search, never borrow a
  brand from the candidates. Possible brand → continue.
- Step 2: search only when the item names no liquor type, or to get the official name; never to confirm.
- Step 4: every candidate gets MATCH, MATCH NO TYPE (same product but the name lacks a liquor type; cannot be chosen)
  or NO MATCH with a reason → `name_reasons`.
- Matching rules: ignore size, pack, proof/ABV (unless part of the name), case/punctuation/spacing, abbreviations,
  accents, and a liquor type the catalogue adds. Brand, line, expression, age must be identical; no extra or missing
  words; brand-only names don't match; "Variety Pack" in both or neither. Several matches → the first listed.
- Brand + Label name: Title Case, abbreviations written out, order brand/age/line/expression/type, must include the
  type, no sizes/packs/proof/codes.
- **Output characters**: everything the model writes uses plain ASCII (accents stripped, ™®© removed, straight
  quotes), except `answer`, which is copied exactly. Records cross systems with different encodings.
- Eight worked examples and a final checklist.

**`system_prompt_v2.md`**: the same rules in a third of the length, with Step 2 changed to "identify the product from
your own knowledge; if you don't recognise it, skip" instead of searching. Used with `rerun` to compare prompts.

**`retag_system_prompt.md`** (pass 2): "be correct, not strict": agreeing is the expected outcome. Step 1: no-brand
rows → IGNORED. Step 2: work out the right answer yourself before reading pass 1's reasoning. Step 3: a list of what
counts as an error (wrong brand/expression/age, extra or missing words, no liquor type, a later match when an earlier
one matched, variety pack mismatch, wrong `is_alcoholic`, bad Brand + Label name, skipped an identifiable product or
guessed an unclear one, answer not copied exactly) and what does not (loose wording, equivalent name phrasing,
judgment calls, confidence level). Output is the `record_review` JSON.

**`third_tag_system_prompt.md`** (pass 3): the judge is not a third tagger. Name the one `dispute_type` and a
checkable `disputed_point`; check that point yourself (search if the text and rules don't settle it); pick MODEL_1 or
MODEL_2, or HUMAN when sources don't settle it, conflict, both are wrong, or a match can't be confirmed. Neutrality
rules: Model 2 saw Model 1's answer, so it isn't independent; reasons are claims, not evidence. For SPELLING/FORMAT
disputes pick the better-formed name and copy it exactly. Output is the `record_decision` JSON.

---

## 7. Worked data flow for one record

Item `Titos 1.75L`, options `Tito's Handmade | Smirnoff Vodka | None`.

1. `clean_row` → `SourceRow(id, "Titos 1.75L", ["Tito's Handmade", "Smirnoff Vodka", "None"], ts)`.
2. `candidates` → `["Tito's Handmade", "Smirnoff Vodka"]`. Question: `Which name most properly identifies Titos
   1.75L - Tito's Handmade OR Smirnoff Vodka OR Misc - Not a Liquor OR None of the above ? ...`
3. Pass 1 answers `None of the above`, `brand_label_name "Tito's Handmade Vodka"` (MATCH NO TYPE can't be chosen).
   `_result` → `tag = suggested_name = "Tito's Handmade Vodka"`, `selected_option None`. `save` appends the name to
   `options`: `["Tito's Handmade", "Smirnoff Vodka", "None", "Tito's Handmade Vodka"]`.
4. `retag`: `first_pass_output` sees `selected_option` empty and the tag equal to `suggested_name`, so it removes the
   name from the options and shows `answer "None of the above", brand_label_name "Tito's Handmade Vodka"`. The
   reviewer (another provider) answers AGREE with the same fields → `tag_2 = "Tito's Handmade Vodka"`.
5. `llm_tag_comparison.tags_match = true` → `llm_tag_votes.decision = 'agreed'`, `final_tag = "Tito's Handmade
   Vodka"`. `needs_third_tag = false`, so `final` never sees it.

Had the reviewer said DISAGREE with `correct_answer "Tito's Handmade"`, `tags_match` would be false, `final` would
get both outputs, and the judge would likely rule `dispute_type FORMAT` (missing liquor type) for MODEL_1.

---

## 8. Tests

```powershell
python -m pytest tests            # 18 tests; pytest is not in requirements.txt
python -m unittest discover -s tests   # only test_clean_row.py
```

| File | Checks |
|---|---|
| `test_clean_row.py` | `read_csv` renames columns, keeps extra ones, drops blank rows; Misc/None-only rows get `[MISC_OPTION]`; one real candidate is invalid; options are trimmed and deduplicated |
| `test_retag.py` | `_retag_request` shows the chosen option, takes a made-up name back out of the options, handles `Others:` and `<Misc>` rows and rows with no first tag; `_tag_rows` yields every row and stops after one window on `BudgetExceeded`; Ctrl+C exits 130 within seconds (subprocess); `user_message` wraps `<question>` only for a review note; `_result` on reviews (AGREE, named, Misc, IGNORED, unclear → skip not error) and on judge answers (MODEL_1/2, HUMAN, a winning skip); `disputed_request` shows real names and both outputs; fenced JSON text counts as the answer; `tags_table` rejects declared and malformed names without a database |
| `test_gemini_web.py` | the proxy tagger parses a fenced JSON reply and turns a reply without JSON into an error |

No test calls a real API or database.

---

## 9. Build order, from an empty folder

1. `requirements.txt`, `.env`, `secrets/`. Check you can reach Postgres (and BigQuery if you need it).
2. `bigquery_source.py` + `tests/test_clean_row.py`.
3. `config.py`, `budget.py`.
4. `claude_tagger.py`: question, tools, `parse_json`, `_result`, `first_pass_output`, `ClaudeTagger`. Copy
   `system_prompt.md` and `retag_system_prompt.md`.
5. `__main__.py` with `run --dry-run` only. Check 5 rows of JSON.
6. `store.py` with the tables and `_upsert`; then `run`, `import`, `export`.
7. `openai_tagger.py`, `gemini_tagger.py`; `retag` with `RETAG_PREFERENCE` routing; the `llm_tag_comparison` and
   `first_second_tag_comparison` views; `tests/test_retag.py`.
8. `final_tagger.py`, `third_tag_system_prompt.md`, `final`, `llm_tag_votes`, `final_tag_summary`.
9. `human_tag_comparison`, `rerun` + `tags_table`, `retag --from/--table`, `gemini_web_tagger.py` +
   `tests/test_gemini_web.py`.

---

## 10. Acceptance checks

- [ ] `1792 Full Proof Bourbon 750ml` with `Buffalo Trace 1792 Full Proof | W L Weller Full Proof` → `None of the
      above`, name `1792 Full Proof Bourbon`, which appears last in the stored `options`.
- [ ] A candidate differing only in size is chosen; `selected_option` is set.
- [ ] `Vodka 750ml` → skip with `skip_reason` "item text has no brand", `tag` NULL, no `error`, no web search.
- [ ] A Misc/None-only row is sent with no candidates; the model names the product or answers Misc.
- [ ] A soda → `Misc - Not a Liquor`, `is_alcoholic false`.
- [ ] A bad API key → every row saved with `error`, the run finishes, `--retry-errors` redoes them.
- [ ] `--max-cost 0.01` → stops after the first window; unreached rows are not in the table.
- [ ] `run` twice → second run tags nothing and spends nothing. Edit a row in Postgres, `run` again → the edit stays.
- [ ] `retag` sends Claude-tagged rows to Gemini (or the next available provider) and never to the same provider;
      `tags_match` ignores case, spaces and `Others: `.
- [ ] `final` touches only `needs_third_tag` rows; a HUMAN decision shows as `decision = 'skipped'` in
      `llm_tag_votes`; every row has a `decision`.
- [ ] `rerun --table llm_tags` is refused; `rerun --table llm_tags_v2` then `retag --from llm_tags_v2 --table
      llm_retags_v2` leaves `llm_tags` and `llm_retags` untouched.
- [ ] `import records-tina-100.csv` → `human_tag_comparison` shows a `comparison` for each row.

---

## 11. Rules the owner has set

- Postgres is the source of truth. CSVs in the root and `taggings/` are exports, never inputs for tags.
- No prompt caching (`cache_control` stays off).
- Effort is `medium` unless asked otherwise (the Gemini file uses `low`; the file's value wins over the default).
- Never commit `secrets/` or `.env`.
- A wrong match is worse than no match: every prompt says so, and the code keeps skips separate from errors so they
  reach a person instead of being retried.

---

## 12. Things that look odd but are on purpose

- **The made-up name lives in `options`.** `save()` appends it so the tag always reads as one of the options in
  exports and views. `first_pass_output` takes it back out before passes 2 and 3 see the row.
- **Skipped and failed pass 1 rows still go through `retag` and `final`.** The reviewer may be able to name what pass
  1 couldn't; the judge sees `answer ""` for that side.
- **`needs_third_tag` includes rows whose pass 2 errored** (`tagged_at_2` is set). To re-review those instead, delete
  their `llm_final_tags` rows, run `retag --retry-errors`, then `final` (COMMANDS.md section 7).
- **`--dry-run` still spends money** and, for `rerun`/`retag`, still needs Postgres (it reads pass 1 from there).
  `retag --dry-run --table new_name` also creates the empty table.
- **`retag --from` with a misspelled table creates an empty clone** and reviews 0 rows instead of failing.
- **`Budget.check()` is advisory**: in-flight requests finish, so the final spend can exceed the cap by up to one
  window of requests.
- **Gemini 2.x + search gets the JSON shape in the prompt, not a schema**, because the API refuses both together.
- **Haiku uses the older web search tool and no server-side fallback.** Both are API limits, not choices.
- **`llm_tag_votes` evaluates pass 3 before `both not alcoholic`**: once a judge has spoken, its answer (or its skip)
  wins over the alcoholic shortcut.
- **`_tag_rows` uses `wait(timeout=0.5)` not `as_completed`**: on Windows, Ctrl+C can't interrupt a wait without a
  timeout.
- **The old `claude_tags` table** is left alone; `three_way_tag_comparison` is created only while it exists.

---

## 13. Extending

**Another provider**: write `<name>_tagger.py` with the same constructor `(llm, budget, effort, web_search,
system_prompt, tool)`, a `.model`, and `tag(row, note=None) -> TagResult` that builds the message with
`user_message(clean_item(row.base_value), candidates(row), note)`, gets a decision dict (tool call or `parse_json`),
and returns `_result(options, decision, sources)`, catching the SDK's errors into `TagResult(error=...)`. Then add
the provider to `DEFAULT_MODELS`, `provider_of_key`, `provider_of_model`, `_default_search_price`, `PRICES`,
`_tagger`, and `RETAG_PREFERENCE`.

**Another prompt experiment**: put the prompt in `tagger/`, run `rerun --prompt FILE --table llm_tags_<x>`, then
`retag --from llm_tags_<x> --table llm_retags_<x>`, and compare with a view over the two tables (the main views only
read `llm_tags`/`llm_retags`; `final` does too).

**A new column on a table**: add it to the `Table` and to `MIGRATIONS` as `ADD COLUMN IF NOT EXISTS`; `create_all`
won't alter an existing table. A new view column goes at the end of the SELECT.
