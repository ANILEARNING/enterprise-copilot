"""Mirrors tests/test_storage.py's SessionStore coverage — same file-backed-
store pattern (app/document_store.py), same test shape."""
import app.document_store as document_store_module
from app.document_store import DocumentStore


def test_default_data_dir_is_used_when_none_given(tmp_path, monkeypatch):
    monkeypatch.setattr(document_store_module, "DEFAULT_DATA_DIR", tmp_path / "default-documents")
    store = DocumentStore()
    doc = store.create("a.md", "hello", tenant_id="default")
    assert (tmp_path / "default-documents" / f"{doc['document_id']}.json").is_file()


def test_create_writes_a_file(tmp_path):
    store = DocumentStore(data_dir=tmp_path)
    doc = store.create("pricing.md", "The plan costs 100.", tenant_id="default")
    assert (tmp_path / f"{doc['document_id']}.json").is_file()
    assert doc["filename"] == "pricing.md"
    assert doc["content"] == "The plan costs 100."
    assert doc["status"] == "indexed"
    assert doc["tenant_id"] == "default"
    assert doc["chunk_hashes"] == {}
    assert doc["size"] == len("The plan costs 100.".encode("utf-8"))


def test_create_with_explicit_document_id(tmp_path):
    store = DocumentStore(data_dir=tmp_path)
    doc = store.create("a.md", "x", tenant_id="default", document_id="fixed-id")
    assert doc["document_id"] == "fixed-id"
    assert (tmp_path / "fixed-id.json").is_file()


def test_update_persists_across_new_store_instances(tmp_path):
    store = DocumentStore(data_dir=tmp_path)
    doc = store.create("a.md", "original", tenant_id="default")
    store.update(doc["document_id"], filename="renamed.md", content="updated content")

    reloaded_store = DocumentStore(data_dir=tmp_path)
    reloaded = reloaded_store.get(doc["document_id"])
    assert reloaded["filename"] == "renamed.md"
    assert reloaded["content"] == "updated content"
    assert reloaded["size"] == len("updated content".encode("utf-8"))


def test_create_and_update_persist_blocks(tmp_path):
    store = DocumentStore(data_dir=tmp_path)
    blocks = [{"kind": "heading", "text": "Title", "level": 1}, {"kind": "paragraph", "text": "Body"}]
    doc = store.create("a.md", "content", tenant_id="default", blocks=blocks)
    assert doc["blocks"] == blocks

    reloaded = DocumentStore(data_dir=tmp_path).get(doc["document_id"])
    assert reloaded["blocks"] == blocks

    new_blocks = [{"kind": "paragraph", "text": "New body"}]
    store.update(doc["document_id"], None, "new content", blocks=new_blocks)
    assert store.get(doc["document_id"])["blocks"] == new_blocks


def test_create_without_blocks_defaults_to_none(tmp_path):
    store = DocumentStore(data_dir=tmp_path)
    doc = store.create("a.md", "content", tenant_id="default")
    assert doc["blocks"] is None


def test_update_without_new_content_keeps_existing_blocks(tmp_path):
    store = DocumentStore(data_dir=tmp_path)
    blocks = [{"kind": "paragraph", "text": "Body"}]
    doc = store.create("a.md", "content", tenant_id="default", blocks=blocks)
    store.update(doc["document_id"], "renamed.md", None)  # filename-only rename, no content
    assert store.get(doc["document_id"])["blocks"] == blocks


def test_update_partial_leaves_unspecified_fields_unchanged(tmp_path):
    store = DocumentStore(data_dir=tmp_path)
    doc = store.create("a.md", "original", tenant_id="default")
    store.update(doc["document_id"], filename=None, content="new content only")
    reloaded = store.get(doc["document_id"])
    assert reloaded["filename"] == "a.md"  # unchanged
    assert reloaded["content"] == "new content only"


def test_update_unknown_document_raises_keyerror(tmp_path):
    store = DocumentStore(data_dir=tmp_path)
    try:
        store.update("does-not-exist", "a.md", "x")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_get_unknown_document_raises_keyerror(tmp_path):
    store = DocumentStore(data_dir=tmp_path)
    try:
        store.get("nope")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_set_chunk_hashes_persists_and_replaces_wholesale(tmp_path):
    store = DocumentStore(data_dir=tmp_path)
    doc = store.create("a.md", "text", tenant_id="default")
    store.set_chunk_hashes(doc["document_id"], {"c1": "hash1", "c2": "hash2"})
    reloaded = DocumentStore(data_dir=tmp_path).get(doc["document_id"])
    assert reloaded["chunk_hashes"] == {"c1": "hash1", "c2": "hash2"}

    # a second call REPLACES, not merges — proves stale entries can't survive a reindex
    store.set_chunk_hashes(doc["document_id"], {"c3": "hash3"})
    reloaded2 = store.get(doc["document_id"])
    assert reloaded2["chunk_hashes"] == {"c3": "hash3"}


def test_set_chunk_hashes_on_unknown_document_is_a_no_op(tmp_path):
    store = DocumentStore(data_dir=tmp_path)
    store.set_chunk_hashes("does-not-exist", {"c1": "h1"})  # must not raise
    assert not (tmp_path / "does-not-exist.json").exists()


def test_delete_removes_the_file(tmp_path):
    store = DocumentStore(data_dir=tmp_path)
    doc = store.create("a.md", "text", tenant_id="default")
    store.delete(doc["document_id"])
    assert not (tmp_path / f"{doc['document_id']}.json").exists()
    try:
        store.get(doc["document_id"])
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_delete_unknown_document_raises_keyerror(tmp_path):
    store = DocumentStore(data_dir=tmp_path)
    try:
        store.delete("does-not-exist")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_list_returns_newest_first(tmp_path):
    store = DocumentStore(data_dir=tmp_path)
    a = store.create("a.md", "first", tenant_id="default")
    store.update(a["document_id"], None, "first updated")  # bump updated_at
    b = store.create("b.md", "second", tenant_id="default")

    listed = store.list()
    ids = [d["document_id"] for d in listed]
    assert ids[0] == b["document_id"]  # most recently created/updated first


def test_cache_avoids_rereading_but_write_still_hits_disk(tmp_path):
    store = DocumentStore(data_dir=tmp_path)
    doc = store.create("a.md", "text", tenant_id="default")
    store.update(doc["document_id"], None, "cached content")
    again = store.get(doc["document_id"])
    assert again is store._cache[doc["document_id"]]
    on_disk = DocumentStore(data_dir=tmp_path).get(doc["document_id"])
    assert on_disk["content"] == "cached content"
