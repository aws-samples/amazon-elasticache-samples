import enum
import importlib.util
import json
import os
import pickle
import stat
import threading
from abc import ABC, abstractmethod
from types import SimpleNamespace

import pytest

from kvtrace import reqlog, reqtiers
from kvtrace.normalize import Anonymizer, Normalizer, normalize_hash
from kvtrace.reqlog import (
    RECORD_KIND,
    STATS_KIND,
    HookErrors,
    RequestLogWriter,
    RequestTracker,
    make_log_connector,
)
from kvtrace.wire import BlockStored, EventBatch


def read_lines(path):
    return [json.loads(line) for line in open(path, encoding="utf-8")]


def test_writer_appends_lines_and_a_stats_line(tmp_path):
    path = tmp_path / "sub" / "req.jsonl"
    w = RequestLogWriter(str(path))
    assert w.submit({"a": 1}) and w.submit({"a": 2})
    w.close()
    lines = read_lines(path)
    assert lines[:2] == [{"a": 1}, {"a": 2}]
    assert lines[2]["kind"] == STATS_KIND and lines[2]["written"] == 2 and lines[2]["dropped"] == 0
    assert not w.submit({"a": 3}) and w.dropped == 1


def test_full_queue_drops_instead_of_blocking(tmp_path):
    release = threading.Event()

    class Stalled(RequestLogWriter):
        def _run(self):
            release.wait(5)
            super()._run()

    w = Stalled(str(tmp_path / "req.jsonl"), queue_size=1)
    assert w.submit({"n": 0})
    assert not w.submit({"n": 1}) and not w.submit({"n": 2})
    assert w.dropped == 2
    release.set()
    w.close()
    lines = read_lines(tmp_path / "req.jsonl")
    assert lines[0] == {"n": 0} and lines[-1]["dropped"] == 2


def test_close_with_a_stuck_writer_leaves_the_file_to_the_thread(tmp_path):
    release = threading.Event()

    class Stuck(RequestLogWriter):
        def _run(self):
            release.wait(5)
            super()._run()

    w = Stuck(str(tmp_path / "req.jsonl"), queue_size=1)
    assert w.submit({"n": 0})
    w.close(timeout=0.05)  # queue full and thread busy: no stats line, file not closed
    release.set()
    w._thread.join(5)
    assert read_lines(tmp_path / "req.jsonl") == [{"n": 0}]


def req(rid, prompt=1000, hashes=(b"\x01" * 32, b"\xab\xcd" * 16), **kw):
    return SimpleNamespace(request_id=rid, num_prompt_tokens=prompt, arrival_time=5.0,
                           block_hashes=list(hashes), status=None, num_preemptions=0, **kw)


def sched(*new):
    return SimpleNamespace(scheduled_new_reqs=[SimpleNamespace(req_id=r, num_computed_tokens=c)
                                               for r, c in new])


def test_tracker_dedupes_repeated_lookups_and_emits_on_finish():
    out = []
    clock = iter([1.0, 2.0, 3.0, 4.0]).__next__
    t = RequestTracker(out.append, node="A1", clock=clock)
    r = req("r1")
    t.on_lookup(r, 256)          # first lookup at t=1
    t.on_lookup(r, 512)          # still waiting; L0 grew
    t.on_schedule(sched(("r1", 512 + 256), ("unknown", 9)))  # t=2; 256 external tokens
    t.on_schedule(sched(("r1", 900)))  # not a first schedule: ignored
    assert out == [] and t.pending == 1
    r.status = SimpleNamespace(name="FINISHED_LENGTH_CAPPED")
    t.on_finish(r)               # t=3
    t.on_finish(r)               # second finish: nothing
    (rec,) = out
    assert rec["kind"] == RECORD_KIND and rec["node"] == "A1" and rec["req_id"] == "r1"
    assert (rec["t_lookup"], rec["t_sched"], rec["t_finish"]) == (1.0, 2.0, 3.0)
    assert (rec["lookups"], rec["l0_tokens"], rec["external_tokens"]) == (2, 512, 256)
    assert rec["prompt_tokens"] == 1000 and rec["blocks"] == 2
    assert rec["last_block_hash"] == "abcdabcdabcdabcd"
    assert rec["status"] == "FINISHED_LENGTH_CAPPED" and t.pending == 0


