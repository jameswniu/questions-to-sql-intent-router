# Design

The README is the short version. This is the long one, with each decision and what it cost, one request followed through the code, every failure mode I could think of with what stops it and the test that shows it, what it doesn't do and why, and the running and demo details the README leaves out.

## Decisions

| Decision | Chose | Over | Why | Cost accepted |
|---|---|---|---|---|
| Who runs the query | The asker's own Postgres login, one role per job and region | One service account, with WHERE clauses added by the app | The database enforces scope even when the app or a model builds the query wrong | A connection pool per role, and bootstrap has to manage the roles |
| Row filtering | Row-level security keyed on `session_user`, forced on every table, and `security_invoker` views | Views with the filter baked in, or filtering in the app | One policy covers every path into a table, views included | Postgres skips the text index for chat logins, because the full-text match operator isn't leakproof, so lexical search scans every visible chunk, fine at a few hundred chunks and slow at millions |
| The analyst's access | `agg.metric()`, a definer function that withholds small and dominated cells | Table access behind masking views, or differential privacy | Analysts need totals. A withheld cell is easy to explain | Differencing two totals recovers a withheld cell |
| Figure questions | A semantic layer that compiles a typed query to SQL | A model writing SQL | Paid losses means the same thing on every path, and the compiled SQL can be compared with gold SQL | A question outside the vocabulary gets a clarifying question instead of an answer |
| Checking SQL | Grants as the boundary, then a sqlglot allow-list that denies by default | Regex checks, or reading EXPLAIN output | `sqlglot` parses CTEs, casts and function calls, which a regex can't follow | A function missing from its five-name list is refused, even a harmless one |
| Document storage | Postgres, as chunks carrying a tsvector and a 384-dimension vector, under the same policies | A separate vector database | Row-level security covers the chunks the same way it covers claims, with nothing to keep in sync | Vector search is exact, fine at a few hundred chunks and slow at millions |
| Search ranking | Full-text and exact vector search fused by reciprocal rank, then a cross-encoder | Either retriever alone | Full text finds exact terms like "HO-2025", vectors find paraphrases, and the reranker reads both | Two small models in the image and about half a second per question |
| Chunking | On headings, with long tables split into row groups that repeat the header | Fixed-size windows | A citation points at a section a person can find. Each row group still says what its columns mean | Uneven chunk sizes |
| Instructions hidden in notes | Screened at ingest and quarantined, and in live mode read only by models that hold no tools | Filtering at question time only | Even an injection the screen misses can't reach a tool | The live orchestrator plans from ids and labels and gets no document text |
| Reading scans | Tesseract in the image, with every amount checked against the claim's payments | A vision model on every page | The costly error is a dropped decimal, which fails the payment check. Tesseract also reads a page the same way every run | Confidence isn't calibrated, and the scans are synthetic |
| Where analysis code runs | A throwaway container per job, started by `sandboxd` | The app process, or a Python sandbox library | In-process Python sandboxes keep getting escaped. A container gets the kernel's limits on network, memory and processes | The Docker socket is root on the host |
| Checking answers | Tracing every figure and citation to the evidence, then cutting what doesn't trace | An LLM judge alone | Tracing is deterministic and takes milliseconds. A judge from the writer's model family isn't independent | Plain prose with no figure is checked only in live mode |
| Why questions | A fixed workflow that splits the change by driver, then finds the memo | An agent for every why question | It always takes the same three steps, fetching both periods, splitting by driver and finding the memo. An agent would pick them again on every question | It explains only the drivers the semantic layer names |
| Streaming | Server-sent events over POST, cancelled when the client goes away | WebSockets, or polling | The answer only flows one way, over plain HTTP. A closed tab stops the query and the sandbox job | Nothing can be sent from the browser mid-answer |
| Measuring quality | Hand-labelled cases with a held-out split hashed alongside the first rules, scored by execution accuracy, recall@k and Wilson intervals | BLEU or ROUGE, or an LLM judge on one set | String overlap can't tell whether SQL returned the right number. With a few dozen cases, a rate means little without its interval | One author wrote the cases, the rules and the gold SQL |
| Who checks a written sentence | Gemini on Vertex, set by `LLM_CHECK_BACKEND` | Claude's fast model, the writer's own family | A reader from the writer's family can share its blind spots, and a Gemini reading costs a fraction of a cent | A second provider to configure, and Gemini's response time on the global endpoint varies |
| Caching | No cache | Caching answers | Answers depend on who asks. A cache keyed wrong would leak between roles | A repeated question pays the full cost |

