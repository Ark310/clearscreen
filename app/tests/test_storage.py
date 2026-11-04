"""SQL Server storage, the secure store and keys: records replaced by ClientID + Unique_ID,
exact repeats found by fingerprint, embeddings rebuilt when a listing changes, matrix similarity
equal to row-by-row, DPAPI round trip, hashed keys."""
import pytest

from clearscreen import db, similarity
from clearscreen.config import SecureStore, protect, unprotect
from clearscreen.security import add_client, client_for_key, hash_key, remove_client, reset_key, set_enabled
from tests.conftest import fake_embed

LISTING = ("Entity Name: ASIA SUN TRADING CO. LTD. Reference No.:41536 Reason: BURMA-EO14014 Address Information "
           "---------- Address Type: Unknown City: Bahan Country: Burma AKA Listing ---------- Full: ASIA SUN TRADING")
OTHER = "Name: Jon Ewyler, Reference No.:12345 Address Information City: Pico Country: Panama"
REC = dict(ClientID="C1", Unique_ID="1", Screened_Fields="Beneficiary Bank Name,Beneficiary Bank Country",
           Screened_Values="Asia Trading,Canada", Database_Screened="OFAC SDN.BDF", Screened_Date="1/1/2026",
           Matched_Details=LISTING, Analyst_Allow_Reasoning="Different country.")
CFG = similarity.Gateway2Config()


def test_add_then_replace_by_client_and_unique_id(sql):
    assert db.upsert_many(sql, [REC, REC | {"Unique_ID": "2"}]) == ["added", "added"]
    sql.commit()
    assert db.upsert_many(sql, [REC | {"Analyst_Allow_Reasoning": "corrected"}]) == ["replaced"]
    sql.commit()
    assert db.get_record(sql, "C1", "1")["Analyst_Allow_Reasoning"] == "corrected"
    assert db.count_by_client(sql) == {"C1": 2}


def test_same_pair_twice_in_one_request_keeps_the_later(sql):
    assert db.upsert_many(sql, [REC, REC | {"Analyst_Allow_Reasoning": "later"}]) == ["added", "replaced"]
    sql.commit()
    assert db.get_record(sql, "C1", "1")["Analyst_Allow_Reasoning"] == "later"


def test_exact_repeat_ignores_case_and_spacing_and_stays_in_client(sql):
    db.upsert_many(sql, [REC, REC | {"ClientID": "C2", "Unique_ID": "9"}])
    sql.commit()
    assert db.exact_repeats(sql, "C1", REC["Screened_Fields"], " asia  TRADING , canada ", LISTING) == ["1"]
    assert db.exact_repeats(sql, "C1", REC["Screened_Fields"], "Asia Trading,Peru", LISTING) == []
    assert db.exact_repeats(sql, "C3", REC["Screened_Fields"], REC["Screened_Values"], LISTING) == []


def test_embeddings_built_and_rebuilt_when_listing_changes(sql):
    db.upsert_many(sql, [REC])
    sql.commit()
    assert similarity.pending_embeddings(sql, CFG, "C1") == 1
    assert similarity.embed_missing(sql, fake_embed, CFG, "C1") == 1
    assert similarity.pending_embeddings(sql, CFG, "C1") == 0
    db.upsert_many(sql, [REC | {"Matched_Details": LISTING + " Notes: updated"}])
    sql.commit()
    assert similarity.pending_embeddings(sql, CFG, "C1") == 1          # cache dropped for the new listing
    db.upsert_many(sql, [REC | {"Matched_Details": LISTING + " Notes: updated", "Analyst_Allow_Reasoning": "x"}])
    sql.commit()


def test_distinct_chunks_embedded_once(sql):
    db.upsert_many(sql, [REC | {"Unique_ID": str(i)} for i in range(5)])
    sql.commit()
    calls = []

    def embed(texts):
        calls.extend(texts)
        return fake_embed(texts)
    similarity.embed_missing(sql, embed, CFG, "C1")
    assert len(calls) == len(similarity.chunk_payload(LISTING))


def test_matrix_scores_equal_row_by_row(sql):
    payloads = (LISTING, OTHER, OTHER + " Notes: extra text here", LISTING.replace("Bahan", "Yangon"))
    db.upsert_many(sql, [REC | {"Unique_ID": str(i), "Matched_Details": p} for i, p in enumerate(payloads)])
    sql.commit()
    similarity.embed_missing(sql, fake_embed, CFG, "C1")
    index = similarity.client_index(sql, CFG, "C1")
    current = similarity._unit(fake_embed(similarity.chunk_payload(LISTING)))
    fast = similarity.payload_similarities(current, index)
    cur = sql.cursor()
    for key, got in zip(index.wl_ids, fast):
        payload = cur.execute("SELECT Matched_Details FROM dbo.WL_Records WHERE RecordKey = ?", int(key)).fetchone()[0]
        want = similarity.payload_similarity(current, similarity._unit(fake_embed(similarity.chunk_payload(payload))))
        assert got == pytest.approx(want, abs=0.01)


def test_other_clients_never_in_the_index(sql):
    db.upsert_many(sql, [REC, REC | {"ClientID": "C2", "Unique_ID": "x"}])
    sql.commit()
    similarity.embed_missing(sql, fake_embed, CFG)
    assert len(similarity.client_index(sql, CFG, "C1").wl_ids) == 1
    alert = db.to_alert(REC | {"Unique_ID": "new"})
    found = similarity.find_candidates(alert, sql, CFG, embed=fake_embed)
    assert found["compared"] == 1 and [c["Source_ID"] for c in found["candidates"]] == ["1"]


# ------------------------------------------------------------- secure store ---
def test_dpapi_round_trip_and_file_is_not_plain_text(tmp_path):
    blob = protect(b'{"groq_api_key": "gsk_test_value"}')
    assert b"gsk_test_value" not in blob and unprotect(blob) == b'{"groq_api_key": "gsk_test_value"}'
    store = SecureStore(tmp_path / "secure.dat")
    store.update(lambda d: d.update(groq_api_key="gsk_test_value"))
    assert b"gsk_test_value" not in (tmp_path / "secure.dat").read_bytes()
    assert SecureStore(tmp_path / "secure.dat").get("groq_api_key") == "gsk_test_value"


def test_client_keys_hashed_reset_disable_remove(tmp_path):
    store = SecureStore(tmp_path / "secure.dat")
    key = add_client(store, "ACME-1", "Acme Bank")
    raw = (tmp_path / "secure.dat").read_bytes()
    assert key.encode() not in unprotect(raw) and hash_key(key).encode() in unprotect(raw)
    assert client_for_key(store, key)[0] == "ACME-1"
    new = reset_key(store, "ACME-1")
    assert client_for_key(store, key) == (None, None) and client_for_key(store, new)[0] == "ACME-1"
    set_enabled(store, "ACME-1", False)
    assert client_for_key(store, new)[1]["enabled"] is False
    with pytest.raises(ValueError):
        add_client(store, "acme-1", "Duplicate (case-insensitive)")
    with pytest.raises(ValueError):
        add_client(store, "bad id!", "Bad")
    remove_client(store, "ACME-1")
    assert client_for_key(store, new) == (None, None)
