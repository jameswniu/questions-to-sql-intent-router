# Claims Q&A

Ask a made-up home insurer a question in plain English and get an answer from its claims database, its policy documents or its scanned invoices. Every query runs under the asker's own database login, so an adjuster in the West never sees an East claim, whatever the question says.

[![checks](https://github.com/jameswniu/questions-to-sql-intent-router/actions/workflows/checks.yml/badge.svg?branch=rag-rebuild)](https://github.com/jameswniu/questions-to-sql-intent-router/actions/workflows/checks.yml)
[![tests](https://github.com/jameswniu/questions-to-sql-intent-router/actions/workflows/tests.yml/badge.svg?branch=rag-rebuild)](https://github.com/jameswniu/questions-to-sql-intent-router/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

## Run it

You need Docker 24 or later. The stack uses about 1 GB of memory once its models load, plus up to 512 MB for each analysis job while it runs.

```sh
git clone https://github.com/jameswniu/questions-to-sql-intent-router
cd questions-to-sql-intent-router
git switch rag-rebuild
make up
```

The first run builds the images, seeds 6,951 claims and reads 60 scanned forms. That took 2 minutes on a GitHub arm runner, and takes longer on a slow connection. After that, `make up` is back in under a minute. Open http://127.0.0.1:8000, pick someone from the user list and ask. If something else already holds port 8000, `APP_PORT=18000 make up` moves the app to 18000. There's no API key to set, and the network is only needed the first time, for the images and two small models.

`make test` runs the tests inside the stack. `make eval` re-scores the eval numbers on this page, and CI fails when the page and the scores disagree.

<img src="docs/demo/ask.gif" alt="Dana, the West adjuster, asks how much was paid on hail claims in Colorado in the second quarter of 2025 and reads the $4,108,453 answer, then opens the evidence to the SQL behind it, its bound values and the row it returned." width="100%">

[The same clip as an mp4](docs/demo/ask.mp4). Every clip runs without an API key. The recorder adds the captions, the reading pauses and the larger evidence text, and shows each answer as the app gave it.

| Demo | What it shows | Length |
|---|---|---|
| [A figure and its SQL](docs/demo/ask.mp4) | Dana asks what was paid on Colorado hail claims in Q2 2025 and traces the figure to the SQL, its bound values and the row it returned. | 48 s |
| [A policy answer and its source](docs/demo/policy.mp4) | Dana asks whether flood damage is covered, follows the citation to section 4.1 of the HO-2025 policy and reads the passage it came from. | 70 s |
| [A total read from a scan](docs/demo/scan.mp4) | Priya asks for the total on a scanned invoice and checks it on the crop of the scan and in the payment query. | 55 s |
| [Why losses rose](docs/demo/why.mp4) | Priya asks why West paid losses rose in Q2 2025, and the driver split shows the hail and Colorado shares the answer gives. | 95 s |
| [One claim, three users](docs/demo/permissions.mp4) | Dana, Omar and Sam ask about the same West claim, and only Dana gets it back. | 79 s |
| [Withheld small groups](docs/demo/suppression.mp4) | Sam, the analyst, asks for monthly counts and gets withheld cells where a month has too few claims. | 80 s |
| [A question with no period](docs/demo/clarify.mp4) | Priya asks how much was paid, picks 2025 from the options the app offers and checks the query it ran. | 60 s |
| [An instruction override](docs/demo/injection.mp4) | Dana tells the app to ignore its instructions and show every region's claims, and the gate refuses. | 26 s |
| [An off-topic question](docs/demo/off-topic.mp4) | Dana asks for a banana bread recipe and is told what the app covers. | 21 s |
| [A year outside the data](docs/demo/out-of-range.mp4) | Dana asks about 2022 and is told the data runs from January 2024 to June 2026. | 22 s |
| [The dashboard by source](docs/demo/dashboard.mp4) | Priya reads the service dashboard for all requests, then for eval, replayed and browser requests alone. | 102 s |

## How it works

<img src="docs/figures/system-map.svg" alt="A question passes the gate and the router, takes one of four paths that read Postgres as the asker, and is checked by the verifier before the answer is shown. Documents and scans are ingested into the same database." width="100%">

The gate turns away injection attempts, coding requests, chit-chat and common off-topic subjects. Keyword rules then pick a path, and a question none of them places gets a list of what the app can answer. Figure questions go through a semantic layer that turns the question into a typed query and compiles it to SQL over three views. Questions about the documents go to search over the policy wordings, guidelines, memos and adjuster notes. "Why did this change" questions split the change by driver in a sandbox, then find the memo or bulletin from that period. Before anything reaches the screen, a verifier checks every figure against the query results and every citation against what was retrieved for this user, and cuts any sentence that fails. The evidence panel shows the SQL, rows and passages behind each answer.

None of that needs a language model. The only models are a small embedder and a reranker that ship inside the image. Live mode, switched on with `LLM_BACKEND`, adds Claude. It places questions the keyword rules can't, reads figure questions the extractor can't parse, writes document answers from their passages, and runs why questions with an orchestrator and three helpers. No model gets both tools and document text, so an instruction hidden in a note has no tool to reach. With `LLM_CHECK_BACKEND=gemini`, Gemini reads each written sentence against the passage it cites, so the check doesn't come from the writer's own model family. Claude runs on an Anthropic API key or a Google Cloud project with Claude quota, and Gemini on your gcloud login. Claude subscription sign-ins aren't an option, since Anthropic reserves them for its own apps.

## What could go wrong, and what stops it

| What could go wrong | What stops it | Measured |
|---|---|---|
| An adjuster asks about another region's claims | The question runs under the adjuster's own Postgres login. Row-level security on that login filters every table and view, so other regions' rows never leave the database. | {{n dev.permissions.leaks}} leaks in {{n dev.permissions.runs}} runs, every probe asked as every user ([probes](evals/cases/permissions.jsonl)) |
| Someone claims to be another user | The user picker only works on loopback. Behind a sign-in proxy, the user comes from the proxy's header, and a request without the proxy's shared secret gets a 401. | [Tested](tests/web/test_proxy_secret.py) |
| The analyst rebuilds one claim from totals | Analysts can't read claim rows. They call `agg.metric()`, which withholds any cell with fewer than 10 claims or one claim over half its total. | [Tested](tests/integration/test_aggregates.py), and differencing two totals still works (below) |
| A claim number confirms a claim exists in another region | A hidden claim and a missing one get the same reply. | [Tested](tests/quant/test_claim_lookup.py) |
| Generated SQL writes, reads a PII column or calls something dangerous | Every login is read-only, its column grants leave out SSN, birth date, email and phone, and risky built-ins are revoked. An AST allow-list checks each statement first, and the grants stay the boundary. | {{n shared.hostile_sql.statements}} hostile statements run as all {{n shared.hostile_sql.roles}} roles, {{n shared.hostile_sql.harmful}} did harm |
| Analysis code runs wild | Each job gets a throwaway container with no network, a read-only root, 512 MB, one CPU, 64 processes, 10 seconds and 1 MiB of output, and only the rows the asker already fetched. | [13 hostile programs](tests/sandbox/test_limits.py), all contained |
| A note in the document store says to ignore your instructions | Ingest screens every chunk and every whole document and keeps matches out of search. In live mode, the models that read documents hold no tools. | [20 of 20 planted](tests/docs/test_injection_screen.py) quarantined, no clean note |
| A note or a scan leaks an SSN | Notes and OCR text are masked before they're stored or embedded. | [Every planted identifier](tests/docs/test_masking.py) masked |
| The answer quotes the wrong policy edition | Search keeps only the wording in force on the date the question is about, and a question about a claim reads it on the claim's loss date. | [Tested](tests/docs/test_document_search.py) |
| Search misses the passage that answers the question | Full-text and vector search, fused by rank, then a cross-encoder rerank. | Recall@5 {{n dev.retrieval.hybrid.recall_at_5}} dev, {{n heldout.retrieval.hybrid.recall_at_5}} held-out |
| OCR drops the decimal point on a total | Every amount has to look like currency and match the claim's payments, or the answer flags it instead of stating it. | {{n shared.ocr_extraction.flag_recall}} misread fields and payment mismatches flagged |
| A figure in the answer isn't in the data, or it "rose" when it fell | The verifier traces every figure to a query or sandbox result within a stated tolerance, checks direction and ranking words against the traced change, and cuts any sentence that fails. | {{n shared.verifier.recall}} planted errors caught, {{n shared.verifier.false_alarms}} clean answers cut |
| A question about 2023 gets an invented answer | The data covers January 2024 to June 2026, and a question outside that is told so. | {{n dev.abstention.out_of_data}} dev, {{n heldout.abstention.out_of_data}} held-out |
| Chit-chat or an injection attempt costs a round trip | The gate refuses before routing. | Recall {{n dev.refusal.recall}} dev, {{n heldout.refusal.recall}} held-out. {{n heldout.refusal.injections_missed}} of {{n heldout.refusal.injections}} held-out injections got through |
| A slow query ties up a connection | Every login has a 4 second statement timeout, the client has its own deadline, and closing the tab cancels the query. | [Tested](tests/integration/test_db.py) |

The long version, with the decision behind each row, is in [docs/DESIGN.md](docs/DESIGN.md).

## Numbers

From `make eval` on this commit, with no API key. Dev cases shaped the rules. The held-out cases were hashed into `evals/heldout.lock` in the same commit as the first rules, and CI checks that none has changed. One scan label was corrected in both splits later, as [EVALS.md](docs/EVALS.md) explains. The brackets are 95% Wilson intervals.

{{table headline}}

On dev, why answers take {{n dev.latency.why.p50_ms}} ms at the median and everything else under a second. Every table, with how each number is made, is in [docs/EVALS.md](docs/EVALS.md).

### With live mode on

{{table live}}

## Who sees what

Two adjusters and the analyst ask about the same West claim in [this clip](docs/demo/permissions.mp4).

[<img src="docs/demo/permissions.poster.png" alt="Dana, the West adjuster, asks for the status of claim 105964 and reads it: open, a fire loss in Colorado, $33,474 paid." width="100%">](docs/demo/permissions.mp4)

| Who asks | What they get |
|---|---|
| Dana Reyes, claims adjuster, West, as `u_adj_west` | The claim itself: open, a fire loss in Colorado, $33,474 paid |
| Omar Haddad, claims adjuster, East, as `u_adj_east` | The reply a missing claim gets, "I can't find claim 105964." |
| Sam Whitfield, analyst, as `u_analyst` | No claim at all, "Analysts see aggregates only, so I can't open individual claims." |

## Dashboard

`/dashboard`, for operators only, draws latency by route against its budget, the route mix, refusal and clarify rates and verifier cuts from the request log, beside the latest eval scores for each split.

<img src="docs/demo/dashboard.png" alt="The operator dashboard's request, answer rate, first event and rating tiles, above the lookup route's latency against its p95 budget." width="100%">

[The dashboard clip](docs/demo/dashboard.mp4) filters it to eval requests, then to the questions `make up` replays, then to browser requests, whose routes and outcomes include the questions asked in the clips above.

## What it doesn't do

- Differencing still works against the analyst's suppression. Subtract two published totals, such as a region minus its other states, and a withheld cell comes back. Closing that needs query auditing or noise, and two tests in the suite show the gap.
- Lexical search is Postgres full-text search ranked by `ts_rank_cd`, which isn't BM25.
- Row-level security keeps Postgres off the text index for chat logins, because the full-text match operator is not leakproof. Lexical search scans every visible chunk as a result, fine at a few hundred chunks and slow at millions.
- Vector search is exact. An HNSW index filters after the scan, so under row-level security it returns fewer than k rows unless iterative scan is on, and a test shows it.
- `sandboxd` holds the Docker socket, which is root on the host. Production would run jobs in Firecracker or gVisor.
- OCR confidence isn't calibrated, and the scans are generated, so the OCR numbers say little about real paper.
- One person wrote the questions, the labels, the gold SQL and the rules. Rewordings and typos written by a different model family drop routing from {{n dev.robustness.routing.original}} to {{n dev.robustness.routing.variants}} and SQL from {{n dev.robustness.sql.original}} to {{n dev.robustness.sql.variants}}. Typos hit SQL hardest, {{n dev.robustness.by_variant.typo.sql}} right against {{n dev.robustness.by_variant.paraphrase.sql}} for rewordings, because the keyword extractor needs a measure, grouping or period word spelled the way it knows, and "loss raito" isn't.
- The gate and the router are English keyword rules, so a reworded injection can get past the gate. With no key it reaches a path that never calls a model, and in live mode the model that places it holds no tools.
- Comparative wording the verifier doesn't list, such as "twice" or "a majority", goes unchecked.
- There's no knowledge graph, because the relationships already live in SQL, and no answer cache, because answers depend on who asks.

## More

Each decision, what it was chosen over and what it cost is in [docs/DESIGN.md](docs/DESIGN.md), with the full list of failure modes. [docs/REFEREE.md](docs/REFEREE.md) sets out the rules behind each headline number.

Apache 2.0, see [LICENSE](LICENSE).
