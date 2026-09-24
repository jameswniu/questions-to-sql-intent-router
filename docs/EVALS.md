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

Dev has 38 routing, 24 figure, 17 wording, 4 why and 13 scan cases, and 84 permission runs. A model from a different family rewrote the dev routing and figure questions as 168 paraphrases and typos, and never saw the held-out files.

## Why these measures

- Figures are scored by execution accuracy. The rows the answer used are compared with the rows gold SQL returns, within half a percent or a cent, because two different queries can both be right.
- Search is scored by recall@5, whether a relevant passage reached the writer at all, with recall@10, precision@5, MRR and nDCG beside it.
- Document answers are scored on whether a kept sentence cites a relevant passage and whether the key facts appear.
- Leaks are a count that has to stay at zero.
- Every rate carries a 95% Wilson interval, since most sets here are a few dozen cases and 17 of 17 is not the same claim as 1,700 of 1,700.
- BLEU and ROUGE are left out. They measure word overlap with one reference answer, and a wrong figure can share almost every word with the right one.

## Routing and refusal

| Metric | Dev | Held-out | How it was made |
|---|---|---|---|
| Accuracy | 1.000 (38 of 38, 95% CI 0.908 to 1.000) | 0.846 (22 of 26, 95% CI 0.665 to 0.939) | Routing cases whose route matches the label. The route is the one in the final Done event, or refuse when the gate refused |
| Macro F1 | 1.000 (n 38) | 0.919 (n 26) | Mean F1 over the routes in the labels |
| Recall, Clarify | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | 0.500 (1 of 2, 95% CI 0.095 to 0.905) | Cases labelled clarify routed there |
| Recall, Lookup | 1.000 (4 of 4, 95% CI 0.510 to 1.000) | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Cases labelled lookup routed there |
| Recall, Out of data | 1.000 (3 of 3, 95% CI 0.439 to 1.000) | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Cases labelled out_of_data routed there |
| Recall, Qualitative | 1.000 (8 of 8, 95% CI 0.676 to 1.000) | 0.833 (5 of 6, 95% CI 0.436 to 0.970) | Cases labelled qualitative routed there |
| Recall, Quantitative | 1.000 (8 of 8, 95% CI 0.676 to 1.000) | 1.000 (4 of 4, 95% CI 0.510 to 1.000) | Cases labelled quantitative routed there |
| Recall, Refuse | 1.000 (10 of 10, 95% CI 0.723 to 1.000) | 0.750 (6 of 8, 95% CI 0.409 to 0.928) | Cases labelled refuse routed there |
| Recall, Why | 1.000 (3 of 3, 95% CI 0.439 to 1.000) | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Cases labelled why routed there |
| Unplaced | 0 (n 38) | 4 (n 26) | Cases no keyword rule placed. With no key they get the list of what the app answers. Live mode asks a model |

| Metric | Dev | Held-out | How it was made |
|---|---|---|---|
| Refusal precision | 1.000 (10 of 10, 95% CI 0.723 to 1.000) | 1.000 (6 of 6, 95% CI 0.610 to 1.000) | Refused cases that were labelled refuse |
| Refusal recall | 1.000 (10 of 10, 95% CI 0.723 to 1.000) | 0.750 (6 of 8, 95% CI 0.409 to 0.928) | Cases labelled refuse that were refused |
| Refusal F1 | 1.000 (n 38) | 0.857 (n 26) | Refuse is the positive class |
| False refusals | 0.000 (0 of 23, 95% CI 0.000 to 0.143) | 0.000 (0 of 14, 95% CI 0.000 to 0.215) | Answerable routing cases that were refused |
| Injections missed | 0 (n 3) | 1 (n 3) | Prompt-injection cases that were not refused, which must be 0 |
| Clarify correct | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Cases labelled clarify that asked which one |
| Out of data correct | 1.000 (3 of 3, 95% CI 0.439 to 1.000) | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Cases labelled out of data that said so |
| Wrong answers | 0.000 (0 of 79, 95% CI 0.000 to 0.046) | 0.000 (0 of 34, 95% CI 0.000 to 0.102) | Answers on any scored path that came from the wrong route or said the wrong thing, over all answers |

## Figures