def test_tracker_bounds_pending_requests():
    t = RequestTracker(lambda r: None, max_pending=2)
    for i in range(3):
        t.on_lookup(req(f"r{i}", hashes=()), 0)
    assert t.pending == 2 and t.evicted == 1


def test_tracker_prefers_vllm_prefill_stats_for_the_split():
    # vLLM kept a sub-block local tail: the lookup saw 512 block-aligned tokens, the
    # scheduler used 520 local tokens and loaded nothing external.
    out = []
    t = RequestTracker(out.append)
    r = req("r1", prefill_stats=SimpleNamespace(num_prompt_tokens=0))
    t.on_lookup(r, 512)
    # vLLM sets the fields of the same PrefillStats object at first schedule.
    r.prefill_stats.__dict__.update(num_prompt_tokens=1000, num_local_cached_tokens=520,
                                    num_external_cached_tokens=0)
    t.on_schedule(sched(("r1", 520)))
    t.on_finish(r)
    (rec,) = out
    assert (rec["l0_tokens"], rec["external_tokens"], rec["split"]) == (520, 0, "prefill_stats")


def test_tracker_keeps_no_request_objects():
    t = RequestTracker(lambda r: None)
    r = req("r1", prefill_stats=SimpleNamespace(num_prompt_tokens=0))
    t.on_lookup(r, 0)
    assert not any(v is r for v in vars(t).values() if not isinstance(v, dict))
    assert all(v is not r for d in vars(t).values() if isinstance(d, dict) for v in d.values())
    assert t._max_pending == 10_000


def test_hook_errors_are_counted_and_logged_at_a_bounded_rate(caplog):
    now = [0.0]
    errors = HookErrors(interval=60.0, clock=lambda: now[0])

    def boom():
        raise RuntimeError("bad request")

    with caplog.at_level("WARNING", logger="kvtrace.reqlog"):
        for _ in range(5):
            errors.run("hook", boom)
        now[0] = 61.0
        errors.run("hook", boom)
    assert errors.count == 6
    warnings = [r for r in caplog.records if "hook failed" in r.getMessage()]
    assert len(warnings) == 2 and "5 since the last warning" in warnings[1].getMessage()


# Stand-ins for vLLM's KVConnectorBase_V1 API (v0.30.0 signatures).
class Role(enum.Enum):
    SCHEDULER = 0
    WORKER = 1


class StubMeta(ABC):  # noqa: B024 - mirrors vLLM's KVConnectorMetadata
    pass


class StubHMA(ABC):
    @abstractmethod
    def request_finished_all_groups(self, request, block_ids):
        raise NotImplementedError


class StubBase(ABC):
    # Keeps no copy of kv_transfer_config: the connector must read the constructor argument.
    def __init__(self, vllm_config, role, kv_cache_config):
        self._role = role
        self.base_shutdown = False

    @abstractmethod
    def start_load_kv(self, forward_context, **kwargs): ...

    @abstractmethod
    def wait_for_layer_load(self, layer_name): ...

    @abstractmethod
    def save_kv_layer(self, layer_name, kv_layer, attn_metadata, **kwargs): ...

    @abstractmethod
    def wait_for_save(self): ...

    @abstractmethod
    def get_num_new_matched_tokens(self, request, num_computed_tokens): ...

    @abstractmethod
    def update_state_after_alloc(self, request, blocks, num_external_tokens): ...

    @abstractmethod
    def build_connector_meta(self, scheduler_output): ...

    def shutdown(self):
        self.base_shutdown = True