## One request, end to end

1. The browser posts the question to `/ask` and reads the reply as a stream ([app/web/app.py](../app/web/app.py), [app/web/stream.py](../app/web/stream.py)).
2. The app works out who is asking, from the demo cookie or the sign-in proxy's header, and applies that person's rate limit ([app/web/auth.py](../app/web/auth.py), [app/web/ratelimit.py](../app/web/ratelimit.py)).
3. The gate screens the text and refuses injection, coding requests, chit-chat and off-topic questions ([app/gate.py](../app/gate.py)).
4. Keyword rules pick a path. Order matters, so a claim number is checked before a policy word, and a policy word before a measure. A question no rule places gets a list of what the app can answer ([app/route.py](../app/route.py), [app/handle.py](../app/handle.py)).
5. The path gathers evidence as the asker.
   - Figures are extracted into a typed query against [semantic/claims.yaml](../semantic/claims.yaml), resolved against the data's as-of date, compiled to SQL over three views, checked by the allow-list and run on the asker's own pool ([app/answer/quant.py](../app/answer/quant.py), [app/semantic/](../app/semantic/), [app/sqlcheck.py](../app/sqlcheck.py), [app/db.py](../app/db.py)).
   - Document questions search the policy wordings, guidelines, memos and adjuster notes the asker can see, and choose passages ([app/answer/qual.py](../app/answer/qual.py), [app/sources/documents.py](../app/sources/documents.py), [app/answer/passages.py](../app/answer/passages.py)).
   - Why questions fetch both periods, split the change by driver in the sandbox and look for the memo or bulletin from around then ([app/answer/why.py](../app/answer/why.py), [app/answer/drivers.py](../app/answer/drivers.py), [app/sandbox/client.py](../app/sandbox/client.py)).
   - A claim number reads one claim through the claim-detail view ([app/answer/lookup.py](../app/answer/lookup.py)), and a question about a scanned form reads its checked fields ([app/answer/scanfield.py](../app/answer/scanfield.py)).
6. The verifier checks every figure against the query and sandbox results and every citation against what was retrieved for this user, and cuts any sentence that fails ([app/verify.py](../app/verify.py)).
7. The stream sends the route, the evidence and the answer, and a closed connection cancels whatever is still running. The evidence panel shows the SQL, rows and passages behind each answer ([app/events.py](../app/events.py), [app/web/static/evidence.js](../app/web/static/evidence.js)).
8. Every answered or refused question writes one request-log row and one audit row, with the question redacted, and emits OpenTelemetry spans ([app/requestlog.py](../app/requestlog.py), [app/telemetry.py](../app/telemetry.py)).

## Failure modes

This is the whole table the README shortens, with what each row measured. The tables after it list every failure mode by where it happens, with the test that shows it.

