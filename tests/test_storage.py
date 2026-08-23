import app.storage as storage_module
from app.storage import SessionStore


def test_default_data_dir_is_used_when_none_given(tmp_path, monkeypatch):
    # Redirect the module default so this test doesn't write into the real repo's data/ dir.
    monkeypatch.setattr(storage_module, "DEFAULT_DATA_DIR", tmp_path / "default-sessions")
    store = SessionStore()
    session = store.create()
    assert (tmp_path / "default-sessions" / f"{session['session_id']}.json").is_file()


def test_create_writes_a_file(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    session = store.create()
    assert (tmp_path / f"{session['session_id']}.json").is_file()
    assert session["messages"] == []


def test_append_persists_across_new_store_instances(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    session = store.create()
    store.append(session["session_id"], "user", "hello")
    store.append(session["session_id"], "assistant", "hi there")

    # a fresh instance (simulating a process restart) must read it back from disk
    reloaded_store = SessionStore(data_dir=tmp_path)
    reloaded = reloaded_store.get(session["session_id"])
    assert [m["content"] for m in reloaded["messages"]] == ["hello", "hi there"]
    assert [m["role"] for m in reloaded["messages"]] == ["user", "assistant"]


def test_get_or_create_reuses_existing_session(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    session = store.create()
    store.append(session["session_id"], "user", "first turn")

    fetched = store.get_or_create(session["session_id"])
    assert fetched["session_id"] == session["session_id"]
    assert len(fetched["messages"]) == 1


def test_get_or_create_makes_a_new_session_for_unknown_id(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    fetched = store.get_or_create("does-not-exist")
    assert fetched["session_id"] != "does-not-exist"
    assert fetched["messages"] == []


def test_get_unknown_session_raises_keyerror(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    try:
        store.get("nope")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_list_returns_newest_first_with_preview(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    a = store.create()
    store.append(a["session_id"], "user", "first session question")
    b = store.create()
    store.append(b["session_id"], "user", "second session question")

    listed = store.list()
    ids = [s["session_id"] for s in listed]
    assert ids[0] == b["session_id"]  # most recently updated first
    assert ids[1] == a["session_id"]
    assert listed[0]["preview"] == "second session question"
    assert listed[0]["message_count"] == 1


def test_set_field_persists_arbitrary_state(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    session = store.create()
    store.set_field(session["session_id"], "pending_skill_run", {"run_id": "abc", "index": 0})

    reloaded = SessionStore(data_dir=tmp_path).get(session["session_id"])
    assert reloaded["pending_skill_run"] == {"run_id": "abc", "index": 0}


def test_set_field_on_unknown_session_is_a_no_op(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    store.set_field("does-not-exist", "pending_skill_run", {"x": 1})  # must not raise
    assert not (tmp_path / "does-not-exist.json").exists()


def test_append_to_unknown_session_is_a_no_op(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    store.append("does-not-exist", "user", "should not crash")  # must not raise
    assert not (tmp_path / "does-not-exist.json").exists()


def test_cache_avoids_rereading_but_write_still_hits_disk(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    session = store.create()
    store.append(session["session_id"], "user", "cached turn")
    # same instance, cache should serve this without re-parsing the file
    again = store.get(session["session_id"])
    assert again is store._cache[session["session_id"]]
    # but the file on disk must also reflect it (a restart would still see it)
    on_disk = SessionStore(data_dir=tmp_path).get(session["session_id"])
    assert on_disk["messages"][0]["content"] == "cached turn"


# --- Checkpointer: turn_checkpoint round-trip + manual named checkpoints ----

def test_turn_checkpoint_field_round_trips_via_set_field(tmp_path):
    # turn_checkpoint is just another arbitrary field from SessionStore's own
    # point of view — set_field() needs no special-casing for it.
    store = SessionStore(data_dir=tmp_path)
    session = store.create()
    checkpoint = {"turn_id": "t1", "stage": "sources_found", "hitl_request_id": None}
    store.set_field(session["session_id"], "turn_checkpoint", checkpoint)

    reloaded = SessionStore(data_dir=tmp_path).get(session["session_id"])
    assert reloaded["turn_checkpoint"] == checkpoint


def test_add_checkpoint_snapshots_message_count_and_memory_state(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    session = store.create()
    store.append(session["session_id"], "user", "hello")
    store.append(session["session_id"], "assistant", "hi there")
    store.set_field(session["session_id"], "memory_state", {"summary": "s", "summarized_count": 0})

    checkpoint = store.add_checkpoint(session["session_id"], "Before refactor discussion")
    assert checkpoint["label"] == "Before refactor discussion"
    assert checkpoint["message_count"] == 2
    assert checkpoint["memory_state"] == {"summary": "s", "summarized_count": 0}
    assert "checkpoint_id" in checkpoint and "created_at" in checkpoint


def test_add_checkpoint_on_unknown_session_is_a_no_op(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    result = store.add_checkpoint("does-not-exist", "label")
    assert result is None
    assert not (tmp_path / "does-not-exist.json").exists()


def test_list_checkpoints_returns_empty_list_for_session_with_none(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    session = store.create()
    assert store.list_checkpoints(session["session_id"]) == []


def test_list_checkpoints_unknown_session_raises_keyerror(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    try:
        store.list_checkpoints("nope")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_restore_checkpoint_truncates_messages_and_resets_memory_state(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    session = store.create()
    store.append(session["session_id"], "user", "hello")
    store.append(session["session_id"], "assistant", "hi there")
    store.set_field(session["session_id"], "memory_state", {"summary": "early", "summarized_count": 0})
    checkpoint = store.add_checkpoint(session["session_id"], "checkpoint A")

    store.append(session["session_id"], "user", "one more thing")
    store.append(session["session_id"], "assistant", "sure")
    store.set_field(session["session_id"], "memory_state", {"summary": "later", "summarized_count": 2})

    restored = store.restore_checkpoint(session["session_id"], checkpoint["checkpoint_id"])
    assert len(restored["messages"]) == 2
    assert [m["content"] for m in restored["messages"]] == ["hello", "hi there"]
    assert restored["memory_state"] == {"summary": "early", "summarized_count": 0}


def test_restore_checkpoint_clears_pending_skill_run_and_turn_checkpoint(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    session = store.create()
    checkpoint = store.add_checkpoint(session["session_id"], "checkpoint A")
    store.set_field(session["session_id"], "pending_skill_run", {"run_id": "r1"})
    store.set_field(session["session_id"], "turn_checkpoint", {"turn_id": "t1"})

    restored = store.restore_checkpoint(session["session_id"], checkpoint["checkpoint_id"])
    assert restored["pending_skill_run"] is None
    assert restored["turn_checkpoint"] is None


def test_restore_checkpoint_unknown_id_raises_keyerror(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    session = store.create()
    try:
        store.restore_checkpoint(session["session_id"], "does-not-exist")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_restore_checkpoint_unknown_session_raises_keyerror(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    try:
        store.restore_checkpoint("nope", "does-not-exist")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_restore_checkpoint_persists_across_new_store_instances(tmp_path):
    store = SessionStore(data_dir=tmp_path)
    session = store.create()
    store.append(session["session_id"], "user", "hello")
    checkpoint = store.add_checkpoint(session["session_id"], "checkpoint A")
    store.append(session["session_id"], "user", "forget this one")
    store.restore_checkpoint(session["session_id"], checkpoint["checkpoint_id"])

    # a fresh instance (simulating a process restart) must see the restored state
    reloaded_store = SessionStore(data_dir=tmp_path)
    reloaded = reloaded_store.get(session["session_id"])
    assert [m["content"] for m in reloaded["messages"]] == ["hello"]
