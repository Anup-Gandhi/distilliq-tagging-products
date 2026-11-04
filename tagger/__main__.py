"""Command line: python -m tagger run | retag | final | export."""
import argparse
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from itertools import islice

from .bigquery_source import MISC_OPTION, FetchStats, SourceRow, fetch_csv, fetch_rows, read_csv
from .budget import Budget, BudgetExceeded
from .claude_tagger import NONE_OPTION, RECORD_REVIEW_TOOL, RETAG_SYSTEM_PROMPT, ClaudeTagger, is_misc
from .config import (DEFAULT_MODELS, EFFORTS, find_llm_configs, load_llm_config, load_settings, provider_of_model)
from .final_tagger import FINAL_SYSTEM_PROMPT, disputed_request

# The provider pass 2 prefers, in order, when it isn't the one that tagged pass 1.
RETAG_PREFERENCE = ("gemini", "anthropic", "openai")


def _tagger(llm, budget, **kw):
    if llm.provider == "openai":
        from .openai_tagger import OpenAITagger
        return OpenAITagger(llm, budget, **kw)
    if llm.provider == "gemini":
        from .gemini_tagger import GeminiTagger
        return GeminiTagger(llm, budget, **kw)
    return ClaudeTagger(llm, budget, **kw)


def _tag_rows(rows, tag, workers):
    """Tag rows in parallel windows of workers x 4. Yields (row, result); result is None after a budget stop."""
    rows = iter(rows)
    over_budget = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while not over_budget and (window := list(islice(rows, workers * 4))):
            futures = {pool.submit(tag, row): row for row in window}
            for future in as_completed(futures):
                try:
                    result = future.result()
                except BudgetExceeded:
                    over_budget, result = True, None
                yield futures[future], result


def _store(settings):
    from .store import Store
    if not settings.database_url:
        sys.exit("DATABASE_URL is not set in .env")
    from sqlalchemy.exc import DBAPIError
    try:
        return Store(settings.database_url)
    except DBAPIError as e:  # can't connect, or not allowed to create the missing database
        message = str(e.orig).splitlines()[0] if e.orig else str(e)
        sys.exit(f"Can't connect to Postgres (DATABASE_URL in .env): {message}")


def _print_dry(row: SourceRow, result, model: str) -> None:
    print(json.dumps({"source_row_id": row.source_row_id, "base_value": row.base_value, "options": row.options,
                      "model": model, **asdict(result)}, default=str, ensure_ascii=False), flush=True)


def _run_loop(rows, tag, save, args, budget, model):
    """Tag rows, save each result (or print it on a dry run), and return (tagged, skipped, failed, budget_stopped)."""
    tagged = skipped = failed = 0
    stopped = False
    for row, result in _tag_rows(rows, tag, args.workers):
        if result is None:  # skipped because the budget ran out: not saved, so the next run picks it up
            stopped = True
            continue
        if args.dry_run:
            _print_dry(row, result, model(row))
        else:
            save(row, result)
        if result.error:
            failed += 1
            print(f"  failed {row.source_row_id}: {result.error}", file=sys.stderr, flush=True)
        elif result.skip_reason:
            skipped += 1
            print(f"  skipped {row.source_row_id}: {result.skip_reason}", file=sys.stderr, flush=True)
        else:
            tagged += 1
        done = tagged + skipped + failed
        if done % 50 == 0:
            print(f"  ... {done} done, ${budget.spent:.2f} spent", file=sys.stderr, flush=True)
    return tagged, skipped, failed, stopped


def _summary(budget, stopped, again: str, **counts) -> None:
    for name, value in counts.items():
        print(f"{name.replace('_', ' '):>15}: {value}")
    print(f"{'spent':>15}: ${budget.spent:.4f} of ${budget.limit:.2f}")
    if stopped:
        print(f"Stopped at the spending limit. Rows not reached weren't saved; run again to continue ({again}).")


def _limited(rows, limit):
    return islice(rows, limit) if limit else rows


# run: pass 1