def config(path=None, **more):
    extra = {"path": path} if path else {}
    extra.update(more)
    return SimpleNamespace(kv_transfer_config=SimpleNamespace(kv_connector_extra_config=extra))


def test_connector_logs_one_line_per_request_and_never_claims_a_load(tmp_path):
    cls = make_log_connector(StubBase, StubMeta, Role.SCHEDULER, StubHMA)
    path = str(tmp_path / "req-{pid}.jsonl")
    conn = cls(config(path), Role.SCHEDULER, kv_cache_config=None)
    r = req("cmpl-1")
    assert conn.get_num_new_matched_tokens(r, 512) == (0, False)
    assert conn.get_num_new_matched_tokens(r, 512) == (0, False)
    conn.update_state_after_alloc(r, blocks=None, num_external_tokens=0)
    meta = conn.build_connector_meta(sched(("cmpl-1", 1024)))
    # vLLM pickles the scheduler output (with this metadata) to send it to TP workers.
    assert isinstance(pickle.loads(pickle.dumps(meta)), StubMeta)
    assert conn.request_finished_all_groups(r, ([1, 2],)) == (False, None)
    assert conn.requires_kv_delivery is False
    conn.shutdown()
    assert conn.base_shutdown
    (written,) = list(tmp_path.glob("req-*.jsonl"))
    assert "{pid}" not in written.name
    rec, stats = read_lines(written)
    assert rec["req_id"] == "cmpl-1" and rec["l0_tokens"] == 512 and rec["external_tokens"] == 512
    assert rec["lookups"] == 2 and stats["written"] == 1
    assert rec["split"] == "computed_minus_lookup"  # the stub request has no prefill_stats


def test_worker_role_needs_no_path_and_does_nothing(tmp_path, monkeypatch):
    cls = make_log_connector(StubBase, StubMeta, Role.SCHEDULER)
    conn = cls(config(), Role.WORKER, kv_cache_config=None)
    assert conn.start_load_kv(None) is None and conn.wait_for_save() is None
    assert conn.get_num_new_matched_tokens(req("w"), 0) == (0, False)
    conn.shutdown()
    # Without a path the scheduler writes to the per-process default path.
    monkeypatch.setattr(reqlog, "DEFAULT_PATH", str(tmp_path / "kvt" / "requests-{pid}.jsonl"))
    sched_conn = cls(config(), Role.SCHEDULER, kv_cache_config=None)
    sched_conn.shutdown()
    assert (tmp_path / "kvt" / f"requests-{os.getpid()}.jsonl").exists()


@pytest.mark.skipif(importlib.util.find_spec("vllm") is not None, reason="vllm is installed")
def test_vllm_connector_import_needs_vllm():
    with pytest.raises(ImportError, match="needs vllm"):
        import kvtrace.vllm_connector  # noqa: F401


class BrokenRequest:
    """A request whose attributes raise, as a vLLM change could make them."""

    @property
    def request_id(self):
        raise AttributeError("no request_id")


class BrokenOutput:
    @property
    def scheduled_new_reqs(self):
        raise TypeError("not iterable")


def test_connector_hooks_survive_broken_requests(tmp_path):
    cls = make_log_connector(StubBase, StubMeta, Role.SCHEDULER, StubHMA)
    conn = cls(config(str(tmp_path / "req-{pid}.jsonl")), Role.SCHEDULER, kv_cache_config=None)
    bad = BrokenRequest()
    assert conn.get_num_new_matched_tokens(bad, 0) == (0, False)
    assert isinstance(conn.build_connector_meta(BrokenOutput()), StubMeta)
    assert conn.request_finished(bad, []) == (False, None)
    assert conn.request_finished_all_groups(bad, ([],)) == (False, None)
    # A good request after the failures is still logged.
    r = req("ok")
    conn.get_num_new_matched_tokens(r, 0)
    conn.build_connector_meta(sched(("ok", 256)))
    conn.request_finished(r, [])
    conn.shutdown()
    rec, stats = read_lines(tmp_path / f"req-{os.getpid()}.jsonl")
    assert rec["req_id"] == "ok" and rec["external_tokens"] == 256
    assert stats["hook_errors"] == 4 and stats["evicted"] == 0


