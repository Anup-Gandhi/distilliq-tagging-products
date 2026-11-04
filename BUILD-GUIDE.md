# Build Guide: Liquor Product Tagger

This guide explains how to build this project from an empty folder. It covers what the product does, every file and
folder, the exact prompts, the database tables and views, the command line, and the order to build things in.
Follow it top to bottom and you will end up with the same product.

---

## 1. What the product does

Liquor stores send item names in their own formats, for example `1792 Full Proof Bourbon 750ml` or
`WTRK 101 BRBN`. Another system has already matched each item to a short list of **candidate catalogue names**.
The candidates are stored in a BigQuery table (`topk_options`) or you can extract it using a .csv file mentioned in the run command.

Human taggers used to look at each item, Google it when unsure, and pick the right candidate. This product
does that job with LLMs instead:

1. **Pass 1, `run`:** an LLM picks the candidate that is exactly the same product, or answers "none" and
   suggests a new catalogue name, or says the item isn't alcoholic.
2. **Pass 2, `retag`:** the *other* LLM provider reviews pass 1's answer and gives the correct result.
3. **Pass 3, `final`:** for rows where passes 1 and 2 disagree, a third LLM call sees both answers and picks
   the best one.
4. **Vote:** a Postgres view takes the final tag by 2-of-3 majority.

Everything is stored in **Postgres**, which is the source of truth. People correct rows directly in Postgres,
and the tagger never overwrites a row that is already there, unless you retry errors.

```
BigQuery topk_options
   │  fetch + clean (bigquery_source.py)
   ▼
SourceRow ──► Pass 1: run ────► llm_tags             (provider A, e.g. Claude)
                 │
                 ▼
              Pass 2: retag ──► claude_retags        (the other provider, e.g. OpenAI)
                 │
                 ▼
        view claude_tag_comparison (tag_1 vs tag_2, tags_match)
                 │  disputed rows only
                 ▼
              Pass 3: final ──► claude_final_tags    (any provider)
                 │
                 ▼
        view claude_tag_votes  (final_tag by 2 of 3 votes)
```

---

## 2. Folder layout, with every part explained

```
distilliq-tagging-products/
├── .env                          # local settings (database URL, Google credentials path)
├── .venv/                        # Python virtual environment (generated; not source)
├── .vscode/settings.json         # makes the VS Code terminal load .env
├── README.md                     # short setup + usage guide for humans
├── claude-tagging-how-it-works.md# long explanation of pass 1, with measured costs
├── BUILD-GUIDE.md                # this file
├── claude_tags.csv               # an OLD 100-row export of pass 1 (reference only)
├── requirements.txt              # pinned Python dependencies
├── final-tags/                   # CSV exports of results, for review
│   ├── new-retagging-10000.csv
│   ├── new-retagging-errors.csv
│   ├── retagging-not-match.csv
│   └── retagging-not-match-final.csv
├── secrets/                      # API keys and the BigQuery service account (never commit)
│   ├── claude.json
│   ├── chatgpt.json
│   └── distilliq-dataset-read.json
└── tagger/                       # the Python package; run with `python -m tagger`
    ├── __init__.py               # empty; marks the package
    ├── __main__.py               # command line: run, retag, final, export
    ├── config.py                 # reads .env and the secrets/ LLM JSON files
    ├── budget.py                 # model prices and the per-run spending cap
    ├── bigquery_source.py        # reads + cleans rows from BigQuery
    ├── claude_tagger.py          # system prompt, record_tag tool, Claude calls, result handling
    ├── openai_tagger.py          # the same thing using OpenAI's Responses API
    ├── final_tagger.py           # pass 3 prompt addition and request building
    └── store.py                  # Postgres tables, views, reads and upserts
```

### 2.1 Root files

| Path | What it is | Notes for a rebuild |
|---|---|---|
| `.env` | Settings loaded by `python-dotenv` | Needs `DATABASE_URL` and `GOOGLE_APPLICATION_CREDENTIALS`. Optional: `BIGQUERY_TABLE`, `TAG_WORKERS`. The current file also holds old variables (`SMTP_*`, `FRONTEND_URL`, `LLM_API_KEY`, `LLM_PROVIDER`, `LLM_MODEL`, `CLAUDE_CONCURRENCY`) that the code no longer reads. Don't rebuild those. |
| `.venv/` | Virtual environment | Create it with `python -m venv .venv`. Don't copy it. |
| `.vscode/settings.json` | `{"python.terminal.useEnvFile": true}` | Optional convenience. |
| `README.md` | Human quick-start | Keep it in sync with the CLI. |
| `claude_tags.csv` | An early 100-row export of the old `claude_tags` table | **Not the source of truth.** It uses the old `Others: <name>` format. Always read from Postgres. |
| `requirements.txt` | Pinned dependencies | See section 3. |

