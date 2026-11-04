# -*- coding: utf-8 -*-
"""End-to-end check of a running ClearScreen, with MADE-UP data only.

  set CLS_API_KEY=<a test client's key>          (the key is read from the environment, not the command line)
  python tools/smoke_test.py https://api.clearscreen.example <ClientID> [--other-key-env CLS_OTHER_KEY]

It loads two made-up whitelist records for the client, waits until they are embedded, then sends
four alerts (exact repeat, AI clear, AI flag, wrong client) and checks the error codes. Use a test
client: the made-up records stay in that client's whitelist (Unique_IDs start with SMOKE-).
Exit code 0 = all checks passed.
"""
import argparse
import os
import sys
import time

import httpx

FIELDS = "Beneficiary Name,Beneficiary Date of Birth,Beneficiary Country,Beneficiary City,Beneficiary Province"
SEP = "-" * 50


def listing(city, country, dob, nationality):
    return (f"Entity   Name: JANE Q DOE   Reference No.:58219   Reason: OFAC SDN   Notes:Program: SDGT   "
            f"Address Information    {SEP}    Address Type: Unknown   City: {city}   Country: {country}   "
            f"AKA Listing NOT AVAILABLE   Additional Information    {SEP}    DOB: {dob}    {SEP}    Nationality: {nationality}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url")
    ap.add_argument("client_id")
    ap.add_argument("--other-key-env", help="environment variable holding ANOTHER client's key (isolation check)")
    ap.add_argument("--insecure", action="store_true", help="do not verify the TLS certificate (testing only)")
    a = ap.parse_args()
    key = os.environ.get("CLS_API_KEY")
    if not key:
        sys.exit("Set CLS_API_KEY to the test client's API key first.")
    http = httpx.Client(base_url=a.base_url.rstrip("/"), headers={"X-API-Key": key}, timeout=600, verify=not a.insecure)
    cid = a.client_id
    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}", flush=True)

    record = dict(ClientID=cid, Unique_ID="SMOKE-WL-1", Screened_Fields=FIELDS,
                  Screened_Values="Jane Doe,1980-05-10,Canada,Toronto,Ontario", Database_Screened="OFAC SDN.BDF",
                  Screened_Date="1/1/2026 9:00:00 AM", Matched_Details=listing("Vancouver", "Canada", "1955-02-01", "Canada"),
                  Analyst_Allow_Reasoning="Customer born 1980-05-10; the listed person was born 1955-02-01. "
                                          "Different date of birth: not the same person.")
    print("1. Health")
    h = http.get("/health").json()
    check("service healthy", h.get("status") == "ok", str(h.get("components")))
    check("key recognised", h.get("client", {}).get("ClientID", "").lower() == cid.lower())

    print("2. Whitelist")
    r = http.post("/whitelist/add", json=[record, record | {"Unique_ID": "SMOKE-WL-2", "Screened_Values": "Jane Doe,1980-05-10,Canada,Ottawa,Ontario"}])
    check("records added", r.status_code == 200, r.text[:200])
    print("   waiting for background embedding", end="", flush=True)
    for _ in range(120):
        if http.get("/health").json().get("client", {}).get("pending_embeddings", 1) == 0:
            break
        print(".", end="", flush=True)
        time.sleep(5)
    print()
    check("records embedded", http.get("/health").json()["client"]["pending_embeddings"] == 0)
    check("record readable", http.get(f"/whitelist/{cid}/SMOKE-WL-1").status_code == 200)

    print("3. Matching (the AI cases call Groq)")
    alert = {k: v for k, v in record.items() if k != "Analyst_Allow_Reasoning"}
    cases = [
        ("exact repeat -> CLEAR", alert | {"Unique_ID": "SMOKE-A1"}, {"CLEAR"}, "exact_repeat_whitelisted"),
        ("listed DOB now 1962: still a different person -> CLEAR (AI)",
         alert | {"Unique_ID": "SMOKE-A2", "Matched_Details": listing("Vancouver", "Canada", "1962-09-17", "Canada")},
         {"CLEAR"}, "prior_adjudication_applicable"),
        ("listed DOB now 1980-05-10, the customer's: the reason is gone -> FLAG or REVIEW (AI)",
         alert | {"Unique_ID": "SMOKE-A3", "Matched_Details": listing("Vancouver", "Canada", "1980-05-10", "Canada")},
         {"FLAG", "REVIEW"}, None),
    ]
    for name, body, want, rule in cases:
        started = time.time()
        r = http.post("/match/groq", json=body)
        if r.status_code != 200:
            check(name, False, f"HTTP {r.status_code} {r.text[:200]}")
            continue
        x = r.json()[0]
        ok = x["disposition"] in want and (rule is None or x["rule"] == rule)
        check(name, ok, f"{x['disposition']} ({x['rule']}) in {time.time() - started:.1f}s - AI calls {x['ai_calls']}, "
                        f"AI {x['ai_seconds']}s, rate-limit wait {x['waited_seconds']}s, cost ${x['cost_usd']:.4f}")

    print("4. Security and errors")
    r = http.post("/match/groq", json=alert | {"ClientID": "SOMEONE-ELSE"})
    check("another client's ID refused", r.status_code == 403 and r.json()["error_code"] == "CLIENT_MISMATCH")
    r = http.post("/match/groq", json={k: v for k, v in alert.items() if k != "ClientID"})
    check("missing ClientID refused", r.status_code == 400 and r.json()["error_code"] == "MISSING_CLIENT_ID")
    r = httpx.get(a.base_url.rstrip("/") + f"/whitelist/{cid}/SMOKE-WL-1", headers={"X-API-Key": "cls_wrong"},
                  verify=not a.insecure)
    check("wrong key refused", r.status_code == 401 and r.json()["error_code"] == "INVALID_API_KEY")
    if a.other_key_env and os.environ.get(a.other_key_env):
        r = httpx.post(a.base_url.rstrip("/") + "/match/groq", json=alert | {"Unique_ID": "SMOKE-A4"},
                       headers={"X-API-Key": os.environ[a.other_key_env]}, timeout=120, verify=not a.insecure)
        check("another client's key cannot use this client's records", r.status_code == 403)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed.")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
