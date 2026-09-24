# Design

The README's table is the short version. This is the long one, with each decision and what it cost, one request followed through the code, and every failure mode I could think of with what stops it and the test that shows it.

## Decisions

| Decision | Chose | Over | Why | Cost accepted |
|---|---|---|---|---|
| Who runs the query | The asker's own Postgres login, one role per job and region | One service account, with WHERE clauses added by the app | The database enforces scope even when the app or a model builds the query wrong | A connection pool per role, and bootstrap has to manage the roles |
| Row filtering | Row-level security keyed on `session_user`, forced on every table, and `security_invoker` views | Views with the filter baked in, or filtering in the app | One policy covers every path into a table, views included | Postgres skips the text index for chat logins, so lexical search scans |
| The analyst's access | `agg.metric()`, a definer function that withholds small and dominated cells | Table access behind masking views, or differential privacy | Analysts need totals. A withheld cell is easy to explain | Differencing two totals recovers a withheld cell |
| Figure questions | A semantic layer that compiles a typed query to SQL | A model writing SQL | Paid losses means the same thing on every path, and the compiled SQL can be compared with gold SQL | A question outside the vocabulary gets a clarifying question instead of an answer |
| Checking SQL | Grants as the boundary, then a sqlglot allow-list that denies by default | Regex checks, or reading EXPLAIN output | sqlglot parses CTEs, casts and function calls, which a regex can't follow | A function missing from its five-name list is refused, even a harmless one |
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
| Caching | No cache | Caching answers | Answers depend on who asks. A cache keyed wrong would leak between roles | A repeated question pays the full cost |

## One request, end to end

1. The browser posts the question to `/ask` and reads the reply as a stream ([app/web/app.py](../app/web/app.py), [app/web/stream.py](../app/web/stream.py)).
2. The app works out who is asking, from the demo cookie or the sign-in proxy's header, and applies that person's rate limit ([app/web/auth.py](../app/web/auth.py), [app/web/ratelimit.py](../app/web/ratelimit.py)).
3. The gate screens the text and refuses injection, coding requests, chit-chat and off-topic questions ([app/gate.py](../app/gate.py)).
4. Keyword rules pick a path. Order matters, so a claim number is checked before a policy word, and a policy word before a measure ([app/route.py](../app/route.py)).
5. The path gathers evidence as the asker.
   - Figures are extracted into a typed query against [semantic/claims.yaml](../semantic/claims.yaml), resolved against the data's as-of date, compiled to SQL, checked by the allow-list and run on the asker's own pool ([app/answer/quant.py](../app/answer/quant.py), [app/semantic/](../app/semantic/), [app/sqlcheck.py](../app/sqlcheck.py), [app/db.py](../app/db.py)).
   - Document questions search the chunks the asker can see, a claim's notes included, and choose passages ([app/answer/qual.py](../app/answer/qual.py), [app/sources/documents.py](../app/sources/documents.py), [app/answer/passages.py](../app/answer/passages.py)).
   - Why questions fetch both periods, split the change by driver in the sandbox and look for the memo or bulletin from around then ([app/answer/why.py](../app/answer/why.py), [app/answer/drivers.py](../app/answer/drivers.py), [app/sandbox/client.py](../app/sandbox/client.py)).
   - A claim number reads one claim through the claim-detail view ([app/answer/lookup.py](../app/answer/lookup.py)), and a question about a scanned form reads its checked fields ([app/answer/scanfield.py](../app/answer/scanfield.py)).
6. The verifier checks the draft against that evidence and keeps what traces to it ([app/verify.py](../app/verify.py)).
7. The stream sends the route, the evidence and the answer, and a closed connection cancels whatever is still running ([app/events.py](../app/events.py)).
8. Every answered or refused question writes one request-log row and one audit row, with the question redacted, and emits OpenTelemetry spans ([app/requestlog.py](../app/requestlog.py), [app/telemetry.py](../app/telemetry.py)).

## Failure modes

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
| The analyst subtracts two totals to recover a withheld cell | Nothing yet. It needs query auditing or noise | `test_known_gap_complementary_filters_can_still_difference`, `test_known_gap_overlapping_month_ranges_can_still_difference` |
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
| An approximate index returns fewer results under row-level security | Vector search is exact. A test shows HNSW coming up short until iterative scan is on | `test_filtered_hnsw_scan_comes_up_short_until_iterative_scan_is_on` |
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
| The rules get tuned on the test set | The held-out files were hashed in the same commit as the first rules, and CI refuses to run the evals if one has changed | [tests/harness/test_heldout_lock.py](../tests/harness/test_heldout_lock.py) |
| The page drifts from the real numbers | The README and EVALS.md are rendered from `evals/report.json`, and CI fails when they differ | [tests/recount/](../tests/recount/) |
| The author's own phrasing flatters the rules | A model from a different family paraphrased the dev questions, and the drop is reported | [evals/cases/paraphrase.jsonl](../evals/cases/paraphrase.jsonl) |

## Live mode

Nothing above calls a language model. Setting `LLM_BACKEND` to `anthropic` or `vertex` adds one where the rules run out, and every model call falls back to the no-key answer on an error, a refusal or a blown budget, and says so.

- Questions no keyword rule places go to a small model that picks one of the router's own labels, reading only the question.
- A figure question the rule extractor can't parse goes to a small model that fills the same typed query. The compiler, the allow-list and the asker's own login take it from there.
- Document answers are written by the main model from the retrieved passages, sent as search-result blocks with citations on and no tools. Each sentence has to pass the verifier and then a reading by the small model against the passage it cites, and the writer gets one retry with the reasons before anything is cut.
- Why questions get an orchestrator with three helpers, each handed only what its step needs. The SQL helper runs typed queries on the asker's pool. The document helper retrieves as the asker and has a model with no tools pick passages, returning only handles and labels. The analysis helper adapts a sandbox template and has no database handle. The orchestrator holds the tools and sees only handles such as `d1`, `a1` and `c1`.
- In a why answer the headline and driver sentences are built in code from the rows, and the model writes only the cited cause sentences, so every number stays traceable.
- The budget is 8 steps and 25 seconds, with at most 4 tool calls a turn and 12 a run.
- The request builder refuses any request that carries both tools and a passage, so the isolation is enforced in code, and the tests check every request a scripted model receives.

The small model that checks a cause sentence reads the same memo the sentence cites, so a memo written to say its claims are supported could talk it into a made-up cause. That sentence can't carry a number and still cites its passage, so a reader can check it. Live mode is tested against a scripted model and hasn't been scored against a real one.

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
