# Evals

Every number here comes from `evals/report.json`, which `make eval` writes by running each case through the same streaming path the browser uses, with no API key. `make claims` rewrites this page and the README from it, and CI fails if either has drifted.

## The cases

There are two splits. The dev cases shaped the rules, so a misrouted dev question could become a new rule. The held-out cases were written at the same time and hashed into `evals/heldout.lock` in the commit that added the first rules, and CI refuses to run the evals if a held-out file no longer matches its hash. One label changed after hashing. Degraded scans used to expect a flag, which scored a correct reading as a miss, so in both splits they now pass when the answer states the true total or flags it, and that one file's hash was recomputed. A hash only shows the files match it. It can't stop a case and its hash being rewritten together, so the git history of `evals/heldout/` is the record of every change.

Each kind of case gets its right answer from somewhere the system under test doesn't touch.

- Figures carry hand-written gold SQL over the base tables. It runs as `gold_reader`, a login that bypasses row-level security, narrowed to the asker's regions by hand, so every figure case also tests the policies, the views and the compiler at once.
- Document questions carry the anchors of the passages that answer them and the fact values from `data/policy.yaml` a right answer has to state.
- Why questions carry the event planted in `data/events.yaml` and the memo or bulletin that explains it.
- Scan questions check against `data/scans/truth.jsonl`, written by the same generator that drew the scans.
- Permission probes run as every user, and every event the browser would get is searched for another region's claim numbers, note canaries, scan totals and any policyholder SSN, phone, email or birth date.

Dev has {{n dev.cases.routing}} routing, {{n dev.cases.quantitative}} figure, {{n dev.cases.qualitative}} wording, {{n dev.cases.why}} why and {{n dev.cases.ocr}} scan cases, and {{n dev.cases.permissions}} permission runs. A model from a different family rewrote the dev routing and figure questions as {{n dev.cases.paraphrase}} paraphrases and typos, and never saw the held-out files.

## Why these measures

- Figures are scored by execution accuracy. The rows the answer used are compared with the rows gold SQL returns, within half a percent or a cent, because two different queries can both be right.
- Search is scored by recall@5, whether a relevant passage reached the writer at all, with recall@10, precision@5, MRR and nDCG beside it.
- Document answers are scored on whether a kept sentence cites a relevant passage and whether the key facts appear.
- Leaks are a count that has to stay at zero.
- Every rate carries a 95% Wilson interval, since most sets here are a few dozen cases and 17 of 17 is not the same claim as 1,700 of 1,700.
- BLEU and ROUGE are left out. They measure word overlap with one reference answer, and a wrong figure can share almost every word with the right one.

## Routing and refusal

{{table routing}}

{{table refusal}}

## Figures

{{table sql}}

## Search

{{table retrieval}}

## Document answers

{{table answers}}

## Why answers

{{table why}}

## Scanned forms

{{table ocr}}

## Permissions

{{table permissions}}

## Hostile SQL

{{table hostile_sql}}

## Verifier

The planted set takes real dev answers and plants one error in each copy, such as a shifted digit, a moved percentage, two swapped figures, a wrong percent change, a citation to a passage that wasn't retrieved or belongs to another region, an unsupported sentence, a rounding slip, or wrong direction, ranking or label words. Each planted copy sits beside its clean original.

{{table verifier}}

## Robustness

Routing fell from {{n dev.robustness.routing.original}} to {{n dev.robustness.routing.variants}} on the rewordings and typos, and SQL from {{n dev.robustness.sql.original}} to {{n dev.robustness.sql.variants}}. Typos hit SQL hardest, {{n dev.robustness.by_variant.typo.sql}} right against {{n dev.robustness.by_variant.paraphrase.sql}} for rewordings, because the keyword extractor needs a measure, grouping or period word spelled the way it knows, and "loss raito" isn't.

## Latency

{{table latency}}

## Live mode

`make eval-live` runs the same cases with live mode on, three times, since a model's answers vary from run to run. It leaves out the paraphrases, to keep a run's cost down, and the direct note search, which never calls a model. Retrieval, the planted verifier errors, the hostile SQL and the OCR fields never call one either, so their numbers above hold in live mode too. The fallback row counts the requests where a model call failed or ran out of time and the no-key answer went out instead. CI never calls a model, so it checks these numbers against the report and doesn't rerun them. They carry their own date and commit, and a later no-key run leaves them in place, so they can trail the numbers above.

{{table live}}