def test_writer_rejects_an_unusable_path_at_start(tmp_path):
    with pytest.raises(ValueError, match="is a directory"):
        RequestLogWriter(str(tmp_path))
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(ValueError, match="cannot open"):
        RequestLogWriter(str(blocker / "sub" / "req.jsonl"))


def test_connector_hmacs_req_id_and_block_hash_with_the_recorder_key(tmp_path):
    key_file = tmp_path / "key"
    key_file.write_bytes(b"shared-secret\n")
    cls = make_log_connector(StubBase, StubMeta, Role.SCHEDULER)
    conn = cls(config(str(tmp_path / "req-{pid}.jsonl"), anonymize_key_file=str(key_file)),
               Role.SCHEDULER, kv_cache_config=None)
    path = tmp_path / f"req-{os.getpid()}.jsonl"
    r = req("cmpl-secret-7")
    conn.get_num_new_matched_tokens(r, 16)
    conn.build_connector_meta(sched(("cmpl-secret-7", 16)))
    conn.request_finished(r, [])
    conn.shutdown()
    raw = path.read_text()
    assert "cmpl-secret-7" not in raw and "abcd" not in raw
    rec = read_lines(path)[0]
    # The same scheme and key as `kvtrace record --anonymize-key-file`.
    anon = Anonymizer(b"shared-secret")
    assert rec["req_id"] == anon("cmpl-secret-7")
    # vLLM's KV events carry the int of the low 64 bits by default.
    event_hash = int.from_bytes(b"\xab\xcd" * 16, "big") & ((1 << 64) - 1)
    assert rec["last_block_hash"] == anon(normalize_hash(event_hash))


def test_connector_without_key_keeps_raw_ids(tmp_path):
    tracker_out = []
    tracker = RequestTracker(tracker_out.append, int_block_hashes=True)
    tracker.on_lookup(req("plain-1"), 0)
    tracker.on_finish(req("plain-1"))
    assert tracker_out[0]["req_id"] == "plain-1"
    assert tracker_out[0]["last_block_hash"] == (b"\xab\xcd" * 16)[-8:].hex()


def _recorder_hash(event_hash, anonymizer):
    stored = BlockStored(block_hashes=[event_hash], parent_block_hash=None, num_tokens=16,
                         block_size=16)
    (rec,) = Normalizer("A1", "m", anonymizer).records(EventBatch(ts=1.0, events=[stored]), 0)
    return rec["h"][0]


@pytest.mark.parametrize("anonymizer", [None, Anonymizer(b"k")], ids=["raw", "hmac"])
@pytest.mark.parametrize("env", [None, "1", "0"], ids=["default", "int", "bytes"])
def test_connector_block_hash_matches_the_recorder_hash_of_the_kv_event(monkeypatch, env,
                                                                       anonymizer):
    # Distinct first and last 8 bytes: a wrong slice cannot match by chance.
    block = bytes(range(32))
    if env is None:
        monkeypatch.delenv(reqlog.INT_BLOCK_HASHES_ENV, raising=False)
    else:
        monkeypatch.setenv(reqlog.INT_BLOCK_HASHES_ENV, env)
    # The form vLLM's maybe_convert_block_hash puts in the KV event.
    event_hash = block if env == "0" else int.from_bytes(block, "big") & ((1 << 64) - 1)
    out = []
    t = RequestTracker(out.append, anonymizer=anonymizer)
    t.on_lookup(req("r1", hashes=(block,)), 0)
    t.on_finish(req("r1"))
    assert out[0]["last_block_hash"] == _recorder_hash(event_hash, anonymizer)