### 2.2 `final-tags/`: exported results

These are review files, produced by `export` or by SQL queries on the views. Nothing reads them back.

| File | Columns | Contents |
|---|---|---|
| `new-retagging-10000.csv` | `base_value, tag, model` | ~10,000 retagged items and their tags |
| `new-retagging-errors.csv` | the full `claude_retags` columns | the few retags that failed |
| `retagging-not-match.csv` | `claude_tag_comparison` columns up to `tag_2_model` | ~1,900 rows where pass 1 and pass 2 disagreed |
| `retagging-not-match-final.csv` | the same, plus `is_alcoholic_1/2/any` | ~900 disagreeing rows used as pass 3 input |

### 2.3 `secrets/`: credentials

Each LLM file is a small JSON file, chosen on the command line by name (`--llm claude` → `secrets/claude.json`):

```json
{"provider": "anthropic", "model": "claude-haiku-5-5", "api_key": "sk-ant-..."}
{"provider": "openai",    "model": "gpt-4.1-mini",     "api_key": "sk-..."}
```

Optional keys: `input_price`, `output_price` (USD per 1M tokens) and `search_price` (USD per search). Use them
for a model that isn't in `budget.py`, or to override its price.

| File | Used for |
|---|---|
| `claude.json` | the Anthropic key (currently `claude-haiku-5-5`) |
| `chatgpt.json` | the OpenAI key (currently `gpt-4.1-mini`) |
| `distilliq-dataset-read.json` | Google service-account key for BigQuery. It has no `api_key`, so the LLM file finder skips it. `GOOGLE_APPLICATION_CREDENTIALS` points here. |
| `final-tagger.json` | **Not present yet.** It is the default `--llm` for pass 3. Create it in the same format, or pass `--llm claude`. |

---

## 3. Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt`:

```
anthropic==2.1.0
google-cloud-bigquery==3.42.1
sqlalchemy==2.0.51
psycopg[binary]==3.3.4
python-dotenv==1.0.1
openai==3.40.0
```

`.env`:

```
DATABASE_URL=postgresql+psycopg://user:password@host:5432/dbname
GOOGLE_APPLICATION_CREDENTIALS=secrets/distilliq-dataset-read.json
# optional
BIGQUERY_TABLE=internal-apps-465610.dev_proximo_cantag.topk_options
TAG_WORKERS=4
```

The tables and views are created automatically the first time the tagger connects to Postgres.

---

## 4. Core data types

These are used by every module. Build them first.

```python
MISC_OPTION = "Misc - Not a Liquor"     # the "not alcoholic" tag

@dataclass(frozen=True)
class SourceRow:                         # one record to tag
    source_row_id: str
    base_value: str                      # the store's item name
    options: list[str]                   # candidate catalogue names
    insert_dttm: datetime | None         # BigQuery insert time (used as the watermark)

@dataclass
class TagResult:                         # one LLM decision
    tag: str | None                      # the final answer; None on error
    selected_option: str | None = None   # the chosen candidate, if one was chosen
    is_alcoholic: bool | None = None
    suggested_name: str | None = None
    confidence: str | None = None        # "high" | "medium" | "low"
    reason: str | None = None
    judgements: list[dict] = []          # one per candidate: {"option", "matches", "reason"}
    sources: list[dict] = []             # up to 5 web pages: {"title", "url"}
    error: str | None = None
```

---

## 5. Module by module

### 5.1 `config.py`: settings and LLM files

- Load `.env` from the project root with `load_dotenv`.
- `Settings`: `database_url`, `google_credentials` (a relative path is resolved from the project root),
  `bigquery_table` (with the default shown above), `tag_workers` (default 4).
- If credentials are set, write the absolute path back to `os.environ["GOOGLE_APPLICATION_CREDENTIALS"]`
  so the BigQuery client finds it.