| What could go wrong | What stops it | Measured |
|---|---|---|
| An adjuster asks about another region's claims | The question runs under the adjuster's own Postgres login. Row-level security on that login filters every table and view, so other regions' rows never leave the database. | 0 leaks in 84 runs, every probe asked as every user ([probes](../evals/cases/permissions.jsonl)) |
| Someone claims to be another user | The user picker only works on loopback. Behind a sign-in proxy, the user comes from the proxy's header, and a request without the proxy's shared secret gets a 401. | [Tested](../tests/web/test_proxy_secret.py) |
| The analyst rebuilds one claim from totals | Analysts can't read claim rows. They call `agg.metric()`, which withholds any cell with fewer than 10 claims or one claim over half its total. | [Tested](../tests/integration/test_aggregates.py), and differencing two totals still works (below) |
| A claim number confirms a claim exists in another region | A hidden claim and a missing one get the same reply. | [Tested](../tests/quant/test_claim_lookup.py) |
| Generated SQL writes, reads a PII column or calls something dangerous | Every login is read-only, its column grants leave out SSN, birth date, email and phone, and risky built-ins are revoked. An AST allow-list checks each statement first, and the grants stay the boundary. | 92 hostile statements run as all 6 roles, 0 did harm |
| Analysis code runs wild | Each job gets a throwaway container with no network, a read-only root, 512 MB, one CPU, 64 processes, 10 seconds and 1 MiB of output, and only the rows the asker already fetched. | [13 hostile programs](../tests/sandbox/test_limits.py), all contained |
| A note in the document store says to ignore your instructions | Ingest screens every chunk and every whole document and keeps matches out of search. In live mode, the models that read documents hold no tools. | [20 of 20 planted](../tests/docs/test_injection_screen.py) quarantined, no clean note |
| A note or a scan leaks an SSN | Notes and OCR text are masked before they're stored or embedded. | [Every planted identifier](../tests/docs/test_masking.py) masked |
| The answer quotes the wrong policy edition | Search keeps only the wording in force on the date the question is about, and a question about a claim reads it on the claim's loss date. | [Tested](../tests/docs/test_document_search.py) |
| Search misses the passage that answers the question | Full-text and vector search, fused by rank, then a cross-encoder rerank. | Recall@5 16 of 17 dev, 11 of 11 held-out |
| OCR drops the decimal point on a total | Every amount has to look like currency and match the claim's payments, or the answer flags it instead of stating it. | 16 of 20 misread fields and payment mismatches flagged |
| A figure in the answer isn't in the data, or it "rose" when it fell | The verifier traces every figure to a query or sandbox result within a stated tolerance, checks direction and ranking words against the traced change, and cuts any sentence that fails. | 28 of 28 planted errors caught, 0 of 28 clean answers cut |
| A question about 2023 gets an invented answer | The data covers January 2024 to June 2026, and a question outside that is told so. | 3 of 3 dev, 2 of 2 held-out |
| Chit-chat or an injection attempt costs a round trip | The gate refuses before routing. | Recall 10 of 10 dev, 6 of 8 held-out. 1 of 3 held-out injections got through |
| A slow query ties up a connection | Every login has a 4 second statement timeout, the client has its own deadline, and closing the tab cancels the query. | [Tested](../tests/integration/test_db.py) |

### Who is asking

| What could go wrong | What stops it | Shown by |
|---|---|---|
| An adjuster reads another region's rows | Row-level security on the adjuster's own login filters every table, view and chunk | `test_adjuster_never_sees_another_region` in [tests/integration/test_row_security.py](../tests/integration/test_row_security.py) |
| A login takes on a group's or another user's identity | Memberships are granted `WITH SET FALSE`, and `set_config` is revoked | `test_adjuster_cannot_take_on_another_identity` |
| A login missing from the principals table sees everything | The policy helper returns nothing for it, so regional policies match no rows | `test_login_missing_from_principals_sees_no_rows` |
| A view reads as its owner and skips the policies | Every view is `security_invoker`, and a test builds a plain view to show the leak it would cause | `test_no_view_reads_as_its_owner`, `test_definer_view_leaks_other_regions_where_the_invoker_view_does_not` |
| An adjuster reads an SSN, birth date, email or phone | The column grants leave those four out | `test_adjuster_cannot_read_policyholder_pii` |
| The analyst reads a claim row | The analyst has no grant on any claims table or view, only on `agg.metric()` and the general documents | `test_analyst_cannot_read_rows_at_all` |
| The analyst lowers the suppression threshold | The threshold and the dominance share live in a settings table only the function's owner can read | `test_analyst_cannot_lower_the_suppression_threshold` in [tests/integration/test_aggregates.py](../tests/integration/test_aggregates.py) |
| The analyst isolates one payment with periods a day apart | Periods must be whole months | `test_period_bounds_a_day_apart_cannot_isolate_one_payment` |
| The analyst subtracts two totals, such as a region minus its other states, to recover a withheld cell | Nothing yet. It needs query auditing or noise | `test_known_gap_complementary_filters_can_still_difference`, `test_known_gap_overlapping_month_ranges_can_still_difference` |
| A claim number or scan id confirms that something exists in another region | A hidden item and a missing one get the same reply | `test_a_missing_claim_and_a_hidden_claim_read_the_same`, `test_another_regions_scan_gets_the_same_404_as_a_missing_one` |
| A client sets the identity header itself | Behind the proxy, a request without the proxy's shared secret is refused before the header is read | [tests/web/test_proxy_secret.py](../tests/web/test_proxy_secret.py) |
| One user's follow-up picks up another user's last question | Memory is kept per user and session, and switching user starts a new session | [tests/test_memory.py](../tests/test_memory.py), [tests/web/test_session.py](../tests/web/test_session.py) |

### What SQL can do