def test_connector_rejects_a_bad_key_file(tmp_path):
    cls = make_log_connector(StubBase, StubMeta, Role.SCHEDULER)
    with pytest.raises(ValueError, match="anonymize_key_file"):
        cls(config(str(tmp_path / "req.jsonl"), anonymize_key_file=str(tmp_path / "missing")),
            Role.SCHEDULER, kv_cache_config=None)


def test_writer_creates_an_owner_only_file_and_tightens_an_old_one(tmp_path):
    new = tmp_path / "new.jsonl"
    RequestLogWriter(str(new)).close()
    assert stat.S_IMODE(new.stat().st_mode) == 0o600
    old = tmp_path / "old.jsonl"
    old.write_text("")
    os.chmod(old, 0o644)
    RequestLogWriter(str(old)).close()
    assert stat.S_IMODE(old.stat().st_mode) == 0o600


def test_writer_rotates_by_size_and_keeps_backup_count(tmp_path):
    path = tmp_path / "req.jsonl"
    w = RequestLogWriter(str(path), max_bytes=100, backup_count=2)
    for n in range(40):
        w.submit({"n": n, "pad": "x" * 20})
    w.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["req.jsonl", "req.jsonl.1", "req.jsonl.2"]
    for name in ("req.jsonl.1", "req.jsonl.2"):
        assert (tmp_path / name).stat().st_size <= 100
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600
    # Oldest first: .2, .1, then the live file; the newest records survive in order.
    rows = [row for name in ("req.jsonl.2", "req.jsonl.1", "req.jsonl")
            for row in read_lines(tmp_path / name)]
    numbers = [row["n"] for row in rows if "n" in row]
    assert numbers == list(range(numbers[0], 40))
    assert rows[-1]["kind"] == STATS_KIND and rows[-1]["rotations"] == w.rotations > 2


def test_writer_with_zero_max_bytes_never_rotates(tmp_path):
    path = tmp_path / "req.jsonl"
    w = RequestLogWriter(str(path), max_bytes=0)
    for n in range(20):
        w.submit({"n": n, "pad": "x" * 50})
    w.close()
    assert [p.name for p in tmp_path.iterdir()] == ["req.jsonl"]


def test_writer_warns_when_the_file_mode_cannot_be_tightened(tmp_path, monkeypatch, caplog):
    path = tmp_path / "old.jsonl"
    path.write_text("")
    os.chmod(path, 0o644)

    def refuse(fd, mode):
        raise PermissionError("not the owner")

    monkeypatch.setattr(reqlog.os, "fchmod", refuse)
    with caplog.at_level("WARNING", logger="kvtrace.reqlog"):
        RequestLogWriter(str(path)).close()
    assert f"{path} has mode 0644, not 0600" in caplog.text


class StaleCopyBase(StubBase):
    """A base whose private copy holds another config, as a renamed or changed vLLM field could."""

    def __init__(self, vllm_config, role, kv_cache_config):
        super().__init__(vllm_config, role, kv_cache_config)
        self._kv_transfer_config = SimpleNamespace(kv_connector_extra_config={})


def _log_one(conn, rid="r1"):
    r = req(rid)
    conn.get_num_new_matched_tokens(r, 16)
    conn.build_connector_meta(sched((rid, 16)))
    conn.request_finished(r, [])


def test_connector_uses_the_child_config_that_multiconnector_passes(tmp_path):
    key_file = tmp_path / "key"
    key_file.write_bytes(b"k")
    # MultiConnector gives each child a copy of vllm_config whose kv_transfer_config is
    # built from that child's entry.
    child = {"kv_connector": "KVTraceLogConnector", "kv_role": "kv_both",
             "kv_connector_extra_config": {"path": str(tmp_path / "req-{pid}.jsonl"),
                                           "anonymize_key_file": str(key_file)}}
    vllm_config = SimpleNamespace(kv_transfer_config=SimpleNamespace(**child))
    cls = make_log_connector(StaleCopyBase, StubMeta, Role.SCHEDULER)
    conn = cls(vllm_config, Role.SCHEDULER, kv_cache_config=None)
    _log_one(conn, "cmpl-raw")
    conn.shutdown()
    path = tmp_path / f"req-{os.getpid()}.jsonl"
    assert "cmpl-raw" not in path.read_text()
    assert read_lines(path)[0]["req_id"] == Anonymizer(b"k")("cmpl-raw")