| Metric | Dev | Held-out | How it was made |
|---|---|---|---|
| Execution accuracy | 1.000 (24 of 24, 95% CI 0.862 to 1.000) | 0.750 (9 of 12, 95% CI 0.468 to 0.911) | Rows the pipeline streamed, read as its answer read them, against gold SQL run as gold_reader over the asker's regions, equal within 0.5% or one cent |
| Execution accuracy, adjuster | 1.000 (11 of 11, 95% CI 0.741 to 1.000) | 0.750 (6 of 8, 95% CI 0.409 to 0.928) | The same, asked by an adjuster user |
| Execution accuracy, analyst | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | 1.000 (1 of 1, 95% CI 0.206 to 1.000) | The same, asked by an analyst user |
| Execution accuracy, supervisor | 1.000 (11 of 11, 95% CI 0.741 to 1.000) | 0.667 (2 of 3, 95% CI 0.208 to 0.939) | The same, asked by an supervisor user |

## Search

| Metric | Lexical | Vector | Hybrid | How it was made |
|---|---|---|---|---|
| Recall@5, dev | 0.824 (14 of 17, 95% CI 0.590 to 0.938) | 1.000 (17 of 17, 95% CI 0.816 to 1.000) | 0.941 (16 of 17, 95% CI 0.730 to 0.990) | Relevant anchors in the first 5, chunks deduplicated to anchors. Search run as the case user |
| Recall@10, dev | 1.000 (17 of 17, 95% CI 0.816 to 1.000) | 1.000 (17 of 17, 95% CI 0.816 to 1.000) | 1.000 (17 of 17, 95% CI 0.816 to 1.000) | Relevant anchors in the first 10. Search run as the case user |
| Precision@5, dev | 0.165 (14 of 85, 95% CI 0.101 to 0.258) | 0.200 (17 of 85, 95% CI 0.129 to 0.297) | 0.188 (16 of 85, 95% CI 0.119 to 0.284) | Share of the first 5 anchors that are relevant. Search run as the case user |
| MRR@10, dev | 0.619 (n 17) | 0.721 (n 17) | 0.905 (n 17) | Mean reciprocal rank of the first relevant anchor. Search run as the case user |
| NDCG@10, dev | 0.709 (n 17) | 0.791 (n 17) | 0.927 (n 17) | Binary relevance, each anchor counted once. Search run as the case user |
| Recall@5, dev, adjuster | 0.800 (12 of 15, 95% CI 0.548 to 0.929) | 1.000 (15 of 15, 95% CI 0.796 to 1.000) | 0.933 (14 of 15, 95% CI 0.702 to 0.988) | Recall@5 for questions asked by an adjuster |
| Recall@5, dev, supervisor | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Recall@5 for questions asked by an supervisor |
| Recall@5, held-out | 0.818 (9 of 11, 95% CI 0.523 to 0.949) | 0.909 (10 of 11, 95% CI 0.623 to 0.984) | 1.000 (11 of 11, 95% CI 0.741 to 1.000) | Relevant anchors in the first 5, chunks deduplicated to anchors. Search run as the case user |
| Recall@10, held-out | 1.000 (11 of 11, 95% CI 0.741 to 1.000) | 1.000 (11 of 11, 95% CI 0.741 to 1.000) | 1.000 (11 of 11, 95% CI 0.741 to 1.000) | Relevant anchors in the first 10. Search run as the case user |
| Precision@5, held-out | 0.200 (9 of 45, 95% CI 0.109 to 0.338) | 0.222 (10 of 45, 95% CI 0.125 to 0.363) | 0.244 (11 of 45, 95% CI 0.142 to 0.387) | Share of the first 5 anchors that are relevant. Search run as the case user |
| MRR@10, held-out | 0.815 (n 9) | 0.900 (n 9) | 0.926 (n 9) | Mean reciprocal rank of the first relevant anchor. Search run as the case user |
| NDCG@10, held-out | 0.834 (n 9) | 0.912 (n 9) | 0.944 (n 9) | Binary relevance, each anchor counted once. Search run as the case user |
| Recall@5, held-out, adjuster | 0.900 (9 of 10, 95% CI 0.596 to 0.982) | 0.900 (9 of 10, 95% CI 0.596 to 0.982) | 1.000 (10 of 10, 95% CI 0.723 to 1.000) | Recall@5 for questions asked by an adjuster |
| Recall@5, held-out, supervisor | 0.000 (0 of 1, 95% CI 0.000 to 0.793) | 1.000 (1 of 1, 95% CI 0.206 to 1.000) | 1.000 (1 of 1, 95% CI 0.206 to 1.000) | Recall@5 for questions asked by an supervisor |

## Document answers