def cmd_run(args, settings):
    llm = load_llm_config(args.llm)
    budget = Budget(args.max_cost)
    tagger = _tagger(llm, budget, effort=args.effort or llm.effort, web_search=not args.no_web_search)
    store = None if args.dry_run else _store(settings)

    skip = store.tagged_ids(include_errors=True) if store else set()
    stats = FetchStats()
    if args.csv:
        if store:
            print(f"Uploaded {store.save_csv(args.csv, read_csv(args.csv))} rows to csv_records", file=sys.stderr)
        source = fetch_csv(args.csv, stats)
    else:
        watermark = None if (args.full or not store) else store.watermark()
        source = fetch_rows(settings.bigquery_table, watermark, stats)

    already = 0

    def pending():
        nonlocal already
        if args.retry_errors and store:
            for r in store.error_rows():
                yield SourceRow(r["source_row_id"], r["base_value"], list(r["options"]), r["source_insert_dttm"])
        for row in source:
            if row.source_row_id in skip:
                already += 1
                continue
            yield row

    tagged, skipped, failed, stopped = _run_loop(
        _limited(pending(), args.limit), tagger.tag,
        lambda row, result: store.save(row, result, tagger.model), args, budget, lambda row: tagger.model)
    _summary(budget, stopped, "add --full" if not args.csv else "same command",
             fetched=stats.fetched, invalid=stats.invalid, already_tagged=already, tagged=tagged,
             skipped=skipped, failed=failed)


# retag: pass 2

def _retag_configs(names):
    """One LLM config per provider, from --llm or from secrets/."""
    if names:
        configs = [load_llm_config(n) for n in names]
    else:
        configs = []
        for provider in RETAG_PREFERENCE:
            found = find_llm_configs(provider)
            if len(found) > 1:
                sys.exit(f"Several {provider} LLM files in secrets/ ({', '.join(p.stem for p in found)}). "
                         "Pick one with --llm.")
            if found:
                configs.append(load_llm_config(str(found[0])))
    by_provider = {}
    for llm in configs:
        if llm.provider in by_provider:
            sys.exit(f"--llm names two {llm.provider} files; give one per provider.")
        by_provider[llm.provider] = llm
    return by_provider


def _other_provider(model: str, available) -> str | None:
    first = provider_of_model(model or "")
    return next((p for p in RETAG_PREFERENCE if p != first and p in available), None)


def _retag_request(first: dict) -> tuple[SourceRow, str]:
    """The record to review and pass 1's output, as retag_system_prompt.md expects them."""
    options = list(first["options"] or [])
    tag = (first["tag"] or "").strip()
    name = tag[len("Others:"):].strip() if tag.lower().startswith("others:") else tag
    answer, brand_label_name = first["selected_option"] or tag, ""
    if is_misc(name):
        answer = MISC_OPTION
    elif not first["selected_option"] and (name != tag or name == first["suggested_name"]):
        # Pass 1 made up this name. save() stored it among the options, but it isn't a catalogue name.
        answer, brand_label_name = NONE_OPTION, name
        options = [o for o in options if o.lower() != name.lower()]
    output = {"answer": answer, "is_alcoholic": first["is_alcoholic"], "brand_label_name": brand_label_name,
              "skip_reason": first["skip_reason"] or "",
              "name_reasons": [{"name": j["option"], "reason": j["reason"]} for j in first["judgements"] or []],
              "confidence": first["confidence"], "reason": first["reason"]}
    note = f"<model_1_output>\n{json.dumps(output, ensure_ascii=False)}\n</model_1_output>"
    return SourceRow(first["source_row_id"], first["base_value"], options, first["source_insert_dttm"]), note


def cmd_retag(args, settings):
    store = _store(settings)
    configs = _retag_configs(args.llm)
    budget = Budget(args.max_cost)

    models = store.first_pass_models()
    route = {}
    for model in models:
        provider = _other_provider(model, configs)
        if provider is None:
            others = [p for p in DEFAULT_MODELS if p != provider_of_model(model or "")]
            sys.exit(f"Rows tagged by {model} need a retagger from another provider ({' or '.join(others)}). "
                     "Add one to secrets/ or pass --llm.")
        route[model] = provider
    taggers = {p: _tagger(configs[p], budget, effort=args.effort or configs[p].effort,
                          web_search=not args.no_web_search, system_prompt=RETAG_SYSTEM_PROMPT,
                          tool=RECORD_REVIEW_TOOL)
               for p in set(route.values())}
    for model, provider in route.items():
        print(f"Pass 1 rows by {model} -> retagged by {taggers[provider].model}", file=sys.stderr)

    done = store.retagged_ids(include_errors=not args.retry_errors)
    firsts = {r["source_row_id"]: r for r in store.first_pass_rows() if r["source_row_id"] not in done}
    already = len(done)

    def tagger_for(row):
        return taggers[route[firsts[row.source_row_id]["model"]]]

    requests = {i: _retag_request(r) for i, r in firsts.items()}
    tagged, skipped, failed, stopped = _run_loop(
        _limited((row for row, _ in requests.values()), args.limit),
        lambda row: tagger_for(row).tag(row, note=requests[row.source_row_id][1]),
        lambda row, result: store.save_retag(firsts[row.source_row_id], result, tagger_for(row).model),
        args, budget, lambda row: tagger_for(row).model)
    _summary(budget, stopped, "same command", first_pass_rows=len(firsts) + already, already_retagged=already,
             tagged=tagged, skipped=skipped, failed=failed)


