"""Builds the quick Swagger test cases (made-up people) and checks each one's match score with
ClearScreen's own scoring, so every case takes the intended path."""
import json, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "app"))
from clearscreen import db
from clearscreen.matching import as_case
from clearscreen.scoring.comparison import build_comparison_trace, MATCH_THRESHOLD
from clearscreen.scoring.case import parse_case

SEP = "-" * 10
def listing(name, ref, notes, street, city, country, dob, nationality, extra=""):
    return (f"Entity Name: {name} Reference No.:{ref} Notes:{notes} Address Information {SEP} "
            f"Street 1: {street} City: {city}" + (f" Country: {country}" if country else "")
            + " "
            f"Additional Information {SEP} DOB: {dob} {SEP} Nationality: {nationality}{extra}")

L1 = listing("Thaddeus Quillfeather-Moreau", "99000101",
             "Named in a 2024 procurement fraud inquiry.",
             "14 Rue des Tanneurs", "Port-Gentil", None, "1951-06-02", "Gabon")
L2 = listing("Ysolde Brannigan-Ferreira", "99000102", "2023 sanctions designation.",
             "Avenida Litoral 220", "Mindelo", "Cabo Verde", "1947-01-15", "Cabo Verde")
L3 = listing("Casimir Oduya Lindqvist", "99000103", "2022 customs evasion report.",
             "Plot 9 Kisiwa Road", "Mombasa", "Kenya", "1960-09-30", "Kenya")
L4 = listing("Perpetua Arnwald Sokolova", "99000104", "PEP: customs official 2015-2021.",
             "ul. Portowa 3", "Gdynia", "Poland", "1958-12-11", "Poland")
LOW = listing("Bartholomew Vane", "99000105", "2021 counterfeit goods conviction.",
              "22 Quay Lane", "Hull", "United Kingdom", "1970-03-03", "United Kingdom")

def rec(uid, fields, values, details, reason, db_="WorldCompliance Data Plus.BDF2", date="11/12/2026 9:00:00 AM"):
    return {"ClientID": "001", "Unique_ID": uid, "Screened_Fields": fields, "Screened_Values": values,
            "Database_Screened": db_, "Screened_Date": date, "Matched_Details": details, "Analyst_Allow_Reasoning": reason}

F = "Name,Street Address,Country,City,Post Code,Province"
single = rec("TEST-WL-0001", F, "THADDEUS QUILLFEATHER-MOREAU,88 MAPLEHURST CRESCENT,CANADA,HALIFAX,B3H 2Y4,PORT AURORA", L1,
             "Customer lives in Halifax, Canada. Listed person is a Gabon national in Port-Gentil. Country and city differ.")
batch = [
    rec("TEST-WL-0002", F, "YSOLDE BRANNIGAN-FERREIRA,412 CEDARVALE AVE,CANADA,TORONTO,M6C 3A1,ONTARIO", L2,
        "Customer born 1992, Canadian. Listed person born 1947, Cabo Verde national. Not a match."),
    rec("TEST-WL-0003", F, "CASIMIR ODUYA LINDQVIST,9 BIRCHGROVE LANE,CANADA,CALGARY,T2N 1N4,ALBERTA", L3,
        "Customer born 1985, lives in Calgary. Listed person born 1960, Kenyan national. Not a match."),
    rec("TEST-WL-0004", F, "PERPETUA ARNWALD SOKOLOVA,1550 RUE SAINT-DENIS,CANADA,MONTREAL,H2X 3K2,QUEBEC", L4,
        "Customer born 1999, student in Montreal. Listed person born 1958, Polish official. Not a match."),
]
def alert(r, **change):
    a = {k: v for k, v in r.items() if k != "Analyst_Allow_Reasoning"} | change
    return a

python_one = alert(batch[0], Unique_ID="TEST-ALERT-PY-01")                       # exact repeat of TEST-WL-0002
ai_one = alert(single, Unique_ID="TEST-ALERT-AI-01", Screened_Date="11/12/2026 3:30:00 PM",
               Screened_Values="THADDEUS QUILLFEATHER-MOREAU,88 MAPLEHURST CRESCENT UNIT 4,CANADA,HALIFAX,B3H 2Y4,PORT AURORA")
python_batch = [
    alert(batch[1], Unique_ID="TEST-ALERT-PY-02"),                                   # exact repeat of 0003
    alert(batch[2], Unique_ID="TEST-ALERT-PY-03"),                                   # exact repeat of 0004
    {"ClientID": "001", "Unique_ID": "TEST-ALERT-PY-04", "Screened_Fields": F,       # low score: different person
     "Screened_Values": "BARTLEY VANCE-OYELARAN,73 HARBOURVIEW DR,CANADA,VICTORIA,V8V 1A1,BRITISH COLUMBIA",
     "Database_Screened": "WorldCompliance Data Plus.BDF2", "Screened_Date": "11/12/2026 3:35:00 PM", "Matched_Details": LOW},
    {"ClientID": "001", "Unique_ID": "TEST-ALERT-PY-05", "Screened_Fields": F,       # no listing details
     "Screened_Values": "ODALYS FENWICK-ARAUJO,5 WILLOW CT,CANADA,REGINA,S4P 3Y2,SASKATCHEWAN",
     "Database_Screened": "OSFI COUNTRY.CDF", "Screened_Date": "11/12/2026 3:36:00 PM", "Matched_Details": "Entity Details NOT AVAILABLE"},
]

def score(a):
    al = db.to_alert(a)
    if db.no_listing_details(al["Matched_Payload"]):
        return None
    c = as_case(al)
    return round(build_comparison_trace(c, parse_case(c)).deterministic_score, 2)

for a in [ai_one, *python_batch]:
    print(a["Unique_ID"], "score", score(a), "threshold", MATCH_THRESHOLD)

out = {"1_single_add": [single], "2_batch_add": batch, "4_python_match": [python_one],
       "5_ai_match": [ai_one], "6_python_batch": python_batch}
for k, v in out.items():
    json.dump(v, open(f"{k}.json", "w", encoding="utf-8"), indent=2, ensure_ascii=False)
print("written")
