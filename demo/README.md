# ClearScreen demo — Client A (ClientID 001)

**Made-up data.** 10,000 whitelist records from Client A's test dataset (`Whitelist.xlsx`, ClientID 1),
relabelled as ClientID **001**, plus demo alerts that show every way ClearScreen can decide.

| File | What |
|---|---|
| `whitelist\client001_whitelist_part1.json` … `part5.json` | The 10,000 records, 2,000 per file, ready for `POST /whitelist/add` |
| `whitelist\client001_whitelist.xlsx` | The same records as Excel (for **Admin → Data → Load from Excel**, if you prefer) |
| `cases\demo_cases.json` | The demo alerts, grouped, each with its expected outcome and why |
| `Load-Whitelist.ps1` | Loads the whitelist through the API and waits until it is searchable |
| `whitelist_900000\`, `Load-Whitelist-900000.ps1` | Set 2: 10,000 more records, Unique_ID 900000–909999 (see section 2) |
| `Run-Demo.ps1` | Sends the demo alerts and shows a scorecard; saves `results\demo_results_*.csv` |

Both scripts run on **your PC or the server** with Windows PowerShell — nothing to install. They ask
for client 001's API key at a hidden prompt (or read `$env:CLS_API_KEY`) and never save it.

---

## 1. Before you start

- ClearScreen Admin → **Clients**: client **001** exists and you have its API key.
- `https://api.clearscreen.example/health` shows `"status":"ok"`.
- Copy this whole folder to where you will run it, e.g. `C:\ClearScreen-Demo` on your PC.

## 2. Load the whitelist (once)

```powershell
cd C:\ClearScreen-Demo
powershell -ExecutionPolicy Bypass -File .\Load-Whitelist.ps1
```

1. Paste client 001's key when asked (nothing shows while you paste).
2. The five files go in within seconds: `10,000 added`.
3. The script then waits while ClearScreen **embeds** the records for the similar-case search
   (prints the number left every 30 s, with an estimate). This is slow on a CPU: the 10,000 records
   are about 45,000 text pieces; it took **64 minutes on a 16-core machine**, so plan on **several
   hours** on the server (fewer cores) — e.g. load in the evening and run the demo the next day.
   You can close the window — embedding carries on in the server; run the script again later and
   it shows where it is (records already loaded are just replaced).

Groups 1–3 work as soon as the records are loaded; groups 4–7 need the embedding to be finished.

### Set 2 (optional): 10,000 more records, Unique_ID 900000–909999

```powershell
powershell -ExecutionPolicy Bypass -File .\Load-Whitelist-900000.ps1
```