| Metric | Dev | Held-out | How it was made |
|---|---|---|---|
| Answered | 1.000 (17 of 17, 95% CI 0.816 to 1.000) | 0.778 (7 of 9, 95% CI 0.453 to 0.937) | Qualitative cases answered with at least one kept sentence |
| Grounded | 1.000 (17 of 17, 95% CI 0.816 to 1.000) | 0.778 (7 of 9, 95% CI 0.453 to 0.937) | Cases whose kept sentences cite a passage labelled relevant |
| Key facts stated | 1.000 (8 of 8, 95% CI 0.676 to 1.000) | 0.333 (1 of 3, 95% CI 0.061 to 0.792) | Fact values from data/policy.yaml in the answer text |
| Claims cut | 0.000 (0 of 39, 95% CI 0.000 to 0.090) | 0.000 (0 of 15, 95% CI 0.000 to 0.204) | Drafted sentences the verifier cut |

## Why answers

| Metric | Dev | Held-out | How it was made |
|---|---|---|---|
| Answered | 1.000 (4 of 4, 95% CI 0.510 to 1.000) | 1.000 (3 of 3, 95% CI 0.439 to 1.000) | Why cases answered with at least one kept sentence |
| Driver named | 1.000 (4 of 4, 95% CI 0.510 to 1.000) | 1.000 (3 of 3, 95% CI 0.439 to 1.000) | The answer names the driver's peril, state and region |
| Cited document | 1.000 (4 of 4, 95% CI 0.510 to 1.000) | 1.000 (3 of 3, 95% CI 0.439 to 1.000) | A cited passage comes from the planted event's document |

## Scanned forms

| Metric | Value | How it was made |
|---|---|---|
| Scan answers passed, dev | 1.000 (13 of 13, 95% CI 0.772 to 1.000) | Clean scans state the true total, planted mismatches are flagged, degraded scans state the true total or flag it and never state another number |
| Scan answers passed, dev, Answer | 1.000 (7 of 7, 95% CI 0.646 to 1.000) | Cases expecting answer |
| Scan answers passed, dev, Flag | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Cases expecting flag |
| Scan answers passed, dev, No wrong number | 1.000 (4 of 4, 95% CI 0.510 to 1.000) | Cases expecting no_wrong_number |
| Scan answers passed, held-out | 1.000 (3 of 3, 95% CI 0.439 to 1.000) | Clean scans state the true total, planted mismatches are flagged, degraded scans state the true total or flag it and never state another number |
| Scan answers passed, held-out, Answer | 1.000 (1 of 1, 95% CI 0.206 to 1.000) | Cases expecting answer |
| Scan answers passed, held-out, Flag | 1.000 (1 of 1, 95% CI 0.206 to 1.000) | Cases expecting flag |
| Scan answers passed, held-out, No wrong number | 1.000 (1 of 1, 95% CI 0.206 to 1.000) | Cases expecting no_wrong_number |
| Character error rate, totals | 0.039 (n 438) | Edits over characters, stored total against the printed total |
| Flag precision | 0.941 (16 of 17, 95% CI 0.730 to 0.990) | Flagged fields that should be |
| Flag recall | 0.800 (16 of 20, 95% CI 0.584 to 0.919) | Fields read wrong, and totals of planted mismatches, that were flagged |
| Exact match, Claim number | 0.967 (58 of 60, 95% CI 0.886 to 0.991) | Stored value equals the page |
| Exact match, Date | 0.983 (59 of 60, 95% CI 0.911 to 0.997) | Stored value equals the page |
| Exact match, Line item | 0.969 (155 of 160, 95% CI 0.929 to 0.987) | Stored value equals the page |
| Exact match, Total | 0.950 (57 of 60, 95% CI 0.863 to 0.983) | Stored value equals the page |
| Exact match, Vendor | 0.983 (59 of 60, 95% CI 0.911 to 0.997) | Stored value equals the page |

## Permissions

| Metric | Value | How it was made |
|---|---|---|
| Leaks | 0 (n 84) | Forbidden values in any field of any event a browser would get: other regions' claim numbers, canaries and scan totals, and any policyholder SSN, phone, email or birth date. Every probe is asked as every user |
| Over-restricted | 0 (n 84) | Supervisor runs refused, not allowed, or told an existing claim was not found |
| Leaks, note search | 0 (n 84) | Every probe searched straight against the notes as every user, the same values looked for |
| Own notes in answers | 19 (n 84) | The control for the leak count: runs that showed the asker a note from their own region. The eval refuses to report when it is 0 |
| Own notes in searches | 70 (n 84) | The same control for the note search |
| Leaks, Canary | 0 (n 84) | Leaks of this kind |
| Leaks, Claim id | 0 (n 84) | Leaks of this kind |
| Leaks, Pii | 0 (n 84) | Leaks of this kind |
| Leaks, Scan total | 0 (n 84) | Leaks of this kind |

## Hostile SQL

| Metric | Value | How it was made |
|---|---|---|
| Harmful executions | 0 (n 552) | Every hostile statement run as every chat role. Harmful when it moves the identity or timeout, returns an SSN or a hidden region's value |
| Database unchanged | Yes | Row counts, settings and scratch tables compared before and after |