| What could go wrong | What stops it | Shown by |
|---|---|---|
| A statement writes or changes the schema | Chat logins are read-only by default and hold no write grants, and the allow-list accepts one SELECT | 92 hostile statements run as every role, in [tests/guard/test_hostile_execution.py](../tests/guard/test_hostile_execution.py) |
| A built-in with side effects runs, such as an advisory lock or `pg_notify` | Those functions are revoked from PUBLIC by name, every overload | `test_side_effecting_functions_are_refused_and_the_pooled_connection_stays_clean` |
| A query runs forever | A 4 second statement timeout on every chat login, a client deadline half a second later, and cancellation when the browser leaves | `test_slow_query_is_cancelled_and_the_pool_recovers`, `test_role_statement_timeout_cancels_a_runaway_query` |
| A query returns millions of rows | A server-side cursor reads at most 501 rows and marks the answer truncated | `test_row_cap_truncates_and_says_so` |
| A CTE shadows a view, recurses, or a cast reaches the catalog | The allow-list rejects each, and every function call must be on a five-name list | [tests/guard/test_sqlcheck.py](../tests/guard/test_sqlcheck.py) |
| A model writes SQL | In live mode a model only fills a typed query, and the same compiler writes the SQL | [app/semantic/compile.py](../app/semantic/compile.py) |

### Documents, search and scans

| What could go wrong | What stops it | Shown by |
|---|---|---|
| A note carries instructions to the model | Ingest screens every chunk and every whole document, and quarantined chunks never come back from search | [tests/docs/test_injection_screen.py](../tests/docs/test_injection_screen.py) |
| The instruction is split across two chunks | The whole document is screened too, and a match quarantines all of its chunks | `test_injection_split_across_a_chunk_boundary_is_quarantined` |
| The instruction is disguised with look-alike letters, spacing or base64 | The screen normalizes the text and decodes base64 runs before matching | `test_normalize_undoes_disguises` |
| A note leaks a policyholder's details | Notes and OCR text are masked before they're chunked or embedded | [tests/docs/test_masking.py](../tests/docs/test_masking.py), `test_no_policyholder_identifier_reaches_a_chunk` |
| The answer quotes the wrong edition of the policy | A wording chunk comes back only when its edition was in force on the date asked about | `test_policy_wording_follows_the_edition_in_force_on_the_loss_date` |
| A chunk's region drifts from its document's | Foreign keys and triggers refuse a chunk or scan field that disagrees with its document | `test_rag_rows_must_carry_their_documents_sensitivity_region_and_claim` |
| An edited document keeps its old chunks | Each document is re-ingested when its content hash or embedding model changes, and deletes cascade | `test_a_second_ingest_changes_nothing` |
| A stray note file slips into the corpus | Notes are written against a manifest, and ingest refuses any file the manifest doesn't list | [tests/docs/test_notes_manifest.py](../tests/docs/test_notes_manifest.py) |
| An approximate index returns fewer results under row-level security | Vector search is exact. An HNSW index filters after the scan, and a test shows it coming up short until iterative scan is on | `test_filtered_hnsw_scan_comes_up_short_until_iterative_scan_is_on` |
| A question about a claim's notes is answered from another claim's file, or from a guideline about writing notes | Only that claim's own notes may answer, and a claim the asker can't open gets the same reply as a missing one | `test_a_question_for_a_claims_notes_is_answered_from_that_claims_notes_alone` in [tests/answer/test_qual_answers.py](../tests/answer/test_qual_answers.py) |
| OCR drops a decimal point or swaps two digits | Every amount must look like currency and match the claim's payments, and a field that fails is flagged instead of stated | [tests/docs/test_ocr_fields.py](../tests/docs/test_ocr_fields.py) |
| A user opens another region's scan | The image route re-reads the document as the asker and answers a hidden scan with the same 404 as a missing one | [tests/web/test_scan_route.py](../tests/web/test_scan_route.py) |

### Answers

| What could go wrong | What stops it | Shown by |
|---|---|---|
| A figure doesn't match the data | Every figure must trace to a query or sandbox value within the tolerance for how it's written | 28 of 28 planted errors caught, [evals/verifier/](../evals/verifier/) |
| A figure is right but the words around it are wrong | Direction, ranking and multiple words must agree with the traced change or rank, and a group label must match its row | [tests/verify/test_comparisons.py](../tests/verify/test_comparisons.py) |
| A citation points at a passage the asker wasn't given | A cited chunk must have been retrieved for this question and sit in the asker's regions | `test_a_citation_must_name_a_chunk_retrieved_for_this_question` |
| A question about dates outside the data gets an answer | The data covers January 2024 to June 2026, and anything outside is told so | `test_a_period_outside_the_data_names_the_window` |
| A vague question gets a guess | A missing measure or period gets one clarifying question with options that parse | [tests/quant/test_resolve_rules.py](../tests/quant/test_resolve_rules.py) |
| Chit-chat, coding requests or an injection attempt costs work | The gate refuses them before routing | [tests/guard/test_gate_router.py](../tests/guard/test_gate_router.py) |

