# Claims Q&A

Ask a made-up home insurer a question in plain English, and get an answer from its claims, policy documents or scanned invoices. Every query runs as the asker's own database login, so a West adjuster never sees an East claim, whatever the question says.

[![checks](https://github.com/jameswniu/questions-to-sql-intent-router/actions/workflows/checks.yml/badge.svg?branch=rag-rebuild)](https://github.com/jameswniu/questions-to-sql-intent-router/actions/workflows/checks.yml)
[![tests](https://github.com/jameswniu/questions-to-sql-intent-router/actions/workflows/tests.yml/badge.svg?branch=rag-rebuild)](https://github.com/jameswniu/questions-to-sql-intent-router/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

## Run it

You need Docker 24 or later and about 1.5 GB of memory, enough for the stack and one analysis job.

```sh
git clone https://github.com/jameswniu/questions-to-sql-intent-router
cd questions-to-sql-intent-router
git switch rag-rebuild
make up
```

The first run seeds 6,951 claims and reads 60 scanned forms, which took 2 minutes on a GitHub arm runner. Then open http://127.0.0.1:8000, pick a user and ask. No API key is needed. `make test` runs the tests.

<img src="docs/demo/ask.gif" alt="Dana, the West adjuster, asks how much was paid on hail claims in Colorado in the second quarter of 2025 and reads the $4,108,453 answer, then opens the evidence to the SQL behind it, its bound values and the row it returned." width="100%">

| Demo | Length |
|---|---|
| [A figure and its SQL](https://cdn.jsdelivr.net/gh/jameswniu/questions-to-sql-intent-router@1b00fc3759f14502f5775c8b20bea64c7b9ae290/docs/demo/ask.mp4) | 54 s |
| [A policy answer and its source](https://cdn.jsdelivr.net/gh/jameswniu/questions-to-sql-intent-router@1b00fc3759f14502f5775c8b20bea64c7b9ae290/docs/demo/policy.mp4) | 77 s |
| [A total read from a scan](https://cdn.jsdelivr.net/gh/jameswniu/questions-to-sql-intent-router@1b00fc3759f14502f5775c8b20bea64c7b9ae290/docs/demo/scan.mp4) | 60 s |
| [Why losses rose](https://cdn.jsdelivr.net/gh/jameswniu/questions-to-sql-intent-router@1b00fc3759f14502f5775c8b20bea64c7b9ae290/docs/demo/why.mp4) | 100 s |
| [One claim, three users](https://cdn.jsdelivr.net/gh/jameswniu/questions-to-sql-intent-router@1b00fc3759f14502f5775c8b20bea64c7b9ae290/docs/demo/permissions.mp4) | 84 s |
| [Withheld small groups](https://cdn.jsdelivr.net/gh/jameswniu/questions-to-sql-intent-router@1b00fc3759f14502f5775c8b20bea64c7b9ae290/docs/demo/suppression.mp4) | 85 s |
| [A question with no period](https://cdn.jsdelivr.net/gh/jameswniu/questions-to-sql-intent-router@1b00fc3759f14502f5775c8b20bea64c7b9ae290/docs/demo/clarify.mp4) | 66 s |
| [An instruction override](https://cdn.jsdelivr.net/gh/jameswniu/questions-to-sql-intent-router@1b00fc3759f14502f5775c8b20bea64c7b9ae290/docs/demo/injection.mp4) | 29 s |
| [An off-topic question](https://cdn.jsdelivr.net/gh/jameswniu/questions-to-sql-intent-router@1b00fc3759f14502f5775c8b20bea64c7b9ae290/docs/demo/off-topic.mp4) | 23 s |
| [A year outside the data](https://cdn.jsdelivr.net/gh/jameswniu/questions-to-sql-intent-router@1b00fc3759f14502f5775c8b20bea64c7b9ae290/docs/demo/out-of-range.mp4) | 25 s |
| [The dashboard by source](https://cdn.jsdelivr.net/gh/jameswniu/questions-to-sql-intent-router@1b00fc3759f14502f5775c8b20bea64c7b9ae290/docs/demo/dashboard.mp4) | 105 s |

## How it works

<img src="docs/figures/system-map.svg" alt="A question passes the gate and the router, then the lookup, figures, documents or why path. Every path queries Postgres as the asker's own login, and the why path also uses a sandbox. The verifier checks the draft before the answer. Ingest loads documents and scans into Postgres." width="100%">

- The gate turns away injection attempts and off-topic questions.
- Keyword rules pick one of four paths, or ask a clarifying question.
- The verifier cuts any sentence whose figure or citation doesn't trace to the evidence.
- No step needs a language model. `LLM_BACKEND` turns on live mode, which adds Claude.

## What could go wrong, and what stops it

| Risk | What stops it | Measured |
|---|---|---|
| An adjuster asks about another region | Their own login, under row-level security | Found {{n dev.permissions.leaks}} leaks in {{n dev.permissions.runs}} runs |
| Generated SQL writes or reads PII | Read-only logins with no PII grants | Ran {{n shared.hostile_sql.statements}} hostile statements, {{n shared.hostile_sql.harmful}} did harm |
| Analysis code runs wild | A throwaway container with no network | All [13 hostile programs](tests/sandbox/test_limits.py) contained |
| A stored note hides an instruction | Ingest quarantines it before search | Quarantined [20 of 20 planted notes](tests/docs/test_injection_screen.py) |
| An injection or off-topic question | The gate refuses it before routing | Refused {{n dev.refusal.recall}} dev, {{n heldout.refusal.recall}} held-out |
| An answer states a wrong figure | The verifier cuts untraced sentences | Caught {{n shared.verifier.recall}} planted errors, cut {{n shared.verifier.false_alarms}} clean answers |
| A question is outside the data | The app says so | Right in {{n dev.abstention.out_of_data}} dev, {{n heldout.abstention.out_of_data}} held-out |
| Search misses the right passage | Full-text and vector search, reranked | Recall@5 {{n dev.retrieval.hybrid.recall_at_5}} dev, {{n heldout.retrieval.hybrid.recall_at_5}} held-out |
| OCR drops a decimal point | Amounts must match the claim's payments | Flagged {{n shared.ocr_extraction.flag_recall}} misread fields |

The full list, with tests, is in [DESIGN.md](docs/DESIGN.md#failure-modes).

## Numbers

`make eval` scores these with no API key. Dev cases shaped the rules, held-out cases didn't, and brackets are 95% Wilson intervals.

{{table headline}}

On dev, why answers take {{n dev.latency.why.p50_ms}} ms at the median and everything else under a second. [EVALS.md](docs/EVALS.md) has every table.

Live mode, scored {{n live.date}} over {{n live.runs}} runs for {{n live.cost_usd}} with {{n live.models.main}}, {{n live.models.fast}} and {{n live.models.check}}, got {{n live.metrics.heldout.abstention.wrong_answer}} held-out answers wrong, and leaked {{n live.metrics.dev.permissions.leaks}} rows on the dev split, where the permission probes ran, as [its full table](docs/EVALS.md#live-mode) shows.

## Who sees what

Two adjusters and the analyst ask about the same West claim in [this clip](https://cdn.jsdelivr.net/gh/jameswniu/questions-to-sql-intent-router@1b00fc3759f14502f5775c8b20bea64c7b9ae290/docs/demo/permissions.mp4).

[<img src="docs/demo/permissions.poster.png" alt="Dana, the West adjuster, asks for the status of claim 105964 and reads that it is open, a fire loss in Colorado with $33,474 paid." width="100%">](https://cdn.jsdelivr.net/gh/jameswniu/questions-to-sql-intent-router@1b00fc3759f14502f5775c8b20bea64c7b9ae290/docs/demo/permissions.mp4)

| Who asks | What they get |
|---|---|
| Dana Reyes, claims adjuster, West, as `u_adj_west` | The claim itself: open, a fire loss in Colorado, $33,474 paid |
| Omar Haddad, claims adjuster, East, as `u_adj_east` | The reply a missing claim gets, "I can't find claim 105964." |
| Sam Whitfield, analyst, as `u_analyst` | No claim at all, "Analysts see aggregates only, so I can't open individual claims." |

## Dashboard

`/dashboard`, for operators only, charts the request log beside the latest eval scores.

<img src="docs/demo/dashboard.png" alt="The operator dashboard's request, answer rate, first event and rating tiles, above two routes' latency against their p95 budgets." width="100%">

[The dashboard clip](https://cdn.jsdelivr.net/gh/jameswniu/questions-to-sql-intent-router@1b00fc3759f14502f5775c8b20bea64c7b9ae290/docs/demo/dashboard.mp4) filters it by where each request came from.

## What it doesn't do

- Subtracting two totals can still recover a withheld cell.
- Search slows at millions of chunks.
- `sandboxd` holds the Docker socket, which is root on the host.
- OCR is untested on real paper.
- One person wrote the questions, gold SQL and rules.
- Rewordings and typos drop routing from {{n dev.robustness.routing.original}} to {{n dev.robustness.routing.variants}} and SQL from {{n dev.robustness.sql.original}} to {{n dev.robustness.sql.variants}}.
- The gate missed {{n heldout.refusal.injections_missed}} of {{n heldout.refusal.injections}} held-out injections.
- The verifier doesn't check words like "twice" or "a majority".

## More

[docs/DESIGN.md](docs/DESIGN.md) has every decision and failure mode, and the detail this page leaves out. [docs/REFEREE.md](docs/REFEREE.md) shows how each headline number is counted.

Apache 2.0, see [LICENSE](LICENSE).
