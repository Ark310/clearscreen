# Quick tests for ClearScreen (client 001)

Made-up people, IDs starting with `TEST-`. Open https://api.clearscreen.example/docs,
click **Authorize** and paste client 001's API key. For each step: open the endpoint, **Try it out**,
paste the whole file's content as the body, **Execute**. Run them in this order.

| # | Endpoint | Body | Expect |
|---|---|---|---|
| 1 | `POST /whitelist/add` | `1_single_add.json` | `added: 1` (TEST-WL-0001) |
| 2 | `POST /whitelist/add` | `2_batch_add.json` | `added: 3` (TEST-WL-0002 to 0004) |
| 3 | `GET /whitelist/{ClientID}/{Unique_ID}` | ClientID `001`, Unique_ID `TEST-WL-0001` | The record from step 1 |
| 4 | `POST /match/main` | `4_python_match.json` | CLEAR, `exact_repeat_whitelisted` (repeat of TEST-WL-0002), well under a second |
| 6 | `POST /match/main` | `6_python_batch.json` | 4 results, well under a second: PY-02 and PY-03 CLEAR `exact_repeat_whitelisted`; PY-04 CLEAR `score_below_threshold` (score 0.48); PY-05 REVIEW `no_listing_details` |
| 5 | `POST /match/main` (or `/match/test` for timings) | `5_ai_match.json` | One AI question. Expected CLEAR `prior_adjudication_applicable`, precedent TEST-WL-0001. **Wait 1 minute after step 1 first**, until Admin -> Embedding shows 0 waiting for client 001 |

Step 5 is the same person as TEST-WL-0001 with a new address (`UNIT 4` added), so it is not an
exact repeat; its score is 0.99 and the only similar record is TEST-WL-0001 (100%), so the AI is
asked once. About 6-10 seconds on the free Groq plan when its allowance is not used up; the AI's
answer can differ, so REVIEW is possible, but CLEAR is expected.

Sending the add files again replaces the records (`replaced` instead of `added`). The TEST- records
stay in client 001's whitelist.

`build_quick_tests.py` rebuilds the files and checks each alert's match score.