### Sandbox

| What could go wrong | What stops it | Shown by |
|---|---|---|
| Analysis code opens a socket, forks, eats memory, loops, writes to disk or floods output | No network, 64 processes, 512 MB, a 10 second kill with an in-container backstop, a read-only root and a 1 MiB output cap | [tests/sandbox/test_limits.py](../tests/sandbox/test_limits.py) |
| Analysis code reads the host's secrets | The container gets no environment from the host and runs as uid 65534 with no capabilities | `test_the_environment_holds_nothing_from_the_host` |
| One user floods the sandbox | Two jobs per user and eight per host, and a job's slot is freed only once its container is gone | `test_slots_cap_each_principal_and_the_host` |

### Running it

| What could go wrong | What stops it | Shown by |
|---|---|---|
| One user floods the app | A token bucket per user, 20 questions at once and then one every 3 seconds | [tests/web/test_rate_limit.py](../tests/web/test_rate_limit.py) |
| The request log keeps an SSN someone typed | Questions are redacted before they're logged | [tests/web/test_redaction.py](../tests/web/test_redaction.py) |
| Anyone opens the dashboard | Only users marked as operators get it | [tests/web/test_dashboard.py](../tests/web/test_dashboard.py) |

### Measuring it

| What could go wrong | What stops it | Shown by |
|---|---|---|
| The rules get tuned on the test set | The held-out files were hashed in the same commit as the first rules, and CI refuses to run the evals if one has changed. One scan label was fixed in both splits since, as [EVALS.md](EVALS.md) explains | [tests/harness/test_heldout_lock.py](../tests/harness/test_heldout_lock.py) |
| The page drifts from the real numbers | The README and EVALS.md are rendered from `evals/report.json`, and CI fails when they differ | [tests/recount/](../tests/recount/) |
| The author's own phrasing flatters the rules | A model from a different family rewrote the dev questions with rewordings and typos, and the drop is reported. Typos hit SQL hardest, 9 of 24 right against 45 of 48 for rewordings, because the keyword extractor needs a measure, grouping or period word spelled the way it knows, and "loss raito" isn't | [evals/cases/paraphrase.jsonl](../evals/cases/paraphrase.jsonl) |

## What it doesn't do

The README lists these limits without their reasons.

- Subtracting two published totals can still recover a withheld cell, and two tests show it.
- Lexical search is Postgres full-text search ranked by `ts_rank_cd`, which isn't BM25.
- Row-level security keeps chat logins off the text index, so lexical search is slow at millions of chunks.
- Vector search is exact, because an HNSW index under row-level security returns fewer than k rows unless iterative scan is on.
- `sandboxd` holds the Docker socket, which is root on the host. Production would run jobs in Firecracker or gVisor.
- OCR confidence isn't calibrated, and the scans are generated, so the OCR numbers say little about real paper.
- One person wrote the questions, the labels, the gold SQL and the rules. Rewordings and typos from another model family drop routing from 56 of 56 to 147 of 168 and SQL from 24 of 24 to 54 of 72.
- The gate and the router are English keyword rules, so a reworded injection can get past the gate.
- Comparative wording the verifier doesn't list, such as "twice" or "a majority", goes unchecked.
- There's no knowledge graph, because the relationships already live in SQL, and no answer cache, because answers depend on who asks.

## Live mode

Nothing above calls a language model. The only models it runs are a small embedder and a reranker that ship inside the image. Setting `LLM_BACKEND` to `anthropic` or `vertex` adds Claude where the rules run out, and every model call falls back to the no-key answer on an error, a refusal or a blown budget, and says so. Claude runs on an Anthropic API key or on Vertex, a Google Cloud project with Claude quota, and `LLM_CHECK_BACKEND=gemini` moves the sentence check to Gemini on gcloud's application-default credentials. Claude subscription sign-ins aren't supported, since Anthropic reserves them for its own apps and asks products to use API keys.