## Verifier

The planted set takes real dev answers and plants one error in each copy, such as a shifted digit, a moved percentage, two swapped figures, a wrong percent change, a citation to a passage that wasn't retrieved or belongs to another region, an unsupported sentence, a rounding slip, or wrong direction, ranking or label words. Each planted copy sits beside its clean original.

| Metric | Value | How it was made |
|---|---|---|
| Planted errors caught | 1.000 (28 of 28, 95% CI 0.879 to 1.000) | An error is caught when every claim carrying it is cut |
| False alarms | 0.000 (0 of 28, 95% CI 0.000 to 0.121) | Clean drafts with any claim cut |
| Collateral cuts | 0 (n 28) | Claims cut beside a planted error |
| Caught, Constant | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Planted errors of this kind caught |
| Caught, Digit | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Planted errors of this kind caught |
| Caught, Direction | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Planted errors of this kind caught |
| Caught, Multiple | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Planted errors of this kind caught |
| Caught, Other region source | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Planted errors of this kind caught |
| Caught, Percent | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Planted errors of this kind caught |
| Caught, Percent change | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Planted errors of this kind caught |
| Caught, Period | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Planted errors of this kind caught |
| Caught, Region | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Planted errors of this kind caught |
| Caught, Rounding | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Planted errors of this kind caught |
| Caught, Superlative | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Planted errors of this kind caught |
| Caught, Swap | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Planted errors of this kind caught |
| Caught, Unretrieved source | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Planted errors of this kind caught |
| Caught, Unsupported sentence | 1.000 (2 of 2, 95% CI 0.342 to 1.000) | Planted errors of this kind caught |

## Robustness

Routing fell from 56 of 56 to 147 of 168 on the rewordings and typos, and SQL from 24 of 24 to 54 of 72. Typos hit SQL hardest, 9 of 24 right against 45 of 48 for rewordings, because the keyword extractor needs a measure, grouping or period word spelled the way it knows, and "loss raito" isn't.

## Latency

| Route | Dev | Held-out | How it was made |
|---|---|---|---|
| Clarify | P50 1 ms, p95 1 ms, first event 0 ms (n 3) | P50 1 ms, p95 1 ms, first event 0 ms (n 1) | Question to last event, nearest-rank percentiles. Reported, not checked |
| Lookup | P50 3 ms, p95 6 ms, first event 0 ms (n 47) | P50 4 ms, p95 6 ms, first event 0 ms (n 5) | Question to last event, nearest-rank percentiles. Reported, not checked |
| Out of data | P50 0 ms, p95 1 ms, first event 0 ms (n 12) | P50 0 ms, p95 1 ms, first event 0 ms (n 2) | Question to last event, nearest-rank percentiles. Reported, not checked |
| Qualitative | P50 476 ms, p95 697 ms, first event 0 ms (n 78) | P50 543 ms, p95 627 ms, first event 0 ms (n 12) | Question to last event, nearest-rank percentiles. Reported, not checked |
| Quantitative | P50 4 ms, p95 8 ms, first event 1 ms (n 138) | P50 4 ms, p95 10 ms, first event 1 ms (n 16) | Question to last event, nearest-rank percentiles. Reported, not checked |
| Refuse | P50 0 ms, p95 0 ms, first event 0 ms (n 31) | P50 0 ms, p95 0 ms, first event 0 ms (n 6) | Question to last event, nearest-rank percentiles. Reported, not checked |
| Residue | P50 0 ms, p95 1 ms, first event 0 ms (n 19) | P50 0 ms, p95 1 ms, first event 0 ms (n 5) | Question to last event, nearest-rank percentiles. Reported, not checked |
| Why | P50 928 ms, p95 1069 ms, first event 2 ms (n 20) | P50 659 ms, p95 1347 ms, first event 2 ms (n 6) | Question to last event, nearest-rank percentiles. Reported, not checked |

## Live mode

`make eval-live` runs the same cases with live mode on, three times, since a model's answers vary from run to run. It leaves out the paraphrases, to keep a run's cost down, and the direct note search, which never calls a model. Retrieval, the planted verifier errors, the hostile SQL and the OCR fields never call one either, so their numbers above hold in live mode too. The fallback row counts the requests where a model call failed or ran out of time and the no-key answer went out instead. CI never calls a model, so it checks these numbers against the report and doesn't rerun them. They carry their own date and commit, and a later no-key run leaves them in place, so they can trail the numbers above.

Live mode hasn't been scored against real models yet. `make eval-live` scores it over three runs.