- `LLMConfig` (frozen dataclass): `provider`, `model`, `api_key`, `input_price`, `output_price`, `search_price`.
- `DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "openai": "gpt-5.5"}`.
- `load_llm_config(name)`:
  1. `name` is either a path or a name in `secrets/` (add `.json` if it's missing). If the file isn't found,
     exit and list the available files.
  2. Exit with a clear message if the JSON is invalid or has no `api_key`.
  3. Work out which provider the key belongs to: a key starting with `sk-ant-` is Anthropic, anything else is
     OpenAI. `provider` defaults to that. Exit if the stated provider doesn't match the key. This catches a
     wrong key before every row fails with a 401.
  4. `model` defaults to `DEFAULT_MODELS[provider]`.
  5. Prices come from the JSON if it sets them, else from `budget.PRICES[model]`. Exit if neither has them.
  6. `search_price` comes from the JSON, else $0.025 for OpenAI `gpt-4*`/`gpt-3*` models, else $0.01.
- `provider_of_model(model)`: `"anthropic"` if the name starts with `claude`, else `"openai"`.
- `find_llm_config(provider)`: scan `secrets/*.json`, skip files without `api_key`, and return the single file
  for that provider. Exit if there are none, or several (then the user picks one with `--llm`).

### 5.2 `budget.py`: prices and the spending cap

```python
PRICES = {  # USD per 1M tokens: (input, output)
    "claude-fable-5-1": (10.00, 50.00), "claude-fable-5": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),   "claude-opus-5": (5.00, 25.00), "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00), "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),   "claude-haiku-4-5": (1.00, 5.00),
    "gpt-5.5": (5.00, 30.00), "gpt-4.1": (2.00, 8.00), "gpt-4.1-mini": (0.40, 1.60),
}
SEARCH_PRICE = 0.01                 # $10 per 1,000 searches
NON_REASONING_SEARCH_PRICE = 0.025  # OpenAI gpt-4.x: $25 per 1,000
CACHE_WRITE, CACHE_READ = 1.25, 0.1 # multiples of the input price
```

`Budget(limit)`: `check()` raises `BudgetExceeded` once `spent >= limit`. Call it before every request.
`charge(cost)` adds to `spent` under a `threading.Lock`. Requests already in flight still finish, so a run can
go a little over the limit.

### 5.3 `bigquery_source.py`: reading and cleaning rows

The query (the watermark parameter `@wm` can be NULL):

```sql
SELECT CAST(sourceRowId AS STRING) AS source_row_id,
       baseValue AS base_value,
       options AS options,
       TIMESTAMP(insert_dttm) AS insert_dttm
FROM `<BIGQUERY_TABLE>`
WHERE @wm IS NULL OR TIMESTAMP(insert_dttm) >= @wm
ORDER BY insert_dttm
```

Read the results with `page_size=5000` and count `fetched` and `invalid` in a `FetchStats` object.

Cleaning rules (`clean_row`):

1. `parse_options(raw)`: split on `|`, trim each part, drop empty parts, and remove duplicates while keeping
   the first occurrence.
2. If every option is `None` (case-insensitive), or there are no options, set the options to `[MISC_OPTION]`.
   This is a "Misc row": the LLM gets no candidates and names the product itself.
3. **Invalid** (skipped and counted): no `source_row_id`, a blank `base_value`, or fewer than 2 options when
   the row isn't a Misc row.

### 5.4 `claude_tagger.py`: the heart of the product

#### The system prompt (used by passes 1 and 2, for every provider)

Copy it exactly. The line breaks inside paragraphs in the source are only for code layout, and each paragraph
is one line of text.

```
You tag retail product records for a liquor catalogue.

Each record is an item name as it appears in a store's data, often abbreviated and with sizes and pack counts, plus a numbered list of candidate catalogue names, or no candidates at all. The tag links the store's item to one catalogue product, so a wrong match is worse than no match: when no candidate is exactly the same product, answer 0 and suggest a name instead.

What counts as the same product
A candidate matches only when it names exactly the same product as the item. These differences are ignored:
- sizes (such as 750ml or 1.75L) and pack counts (such as 6pk or 12 x 355ml);
- letter case, punctuation and spacing ("W.L. Weller" is "W L Weller");
- standard abbreviations and their full forms ("Yr" is "Year").
Everything else must be the same: the brand, product line, flavour/expression, age statement, proof and every other word of the name. A candidate does not match when it:
- names a different brand, even for a similar product;
- has a word the item doesn't have, such as a parent company or distillery name, or lacks a word the item has;
- names a different expression of the same brand, or only the brand.

For example, for the item "1792 Full Proof Bourbon 750ml":
- "1792 Full Proof Bourbon 1.75L" matches (only the size differs);
- "Buffalo Trace 1792 Full Proof" does not (it adds Buffalo Trace and lacks Bourbon);
- "W L Weller Full Proof" does not (different brand).
With only the last two offered, the answer is 0 with the suggested name "1792 Full Proof Bourbon".

If several candidates match equally well, pick the lowest number. If you can't tell whether a candidate matches, answer 0.

Web search
Matching is a comparison of names, so decide it from the text. Search only when you can't tell what the item is (a cryptic abbreviation or a product you don't recognise), or when you need the product's real name to suggest one. Don't search to confirm a decision you can already make.

Answer
When you have decided, call record_tag once:
- option_number: the number of the matching candidate, or 0 if none matches (always 0 when there are no candidates).
- option_reasons: one entry per candidate, in order, each a short phrase saying why it does or doesn't match, such as "only the size differs" or "different brand (W L Weller)". Empty when there are no candidates.
- is_alcoholic: whether the item is an alcoholic drink.
- suggested_name: when option_number is 0 and the item is alcoholic, a new catalogue name for the product, written so it would look like one more candidate in the list: the same style as the candidates, in Title Case with abbreviations written out and the usual order of brand, age, product line, expression and type, such as "Whistlepig 6 Year Piggyback Rye Whiskey". Leave out sizes, pack counts, proof or ABV (unless part of the product's name), store codes and stray punctuation. With no candidates, use that same catalogue style. Otherwise "".
- confidence: high when the item text clearly identifies the product and the decision is clear-cut, medium when you had to interpret the item text, low when it is too vague to be sure.
- reason: one sentence explaining the final decision.
```

#### The user message

```python
def candidates(row):   # options shown to the model
    return [o for o in row.options if o.lower() != "none" and o != MISC_OPTION]

def user_message(item, options, note=None):
    listed = "\n".join(f"{n}. {o}" for n, o in enumerate(options, 1)) if options \
             else "none (name the product yourself)"
    message = f"Item: {item}\n\nCandidates:\n{listed}"
    return f"{message}\n\n{note}" if note else message
```

`item` is `base_value` with runs of whitespace collapsed to one space and the ends trimmed.

Example:

```
Item: 1792 Full Proof Bourbon 750ml

Candidates:
1. 1792 12 Yr Bourbon
2. W L Weller Full Proof Bourbon
3. Buffalo Trace 1792 Full Proof
```

#### The `record_tag` tool (strict schema)

```python
RECORD_TAG_TOOL = {
    "name": "record_tag",
    "description": "Record which candidate identifies the item. Call exactly once, after any searching.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "option_number": {"type": "integer"},
            "option_reasons": {"type": "array", "items": {
                "type": "object",
                "properties": {"option_number": {"type": "integer"}, "reason": {"type": "string"}},
                "required": ["option_number", "reason"], "additionalProperties": False}},
            "is_alcoholic": {"type": "boolean"},
            "suggested_name": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string"},
        },
        "required": ["option_number", "option_reasons", "is_alcoholic", "suggested_name", "confidence", "reason"],
        "additionalProperties": False,
    },
}
```

#### Web search tool

- Most Claude models: `{"type": "web_search_20260209", "name": "web_search", "max_uses": 2}`
- Haiku (if `"haiku"` is in the model name): `{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}`.
  Haiku doesn't support the newer version or the server-side fallback.
- `--no-web-search` sends only `record_tag`.

#### The request (`ClaudeTagger`)

- Client: `anthropic.Anthropic(api_key=..., timeout=180.0, max_retries=4)`.
- Call `client.beta.messages.create(model, max_tokens=16000, system=SYSTEM_PROMPT, messages, tools,
  output_config={"effort": effort})`.
- Except on Haiku, also pass `betas=["server-side-fallback-2026-07-01"], fallbacks="default"`, so that a
  refusal is retried on a fallback model.
- **Effort defaults to `medium`.** Keep it unless the user asks for another level.
- **No prompt caching.** Don't add `cache_control`. The owner chose this on purpose.
- **`pause_turn` loop:** a long server-side search can pause. If `stop_reason == "pause_turn"`, append the
  assistant content to the messages and send again unchanged, at most 3 more times. Check the budget before
  each send and charge it after.
- If `stop_reason == "refusal"`, the result is an error: "Claude declined to tag this record".
- Find the `tool_use` block named `record_tag` and use its `input`. If there isn't one, the result is the
  error "No decision recorded (stop_reason=...)".
- Catch `APIStatusError` (→ `"API error {code}: {message}"`) and `APIConnectionError`. Every failure becomes
  a `TagResult` with `error` set. A failure never stops the run.

#### Cost of one response

```
tokens = (input_tokens * in_price
        + cache_creation_input_tokens * in_price * 1.25
        + cache_read_input_tokens     * in_price * 0.1
        + output_tokens * out_price) / 1_000_000
cost   = tokens + web_search_requests * search_price
```

#### Turning the decision into a `TagResult` (`_result`, shared by every provider)

1. `chosen = option_number`. Build `judgements` with one entry per candidate:
   `{"option", "matches": n == chosen, "reason": the reason for n}`.
2. If `chosen` isn't between 0 and the number of candidates, the result is the error
   "Claude chose option N, which doesn't exist".
3. If `chosen > 0`: `tag = selected_option = options[chosen-1]`.
4. If `chosen == 0`:
   - not alcoholic → `tag = MISC_OPTION`
   - alcoholic with a suggested name → `tag = suggested_name`
   - alcoholic with no name → `tag = None`, error "No option fit and no name was suggested"
5. `sources`: from the `web_search_tool_result` blocks, unique by URL, `{"title" (or the URL), "url"}`, at
   most 5. Skip error results, which are a single object rather than a list.

### 5.5 `openai_tagger.py`: the same tagger on OpenAI

It reuses `SYSTEM_PROMPT`, `RECORD_TAG_TOOL`, `candidates`, `user_message`, `_result`, `TagResult` and
`TaggingError` from `claude_tagger.py`. Only the API call changes:

- `openai.OpenAI(api_key, timeout=180.0, max_retries=4)` and
  `client.responses.create(model, max_output_tokens=16000, instructions=system_prompt, input=message,
  tools=[{"type": "web_search"}, RECORD_TAG_FUNCTION], include=["web_search_call.action.sources"])`.
- `RECORD_TAG_FUNCTION = {"type": "function", "name": ..., "description": ..., "strict": True,
  "parameters": RECORD_TAG_TOOL["input_schema"]}`.
- Pass `reasoning={"effort": effort}`, except for models starting with `gpt-4` or `gpt-3`, which reject it.
- Find the output item with `type == "function_call"` and `name == "record_tag"`, then `json.loads(arguments)`.
- Cost: `(input - cached) * in + cached * in * 0.1 + output * out` per 1M tokens, plus the number of
  `web_search_call` items × `search_price`. Output tokens include reasoning tokens.
- Sources: from `web_search_call` items whose `action.type == "search"`, from `action.sources`, at most 5.

### 5.6 `final_tagger.py`: pass 3

**System prompt** = the full `SYSTEM_PROMPT` above, plus:

```

Disputed records
Two earlier taggers disagreed on this record. Their answers are listed after the candidates, and each earlier answer that names a product is also one of the candidates. Decide which is right by the rules above, judging every candidate on its own merits: an answer isn't more likely to be right because a tagger gave it. Pick one of the earlier answers when it names exactly the same product as the item, or another candidate if that one does instead. Answer 0 with a suggested name only when no candidate is exactly the same product, and set is_alcoholic to false when the item isn't an alcoholic drink.
```

**`is_misc(option)`**: true when the option, with `<` and `>` removed and trimmed, equals `MISC_OPTION`
ignoring case. This also catches the `<Misc - not a Liquor>` spelling.

**`disputed_request(row)`**, where `row` is a `claude_tag_comparison` row:

1. Candidates = `row.options + [tag_1, tag_2]`, trimmed, without `none` or Misc, and with duplicates removed
   ignoring case (keep the first spelling).
2. Describe each earlier answer:
   - empty → `no answer`
   - Misc → `not an alcoholic drink (Misc - Not a Liquor)`
   - found in the candidates → `candidate N, <name>`
   - otherwise → the tag text itself
3. Note, appended to the user message:
   ```
   Earlier answers (they disagree):
   - Tagger 1: candidate 3, 1792 Full Proof Bourbon
   - Tagger 2: candidate 4, Buffalo Trace 1792 Full Proof
   ```
4. Return `(SourceRow(id, base_value, candidates, None), note)`.

### 5.7 `store.py`: Postgres

Use SQLAlchemy Core with the `psycopg` driver. On connect, run `metadata.create_all`, then the `MIGRATIONS`
(`ADD COLUMN IF NOT EXISTS` statements), because `create_all` doesn't change a table made by an earlier version.
The old `claude_tags` table is left as it is: pass 1 now writes to `llm_tags`. Then create or replace both views. Every write is an **upsert** on `source_row_id`
(`INSERT ... ON CONFLICT DO UPDATE` of every column except the key, with `tagged_at = now()`).

#### Table `llm_tags`: pass 1

| Column | Type | Meaning |
|---|---|---|
| `source_row_id` | String, **PK** | BigQuery `sourceRowId` |
| `base_value` | Text, not null | the item name |
| `options` | `ARRAY(String)`, not null | the candidates. **A suggested name is appended here**, so it reads like one more option. |
| `tag` | Text | an option, `Misc - Not a Liquor`, or the suggested name. NULL on error. |
| `selected_option` | Text | the chosen candidate, or NULL |
| `is_alcoholic` | Boolean | |
| `suggested_name` | Text | |
| `confidence` | String(10) | high / medium / low |
| `reason` | Text | one-sentence justification |
| `judgements` | JSONB, default `[]` | a verdict per option |
| `sources` | JSONB, default `[]` | web pages used |
| `error` | Text | why tagging failed |
| `model` | String(64), not null | the model that tagged it |
| `source_insert_dttm` | timestamptz | BigQuery insert time (the watermark) |
| `tagged_at` | timestamptz, default now() | |

#### Table `claude_retags`: pass 2

`source_row_id` (PK), `base_value`, `first_tag` (not null; pass 1's tag at the time), `first_model`,
`tag`, `is_alcoholic`, `suggested_name`, `confidence`, `reason`, `sources`, `error`, `model`, `tagged_at`.
**A name pass 2 makes up itself is saved as `Others: <name>`.**

#### Table `claude_final_tags`: pass 3

`source_row_id` (PK), `base_value`, `options` (exactly as shown to the model), `tag_1`, `tag_2`,
`tag_1_model`, `tag_2_model`, `tag`, `selected_option`, `is_alcoholic`, `suggested_name`, `confidence`,
`reason`, `judgements`, `sources`, `error`, `model`, `tagged_at`.

#### View `claude_tag_comparison`

```sql
CREATE OR REPLACE VIEW claude_tag_comparison AS
WITH tags AS (
    SELECT
        t.source_row_id, t.base_value, t.options,
        regexp_replace(regexp_replace(t.tag, '^\s*others:\s*', '', 'i'),
                       '^\s*<\s*(misc - not a liquor)\s*>\s*$', '\1', 'i') AS tag_1,
        regexp_replace(regexp_replace(r.tag, '^\s*others:\s*', '', 'i'),
                       '^\s*<\s*(misc - not a liquor)\s*>\s*$', '\1', 'i') AS tag_2,
        t.model AS tag_1_model, r.model AS tag_2_model,
        t.is_alcoholic AS is_alcoholic_1, r.is_alcoholic AS is_alcoholic_2,
        r.source_row_id IS NOT NULL AS retagged,
        f.tag AS tag_3, f.model AS tag_3_model, f.is_alcoholic AS is_alcoholic_3
    FROM llm_tags t
    LEFT JOIN claude_retags r USING (source_row_id)
    LEFT JOIN claude_final_tags f USING (source_row_id)
)
SELECT
    source_row_id, base_value, options, tag_1, tag_2,
    CASE WHEN retagged THEN COALESCE(lower(trim(tag_1)) = lower(trim(tag_2)), false) END AS tags_match,
    tag_1_model, tag_2_model,
    -- new columns go last: CREATE OR REPLACE VIEW can only add columns at the end
    is_alcoholic_1, is_alcoholic_2,
    is_alcoholic_1 OR is_alcoholic_2 AS is_alcoholic_any,
    tag_3, tag_3_model, is_alcoholic_3
FROM tags
```

**Disputed rows** (the input to pass 3):
`tag_2 IS NOT NULL AND tags_match IS NOT TRUE AND is_alcoholic_any IS TRUE`.

#### View `claude_tag_votes`: the final answer

```sql
CREATE OR REPLACE VIEW claude_tag_votes AS
WITH v AS (
    SELECT c.*,
           lower(trim(tag_3)) = lower(trim(tag_1)) AS tag_3_agrees_1,
           lower(trim(tag_3)) = lower(trim(tag_2)) AS tag_3_agrees_2
    FROM claude_tag_comparison c
)
SELECT
    source_row_id, base_value, options, tag_1, tag_2, tag_3,
    CASE
        WHEN tags_match THEN tag_1
        WHEN tag_2 IS NULL THEN NULL
        WHEN NOT is_alcoholic_any THEN 'Misc - Not a Liquor'
        WHEN tag_3_agrees_1 OR tag_3_agrees_2 THEN tag_3
    END AS final_tag,
    CASE
        WHEN tags_match THEN 'agreed'
        WHEN tag_2 IS NULL THEN 'needs retag'
        WHEN NOT is_alcoholic_any THEN 'both not alcoholic'
        WHEN tag_3 IS NULL THEN 'needs final'
        WHEN tag_3_agrees_1 THEN 'final agrees with 1'
        WHEN tag_3_agrees_2 THEN 'final agrees with 2'
        ELSE 'no majority'
    END AS decision,
    tag_1_model, tag_2_model, tag_3_model,
    CASE WHEN is_alcoholic_1 = is_alcoholic_2 THEN is_alcoholic_1 ELSE is_alcoholic_3 END AS is_alcoholic,
    is_alcoholic_1, is_alcoholic_2, is_alcoholic_3
FROM v
```

#### Store methods

| Method | Does |
|---|---|
| `watermark()` | `MAX(llm_tags.source_insert_dttm)` |
| `tagged_ids(include_errors)` | ids in `llm_tags` (optionally only the ones without an error) |
| `save(row, result, model)` | upserts pass 1. Appends the suggested name to `options` when the tag is that name. |
| `error_rows()` | pass 1 rows with an error, oldest first, **as stored**, including human edits |
| `first_pass_rows()` | pass 1 rows with a tag, oldest first |
| `first_pass_models()` | the distinct models that tagged pass 1 rows |
| `retagged_ids(include_errors)` / `save_retag(first, result, model)` | pass 2 |
| `disputed_rows()` | `SELECT * FROM claude_tag_comparison WHERE <disputed> ORDER BY source_row_id` |
| `final_tagged_ids(include_errors)` / `save_final(disputed, row, result, model)` | pass 3 |
| `all_rows()` / `all_retags()` | for `export` |

### 5.8 `__main__.py`: the command line

`python -m tagger <command>`. All three tagging commands take `--max-cost USD` (default 15), `--limit N`,
`--retry-errors`, `--workers N` (default `TAG_WORKERS`), `--effort low|medium|high` (default medium),
`--no-web-search` and `--dry-run`. `--dry-run` prints JSON lines, writes nothing and defaults to
`--limit 5`. `run` without `--dry-run` needs Postgres.

#### `run --llm NAME [--full]`: pass 1

1. Load the LLM config. Unless this is a dry run, open the Store.
2. Watermark: `store.watermark()`, or none with `--full`.
3. `skip` = every id already in `llm_tags`, **including errors**. Rows already in Postgres are never read
   again from BigQuery, so human edits there are kept.
4. Pending rows = BigQuery rows newer than the watermark and not in `skip`. With `--retry-errors`, the
   stored error rows go first, rebuilt from Postgres.
5. Tag them in parallel and save each result as it arrives.
6. Print a summary: fetched, invalid, already tagged, tagged, failed, and money spent of the limit.

#### `retag [--llm NAME ...]`: pass 2

1. Each first-pass row is retagged by the **other provider**: a row tagged by a `claude*` model goes to
   OpenAI, and any other row goes to Anthropic. Use `find_llm_config(provider)`, or the file given with
   `--llm` for that provider (`--llm` can be repeated).
2. Use `tagger/retag_system_prompt.md` and the `record_review` tool. The message is `<question>` (pass 1's
   stored options, minus any name pass 1 made up) and `<model_1_output>` (pass 1's answer). For old rows, the
   made-up name is the tag with `Others: ` removed.
3. `_from_review` turns the review into `record_tag`'s fields: `correct_answer` becomes the tag, and IGNORED, or
   "None of the above" with no name on an alcoholic item, becomes a skip.
4. Skip rows already in `claude_retags`. Rows that failed are skipped too, unless `--retry-errors` is set.
5. Save with `save_retag`.

#### `final [--llm NAME]`: pass 3

1. The default `--llm` is `final-tagger`. Use the pass 3 system prompt.
2. Load `disputed_rows()`, skipping the ones done already (and the failed ones, unless `--retry-errors`).
3. Tag each row with `tagger.tag(row, note=...)` from `disputed_request` and save it with `save_final`.

#### `export PATH [--retags]`

Writes `llm_tags`, or `claude_retags` with `--retags`, to a CSV file. `options` is joined with ` | `, and
`judgements` and `sources` are written as JSON.

#### The parallel loop (`_tag_rows`)

```python
with ThreadPoolExecutor(max_workers=workers) as pool:
    while not over_budget and (window := list(islice(rows, workers * 4))):
        futures = {pool.submit(tag, row): row for row in window}
        for future in as_completed(futures):
            try: result = future.result()
            except BudgetExceeded: over_budget, result = True, None
            yield futures[future], result
```

Rows are submitted in windows of `workers × 4`, so a big table isn't held in memory all at once. A `None`
result means the row was skipped because the budget ran out. It is **not saved**, so the next run picks it
up. After a budget stop, tell the user to run again, adding `--full` for `run`.

`_tagger(llm, budget, **kw)` returns an `OpenAITagger` when the provider is `openai`, else a `ClaudeTagger`.

---

## 6. Build order

1. `requirements.txt`, `.env` and `secrets/`. Check that you can connect to BigQuery and Postgres.
2. `config.py`, then `budget.py`.
3. `bigquery_source.py`. Test that `parse_options` and `clean_row` behave as in section 5.3.
4. `claude_tagger.py`: the prompt, tool, request and `_result`. Try it on one hard-coded `SourceRow`.
5. `__main__.py` with only `run --dry-run`. Check the JSON for 5 rows.
6. `store.py` with `llm_tags`, then `run` without `--dry-run`, then `export`.
7. `openai_tagger.py`, then `retag` and `claude_retags`, then the `claude_tag_comparison` view.
8. `final_tagger.py`, then `final`, `claude_final_tags` and the `claude_tag_votes` view.
9. A README.

---

## 7. Checks before calling it done

- [ ] `1792 Full Proof Bourbon 750ml` with candidates `Buffalo Trace 1792 Full Proof` and
      `W L Weller Full Proof` → option 0, suggested name `1792 Full Proof Bourbon`, which is appended to `options`.
- [ ] A candidate that differs only in size → it is chosen.
- [ ] A row whose options are all `None` → sent with no candidates. The model names the product, or the
      tag is `Misc - Not a Liquor`.
- [ ] A non-alcoholic item (e.g. a soda) → `tag = "Misc - Not a Liquor"`.
- [ ] A bad API key → every row is saved with `error` set, and the run finishes.
- [ ] `--max-cost 0.01` → the run stops early, and unreached rows aren't saved.
- [ ] Running `run` twice → the second run tags nothing new and costs nothing.
- [ ] Edit a row in Postgres, then `run` → the edit is kept.
- [ ] `retag` uses the other provider. `claude_tag_comparison.tags_match` ignores case, spaces and `Others: `.
- [ ] `final` only touches disputed alcoholic rows. `claude_tag_votes.decision` gives a value for every row.

---

## 8. Rules the owner has set

- **Postgres is the source of truth.** Never read tags from `claude_tags.csv` or the CSVs in `final-tags/`.
- **No prompt caching** (`cache_control` stays off).
- **Effort is `medium`** unless the owner asks for another level.
- Never commit anything from `secrets/` or `.env`.

---

## 9. Measured cost (pass 1, Claude Opus 5.5, from `claude-tagging-how-it-works.md`)

| Mode | Input tokens | Output tokens | Cost / record |
|---|---|---|---|
| `--no-web-search` | ~910 | ~220 | ~$0.008 |
| web search on, no search made | ~6,500 | ~235 | ~$0.031 |
| web search on, one search | ~21,200 | ~520 | ~$0.105 |

Turning web search on adds ~5,600 tokens of tool instructions to every request. Each search adds ~15,000
tokens of result pages. Cheaper models (Haiku 5.5, gpt-4.1-mini) cost far less per token. Run
`--dry-run --limit 20` and check the reported spend before a big run.

---

## 10. Adding another provider (e.g. Gemini). Optional, not built yet

Follow the pattern of `openai_tagger.py`:

1. Write a new `<provider>_tagger.py` class with the same constructor, `tag(row, note=None) -> TagResult`,
   `_request` and `_cost`. Reuse `SYSTEM_PROMPT`, the `record_tag` schema (as a function declaration),
   `candidates`, `user_message` and `_result` unchanged.
2. Use the provider's own web search tool. For Gemini this is Grounding with Google Search, which is the
   closest match to Google's AI Overviews. Collect its grounding sources into `sources`.
3. In `config.py`: add the provider to `DEFAULT_MODELS`, detect its keys (Gemini keys start with `AIza`), and
   update `provider_of_model`.
4. In `budget.py`: add the model's prices and its search price.
5. In `__main__.py`: return the new class from `_tagger`. Decide how `OTHER_PROVIDER` should work, since
   `retag` currently assumes exactly two providers.