`whitelist_900000\` holds 10,000 **other** Client A records (none of set 1, none of the rows the demo
cases were built from), renumbered 900000–909999, as five JSON parts plus an Excel copy.
`id_map.csv` lists each new Unique_ID next to its original one in `Whitelist.xlsx`. Loading takes
seconds; embedding takes as long as set 1 again. With both sets loaded, client 001 has 20,000
records. A bigger history means more similar cases, so groups 4–5 can come out differently than in
the rehearsal (an alert in group 4 may now find a similar case and go to the AI).

## 3. Run the demo

**Groups 1–4 and the security checks — from your PC, through Cloudflare** (about 5 seconds):
```powershell
powershell -ExecutionPolicy Bypass -File .\Run-Demo.ps1 -SkipAI
```

**Groups 5–7, the AI review — on the server, against the API itself** (see *AI cases and Groq* below):
```powershell
powershell -ExecutionPolicy Bypass -File .\Run-Demo.ps1 -Api http://127.0.0.1:8000 -Category send_to_ai
powershell -ExecutionPolicy Bypass -File .\Run-Demo.ps1 -Api http://127.0.0.1:8000 -Category ai_same_case_new_address
powershell -ExecutionPolicy Bypass -File .\Run-Demo.ps1 -Api http://127.0.0.1:8000 -Category ai_location_now_matches
```
(With a paid Groq plan you can simply run `.\Run-Demo.ps1` for everything from your PC.)

The script calls **`POST /match/main`**, the endpoint clients use (ClearScreen 1.4.0 or later). Each
line shows the case, the expected and the actual decision, the rule that decided and the time.
`[ OK ]` = as expected. At the end: three security checks and the CSV with ClearScreen's explanation
of every decision.

To also see the AI calls and the time spent waiting for Groq, add **`-Endpoint test`**: it calls the
internal `POST /match/test`, which must first be switched on in **ClearScreen Admin → Settings →
Test endpoint** (switch it off again afterwards).

## 4. What the demo shows

59 demo alerts in 7 groups, plus 3 security checks. Every alert is built from Client A's data: group 1
re-sends whitelisted records; groups 2–5 are Client A rows that were **not** among the 10,000 (new
alerts); groups 6–7 are whitelisted records with one deliberate change.

| Group | Alerts | What it shows | Expected |
|---|---|---|---|
| 1. Exact repeats | 12 | A whitelisted case comes in again unchanged | **CLEAR** instantly, no AI (`exact_repeat_whitelisted`) |
| 2. No listing details | 5 | LexisNexis sent "Entity Details NOT AVAILABLE" | **REVIEW** — nothing to compare |
| 3. Clearly not the listed person | 10 | Customer scores below 0.80 against the listing | **CLEAR**, no AI (`score_below_threshold`) |
| 4. Possible match, no history | 8 | Score 0.80+, but no similar whitelisted case to rely on | **REVIEW** (`no_similar_whitelisted`) |
| 5. Possible match with history | 10 | Similar whitelisted cases exist; the AI checks whether their reason applies | AI: 5 REVIEW and 1 CLEAR as in the rehearsal, 4 the AI's own call |
| 6. Same case, address written differently | 6 | Whitelisted case with " UNIT 2" added to the address | AI should **CLEAR** — the earlier reason still applies |
| 7. Location now matches the listing | 8 | Case whitelisted for "different location", customer's location changed to the listing's | AI must **not CLEAR**: 5 **FLAG** expected (REVIEW is also safe); 3 AI's own call, because their reason also cites DOB, name or entity type |
| Security checks | 3 | Wrong key; another client's ID; no ClientID | Refused: `INVALID_API_KEY`, `CLIENT_MISMATCH`, `MISSING_CLIENT_ID` |

**Rehearsal.** The whole demo was rehearsed on a copy of ClearScreen 1.3.0 with exactly this whitelist:
groups 1–4 and the security checks came out **38 of 38 as expected**. Groups 6–7 are designed
outcomes; Groq's free-tier limit stopped the rehearsal before it reached them.

`[ OK ]` = as expected · `[ AI ]` = no fixed expectation, the AI decides · `[DIFF]` = different.
The CSV in `results\` has, per alert, the decision, the rule, ClearScreen's explanation, the
whitelisted record it relied on (`precedent`, `precedent_reasoning`) and the time; with
`-Endpoint test` also the AI calls and the time spent waiting for Groq.

## AI cases and Groq

The AI review itself takes about 5–15 seconds per alert. On Groq's **free tier**, though, ClearScreen
has to wait for Groq's rate limit (each review sends a long prompt, and the free tier allows few tokens
per minute). In the rehearsal, AI-reviewed alerts took **25–110 seconds**, and some gave up with
`AI_LIMIT_REACHED` after several minutes (called `GROQ_LIMIT_REACHED` before 1.4.0).

Through Cloudflare, every request must finish within **100 seconds** (else error 524), so:

- **For the demo:** run groups 5–7 **on the server** with `-Api http://127.0.0.1:8000` (no Cloudflare,
  the script waits up to 10 minutes per alert), one group at a time. If Groq's limit is used up,
  wait a while and run that group again.
- **For production:** use a **paid Groq plan** — then AI alerts take seconds and work through
  Cloudflare. Alternatively, **Admin → Settings → AI reviews per alert** (default 5) can be lowered
  to make fewer AI calls per alert.

## 5. Showing it live in the browser (Swagger)

1. Open `https://api.clearscreen.example/docs` → **Authorize** → paste client 001's key.
2. **POST /match/main** → **Try it out** → paste one alert from `cases\demo_cases.json` (the `alert`
   part) → **Execute**. The response shows `disposition`, `rule`, the `explanation` and, when a
   whitelisted record was relied on, `precedent` and `precedent_reasoning`.
3. **GET /whitelist/001/{Unique_ID}** shows the whitelisted record a decision relied on
   (`precedent` in the response).
4. **GET /search** with `q` = a name from an alert shows client 001's most similar whitelisted records.

To look at the stored data: ClearScreen Admin → **Data → Open SQL Server Management Studio** →
`dbo.WL_Records` (filter `ClientID = '001'`).

## 6. Good to know

- **AI decisions (groups 5–7) can vary between runs.** A case that comes back **REVIEW** instead of
  CLEAR is the safe direction: a human looks at it.
- Running the demo again is harmless: alerts are not stored. Each AI alert uses Groq (up to 5 calls).
- **To remove the demo data:** Admin → Clients → select 001 → **Remove client…** → type
  `DELETE RECORDS`. Or keep the client and reload with `Load-Whitelist.ps1`.