@pytest.mark.parametrize("vllm_config", [
    SimpleNamespace(),
    SimpleNamespace(kv_transfer_config=None),
    SimpleNamespace(kv_transfer_config=SimpleNamespace(extra_config={"path": "x"})),
    SimpleNamespace(kv_transfer_config=SimpleNamespace(kv_connector_extra_config="path=x")),
], ids=["no-kv-transfer-config", "none", "renamed-field", "not-a-mapping"])
def test_connector_refuses_to_start_without_its_config(tmp_path, monkeypatch, vllm_config):
    monkeypatch.setattr(reqlog, "DEFAULT_PATH", str(tmp_path / "requests-{pid}.jsonl"))
    cls = make_log_connector(StubBase, StubMeta, Role.SCHEDULER)
    with pytest.raises(ValueError, match="KVTraceLogConnector"):
        cls(vllm_config, Role.SCHEDULER, kv_cache_config=None)
    assert list(tmp_path.iterdir()) == []


def test_connector_refuses_an_empty_or_unreadable_key_file(tmp_path):
    cls = make_log_connector(StubBase, StubMeta, Role.SCHEDULER)
    empty = tmp_path / "empty"
    empty.write_bytes(b"\n")
    unreadable = tmp_path / "unreadable"
    unreadable.write_bytes(b"k")
    unreadable.chmod(0)
    cases = [str(empty), "", str(tmp_path)]
    if not os.access(unreadable, os.R_OK):  # root can read a mode 000 file
        cases.append(str(unreadable))
    for key_file in cases:
        with pytest.raises(ValueError, match="anonymize_key_file"):
            cls(config(str(tmp_path / "out" / "req-{pid}.jsonl"), anonymize_key_file=key_file),
                Role.SCHEDULER, kv_cache_config=None)
    # The key is checked before the log file is opened, so no raw log exists.
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("path,expected", [
    ("/d/requests.jsonl", "/d/requests-42.jsonl"),
    ("/d/requests", "/d/requests-42"),
    ("/d/requests-{pid}.jsonl", "/d/requests-42.jsonl"),
    ("/d/{pid}/r.jsonl", "/d/42/r.jsonl"),
])
def test_resolve_log_path_always_gives_each_process_its_own_file(path, expected):
    assert reqlog.resolve_log_path(path, 42) == expected


def test_connector_adds_the_pid_to_a_fixed_path_and_tiers_reads_its_rotations(tmp_path, caplog):
    cls = make_log_connector(StubBase, StubMeta, Role.SCHEDULER)
    conn = cls(config(str(tmp_path / "req.jsonl"), max_bytes=300, backup_count=10),
               Role.SCHEDULER, kv_cache_config=None)
    assert "has no {pid}" in caplog.text
    for n in range(6):
        _log_one(conn, f"r{n}")
    conn.shutdown()
    assert not (tmp_path / "req.jsonl").exists()
    # The shell glob of the documented `--requests .../req-*.jsonl` matches only the live
    # file; tiers adds its rotated siblings.
    live = sorted(str(p) for p in tmp_path.glob("req-*.jsonl"))
    assert live == [str(tmp_path / f"req-{os.getpid()}.jsonl")]
    assert (tmp_path / f"req-{os.getpid()}.jsonl.1").exists()
    _, engine, _ = reqtiers.load_rows(live)
    assert sorted(row["req_id"] for row in engine) == [f"r{n}" for n in range(6)]