# final: pass 3

def cmd_final(args, settings):
    store = _store(settings)
    llm = load_llm_config(args.llm)
    budget = Budget(args.max_cost)
    tagger = _tagger(llm, budget, effort=args.effort or llm.effort, web_search=not args.no_web_search,
                     system_prompt=FINAL_SYSTEM_PROMPT)

    done = store.final_tagged_ids(include_errors=not args.retry_errors)
    disputed = {str(r["source_row_id"]): r for r in store.disputed_rows()}
    requests = {i: disputed_request(r) for i, r in disputed.items() if i not in done}

    tagged, skipped, failed, stopped = _run_loop(
        _limited((row for row, _ in requests.values()), args.limit),
        lambda row: tagger.tag(row, note=requests[row.source_row_id][1]),
        lambda row, result: store.save_final(disputed[row.source_row_id], row, result, tagger.model),
        args, budget, lambda row: tagger.model)
    _summary(budget, stopped, "same command", disputed=len(disputed), already_final=len(disputed) - len(requests),
             tagged=tagged, skipped=skipped, failed=failed)


# import

def cmd_import(args, settings):
    store = _store(settings)
    for path in args.paths:
        print(f"Uploaded {store.save_csv(path, read_csv(path))} rows from {path} to csv_records")


# export

def cmd_export(args, settings):
    store = _store(settings)
    rows = store.all_retags() if args.retags else store.all_rows()
    if not rows:
        print("Nothing to export.")
        return
    with open(args.path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            out = dict(row)
            if out.get("options") is not None:
                out["options"] = " | ".join(out["options"])
            for key in ("judgements", "sources"):
                if key in out:
                    out[key] = json.dumps(out[key], ensure_ascii=False)
            writer.writerow(out)
    print(f"Wrote {len(rows)} rows to {args.path}")


def main(argv=None):
    settings = load_settings()
    parser = argparse.ArgumentParser(prog="python -m tagger", description="Tag liquor products with LLMs.")
    commands = parser.add_subparsers(dest="command", required=True)

    def tagging(name, help):
        p = commands.add_parser(name, help=help)
        p.add_argument("--max-cost", type=float, default=15.0, metavar="USD", help="spending cap (default 15)")
        p.add_argument("--limit", type=int, help="tag at most N rows")
        p.add_argument("--retry-errors", action="store_true", help="tag failed rows again")
        p.add_argument("--workers", type=int, default=settings.tag_workers, help="parallel requests")
        p.add_argument("--effort", choices=EFFORTS,
                       help="overrides the LLM file's effort (default: the file's, else medium)")
        p.add_argument("--no-web-search", action="store_true")
        p.add_argument("--dry-run", action="store_true", help="print JSON lines, write nothing (default --limit 5)")
        return p

    p = tagging("run", "pass 1: tag new rows from BigQuery (or a CSV)")
    p.add_argument("--llm", required=True, help="LLM file in secrets/, e.g. claude, chatgpt or gemini")
    p.add_argument("--full", action="store_true", help="ignore the watermark and read the whole table")
    p.add_argument("--csv", metavar="PATH", help="read rows from a CSV export of topk_options instead of BigQuery")
    p.set_defaults(func=cmd_run)

    p = tagging("retag", "pass 2: tag pass 1 rows again with another provider")
    p.add_argument("--llm", action="append", help="LLM file to retag with; repeat for several providers")
    p.set_defaults(func=cmd_retag)

    p = tagging("final", "pass 3: settle rows where passes 1 and 2 disagree")
    p.add_argument("--llm", default="final-tagger", help="LLM file (default final-tagger)")
    p.set_defaults(func=cmd_final)

    p = commands.add_parser("import", help="upload CSV files to csv_records without tagging them")
    p.add_argument("paths", nargs="+", metavar="PATH")
    p.set_defaults(func=cmd_import)

    p = commands.add_parser("export", help="write llm_tags (or claude_retags) to a CSV file")
    p.add_argument("path")
    p.add_argument("--retags", action="store_true")
    p.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    if getattr(args, "dry_run", False) and args.limit is None:
        args.limit = 5
    args.func(args, settings)


if __name__ == "__main__":
    main()