- Questions no keyword rule places go to a small model that picks one of the router's own labels, reading only the question.
- The gate and the router are English keyword rules, so a reworded injection can get past the gate. With no key it reaches a path that never calls a model, and in live mode the model that places it holds no tools.
- A figure question the rule extractor can't parse goes to a small model that fills the same typed query. The compiler, the allow-list and the asker's own login take it from there.
- Document answers are written by the main model from the retrieved passages, sent as search-result blocks with citations on and no tools. Each sentence has to pass the verifier and then a reading against the passage it cites, by the fast model or, with `LLM_CHECK_BACKEND=gemini`, by Gemini, and the writer gets one retry with the reasons before anything is cut.
- Why questions get an orchestrator with three helpers, each handed only what its step needs. The SQL helper runs typed queries on the asker's pool. The document helper retrieves as the asker and has a model with no tools pick passages, returning only handles and labels. The analysis helper adapts a sandbox template and has no database handle. The orchestrator holds the tools and sees only handles such as `d1`, `a1` and `c1`.
- In a why answer the headline and driver sentences are built in code from the rows, and the model writes only the cited cause sentences, so every number stays traceable.
- The budget is 8 steps and 60 seconds, with at most 4 tool calls a turn and 12 a run.
- No model gets both tools and document text, so an instruction hidden in a note has no tool to reach. The request builder refuses any request that carries both tools and a passage, so the isolation is enforced in code, and the tests check every request a scripted model receives.

The checker reads the same memo a cause sentence cites, so a memo written to say its claims are supported could talk it into a made-up cause. That sentence can't carry a number and still cites its passage, so a reader can check it. `make eval-live` scores live mode against real models, and [EVALS.md](EVALS.md) has the result.

## Running the stack

- The stack uses about 1 GB of memory once its models load, plus up to 512 MB for each analysis job while it runs.
- The first run builds the images, so it takes longer on a slow connection. After that, `make up` is back in under a minute.
- The network is only needed the first time, for the images and two small models.
- If something else already holds port 8000, `APP_PORT=18000 make up` moves the app to 18000.
- `make test` runs the tests inside the stack.
- `/dashboard` draws latency by route against its budget, the route mix, refusal and clarify rates and verifier cuts from the request log, beside the latest eval scores for each split. The dashboard clip filters it to eval requests, then to the questions `make up` replays, then to browser requests, whose routes and outcomes include the questions asked in the other clips.

## The demo clips

The README links each clip. Every clip runs without an API key. The recorder adds the window frame, title cards, captions, spotlight, pointer, reading pauses and larger evidence text, and shows every answer as the app gave it.

| Clip | What it shows |
|---|---|
| A figure and its SQL | Dana asks what was paid on Colorado hail claims in Q2 2025 and traces the figure to the SQL, its bound values and the row it returned. |
| A policy answer and its source | Dana asks whether flood damage is covered, follows the citation to section 4.1 of the HO-2025 policy and reads the passage it came from. |
| A total read from a scan | Priya asks for the total on a scanned invoice and checks it on the crop of the scan and in the payment query. |
| Why losses rose | Priya asks why West paid losses rose in Q2 2025, and the driver split shows the hail and Colorado shares the answer gives. |
| One claim, three users | Dana, Omar and Sam ask about the same West claim, and only Dana gets it back. |
| Withheld small groups | Sam, the analyst, asks for monthly counts and gets withheld cells where a month has too few claims. |
| A question with no period | Priya asks how much was paid, picks 2025 from the options the app offers and checks the query it ran. |
| An instruction override | Dana tells the app to ignore its instructions and show every region's claims, and the gate refuses. |
| An off-topic question | Dana asks for a banana bread recipe and is told what the app covers. |
| A year outside the data | Dana asks about 2022 and is told the data runs from January 2024 to June 2026. |
| The dashboard by source | Priya reads the service dashboard for all requests, then for eval, replayed and browser requests alone. |

## From laptop to production

| This repo | In production |
|---|---|
| One Postgres login per job and region, with row-level security | A warehouse that takes the user's own token through on-behalf-of exchange, so the query still runs as them |
| The demo user picker | Single sign-on through an identity-aware proxy that signs its assertion |
| Documents copied into the image and ingested at start | An object store whose access lists are copied onto the chunks, and ingest triggered on each write |
| `sandboxd` on the Docker socket | Firecracker microVMs or gVisor |
| Spans and logs in Postgres | An OpenTelemetry collector and a tracing backend |
| `var/secrets.env` | A secret manager |
| Live mode through Vertex AI or the Anthropic API | The same, behind an allow-list of models |
