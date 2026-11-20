/*
 * Tests for EcdlStreamWriter. Plain Java (no test framework in the Debezium
 * image): run main(); a failure exits non-zero, which fails the image build.
 *
 * The fake broker behaves like Valkey: pipelined commands are applied one by one,
 * a refused command does not stop the ones after it, a dropped connection can
 * happen after some commands were applied, and WAIT reports replica acks.
 *
 * The property every test checks, whatever happened: for every record, its LAST
 * entry in the stream comes after the last entry of every record before it (so
 * each row's newest change is the one that stays), and nothing that is not in
 * the stream was marked processed.
 */
package io.debezium.server.redis;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedList;
import java.util.List;
import java.util.Map;

import io.debezium.server.redis.EcdlStreamWriter.ConnectionException;
import io.debezium.server.redis.EcdlStreamWriter.Entry;
import io.debezium.server.redis.EcdlStreamWriter.ServerIdentity;

public final class EcdlStreamWriterTest {

    private static int passed;
    private static final List<String> failures = new ArrayList<>();

    static final class FakeBroker implements EcdlStreamWriter.Broker {
        final List<String> stream = new ArrayList<>();
        final LinkedList<String> runIds = new LinkedList<>();
        final LinkedList<Boolean> primaries = new LinkedList<>();
        String runId = "run-A";
        boolean primary = true;
        final Map<Integer, Map<Integer, String>> refuse = new HashMap<>();
        final Map<Integer, Integer> dropAfter = new HashMap<>();
        final LinkedList<Long> waitReplies = new LinkedList<>();
        final Map<Integer, Boolean> waitDrops = new HashMap<>();
        int calls;
        int waits;
        int connects;
        boolean connected;

        @Override
        public ServerIdentity connect() {
            connects++;
            if (!runIds.isEmpty()) {
                runId = runIds.removeFirst();
            }
            if (!primaries.isEmpty()) {
                primary = primaries.removeFirst();
            }
            connected = true;
            return new ServerIdentity(runId, primary);
        }

        @Override
        public List<Object> addAll(List<Entry> entries) {
            if (!connected) {
                throw new ConnectionException("not connected", null);
            }
            calls++;
            Map<Integer, String> r = refuse.getOrDefault(calls, Map.of());
            Integer drop = dropAfter.get(calls);
            List<Object> out = new ArrayList<>();
            for (int i = 0; i < entries.size(); i++) {
                if (drop != null && i == drop) {
                    connected = false;
                    throw new ConnectionException("connection reset", null);
                }
                String err = r.get(i);
                if (err != null) {
                    out.add(new RuntimeException(err));
                    continue;
                }
                stream.add(entries.get(i).getValue().get("id"));
                out.add((1000 + stream.size()) + "-0");
            }
            return out;
        }

        @Override
        public long waitReplicas(int n, long timeout) {
            waits++;
            if (Boolean.TRUE.equals(waitDrops.get(waits))) {
                connected = false;
                throw new ConnectionException("connection reset during WAIT", null);
            }
            return waitReplies.isEmpty() ? 1 : waitReplies.removeFirst();
        }

        @Override
        public void close() {
            connected = false;
        }
    }

    static final class Run {
        final FakeBroker broker = new FakeBroker();
        final List<String> marked = new ArrayList<>();
        final List<Long> sleeps = new ArrayList<>();
        long now = 0;
        EcdlStreamWriter.Settings settings = new EcdlStreamWriter.Settings().retryDelays(100, 800).waitAttempts(3);
        Throwable thrown;

        void write(int n) {
            List<String> records = new ArrayList<>();
            List<Entry> entries = new ArrayList<>();
            for (int i = 1; i <= n; i++) {
                records.add("r" + i);
                entries.add(new Entry("data:t", Map.of("id", "r" + i)));
            }
            EcdlStreamWriter w = new EcdlStreamWriter(broker, settings, ms -> {
                sleeps.add(ms);
                now += ms;
            }, () -> now);
            try {
                w.start();
                w.write(records, entries, marked::add);
            }
            catch (Throwable t) {
                thrown = t;
            }
        }
    }

    static void check(String name, boolean cond, String detail) {
        if (cond) {
            passed++;
        }
        else {
            failures.add(name + ": " + detail);
        }
    }

    static void invariants(String name, Run run, int n) {
        Map<String, Integer> last = new HashMap<>();
        for (int i = 0; i < run.broker.stream.size(); i++) {
            last.put(run.broker.stream.get(i), i);
        }
        int prev = -1;
        boolean ordered = true;
        for (int i = 1; i <= n; i++) {
            Integer pos = last.get("r" + i);
            if (pos == null) {
                continue;
            }
            if (pos < prev) {
                ordered = false;
            }
            prev = pos;
        }
        check(name + ".newest-last", ordered, "stream " + run.broker.stream);
        boolean allIn = true;
        for (String m : run.marked) {
            allIn &= last.containsKey(m);
        }
        check(name + ".marked-in-stream", allIn, "marked " + run.marked + " stream " + run.broker.stream);
        boolean prefix = true;
        for (int i = 0; i < run.marked.size(); i++) {
            prefix &= run.marked.get(i).equals("r" + (i + 1));
        }
        check(name + ".marked-in-order", prefix, "marked " + run.marked);
    }

    static void normalBatch() {
        Run r = new Run();
        r.write(5);
        check("normal.no-error", r.thrown == null, String.valueOf(r.thrown));
        check("normal.all-marked", r.marked.size() == 5, "" + r.marked);
        check("normal.one-write", r.broker.calls == 1, "calls " + r.broker.calls);
        check("normal.waited", r.broker.waits == 1, "waits " + r.broker.waits);
        check("normal.no-duplicates", r.broker.stream.size() == 5, "" + r.broker.stream);
        invariants("normal", r, 5);
    }

    static void emptyBatch() {
        Run r = new Run();
        r.write(0);
        check("empty.no-error", r.thrown == null, String.valueOf(r.thrown));
        check("empty.no-write", r.broker.calls == 0 && r.broker.waits == 0, "calls " + r.broker.calls);
    }

    static void partialOomKeepsOrder() {
        Run r = new Run();
        r.broker.refuse.put(1, Map.of(2, "OOM command not allowed when used memory > 'maxmemory'."));
        r.write(5);
        check("partial-oom.no-error", r.thrown == null, String.valueOf(r.thrown));
        check("partial-oom.all-marked", r.marked.size() == 5, "" + r.marked);
        check("partial-oom.retried-once", r.broker.calls == 2, "calls " + r.broker.calls);
        check("partial-oom.slept", r.sleeps.equals(List.of(100L)), "" + r.sleeps);
        invariants("partial-oom", r, 5);
    }

    static void oomLastingThenClears() {
        Run r = new Run();
        for (int call = 1; call <= 5; call++) {
            r.broker.refuse.put(call, Map.of(0, "OOM command not allowed"));
        }
        r.write(3);
        check("oom-lasting.no-error", r.thrown == null, String.valueOf(r.thrown));
        check("oom-lasting.backoff-capped", r.sleeps.equals(List.of(100L, 200L, 400L, 800L, 800L)), "" + r.sleeps);
        check("oom-lasting.all-marked", r.marked.size() == 3, "" + r.marked);
        invariants("oom-lasting", r, 3);
    }

    static void transientRefusals() {
        for (String err : List.of("LOADING Valkey is loading", "BUSY script", "TRYAGAIN", "MASTERDOWN link down")) {
            Run r = new Run();
            r.broker.refuse.put(1, Map.of(1, err));
            r.write(3);
            String w = EcdlStreamWriter.firstWord(err);
            check("transient." + w, r.thrown == null && r.marked.size() == 3,
                    "thrown " + r.thrown + " marked " + r.marked);
            invariants("transient." + w, r, 3);
        }
    }

    static void permanentRefusalStops() {
        for (String err : List.of("WRONGTYPE Operation against a key holding the wrong kind of value",
                "NOPERM this user has no permissions", "ERR unknown command")) {
            Run r = new Run();
            r.broker.refuse.put(1, Map.of(2, err));
            r.write(5);
            String w = EcdlStreamWriter.firstWord(err);
            check("permanent." + w + ".stops", r.thrown != null, "no exception");
            check("permanent." + w + ".prefix-only", r.marked.equals(List.of("r1", "r2")), "" + r.marked);
            check("permanent." + w + ".no-retry", r.broker.calls == 1, "calls " + r.broker.calls);
            invariants("permanent." + w, r, 5);
        }
    }

    static void readonlyMeansFailover() {
        Run r = new Run();
        r.broker.refuse.put(1, Map.of(0, "READONLY You can't write against a read only replica."));
        r.write(4);
        check("readonly.stops", r.thrown != null && r.thrown.getMessage().contains("failed over"),
                String.valueOf(r.thrown));
        check("readonly.nothing-marked", r.marked.isEmpty(), "" + r.marked);
    }

    static void dropThenSamePrimaryContinues() {
        Run r = new Run();
        r.broker.dropAfter.put(1, 3);
        r.write(5);
        check("drop-same.no-error", r.thrown == null, String.valueOf(r.thrown));
        check("drop-same.all-marked", r.marked.size() == 5, "" + r.marked);
        check("drop-same.reconnected", r.broker.connects == 2, "connects " + r.broker.connects);
        invariants("drop-same", r, 5);
    }

    static void dropThenNewPrimaryStops() {
        Run r = new Run();
        r.broker.dropAfter.put(1, 2);
        r.broker.runIds.add("run-A");
        r.broker.runIds.add("run-B");
        r.write(5);
        check("drop-new.stops", r.thrown != null && r.thrown.getMessage().contains("different primary"),
                String.valueOf(r.thrown));
        check("drop-new.nothing-marked", r.marked.isEmpty(), "" + r.marked);
    }

    static void reconnectToReplicaStops() {
        Run r = new Run();
        r.broker.dropAfter.put(1, 0);
        r.broker.primaries.add(true);
        r.broker.primaries.add(false);
        r.write(3);
        check("reconnect-replica.stops", r.thrown != null && r.thrown.getMessage().contains("replica"),
                String.valueOf(r.thrown));
        check("reconnect-replica.nothing-marked", r.marked.isEmpty(), "" + r.marked);
    }

    static void startOnReplicaRefused() {
        Run r = new Run();
        r.broker.primary = false;
        r.write(2);
        check("start-replica.refused", r.thrown != null && r.thrown.getMessage().contains("replica"),
                String.valueOf(r.thrown));
        check("start-replica.no-write", r.broker.calls == 0, "calls " + r.broker.calls);
    }

    static void noReplicaAckStops() {
        Run r = new Run();
        r.broker.waitReplies.addAll(List.of(0L, 0L, 0L));
        r.write(3);
        check("no-ack.stops", r.thrown != null && r.thrown.getMessage().contains("no replica"),
                String.valueOf(r.thrown));
        check("no-ack.nothing-marked", r.marked.isEmpty(), "" + r.marked);
        check("no-ack.attempts", r.broker.waits == 3, "waits " + r.broker.waits);
    }

    static void lateReplicaAckMarks() {
        Run r = new Run();
        r.broker.waitReplies.addAll(List.of(0L, 0L, 1L));
        r.write(3);
        check("late-ack.no-error", r.thrown == null, String.valueOf(r.thrown));
        check("late-ack.all-marked", r.marked.size() == 3, "" + r.marked);
    }

    static void waitDisabledForSingleNode() {
        Run r = new Run();
        r.settings.waitForReplica(false);
        r.write(3);
        check("no-wait.no-error", r.thrown == null, String.valueOf(r.thrown));
        check("no-wait.never-waits", r.broker.waits == 0, "waits " + r.broker.waits);
        check("no-wait.all-marked", r.marked.size() == 3, "" + r.marked);
    }

    static void dropDuringWaitSamePrimary() {
        Run r = new Run();
        r.broker.waitDrops.put(1, true);
        r.write(3);
        check("wait-drop-same.no-error", r.thrown == null, String.valueOf(r.thrown));
        check("wait-drop-same.all-marked", r.marked.size() == 3, "" + r.marked);
        check("wait-drop-same.single-write", r.broker.calls == 1, "calls " + r.broker.calls);
    }

    static void dropDuringWaitNewPrimary() {
        Run r = new Run();
        r.broker.waitDrops.put(1, true);
        r.broker.runIds.add("run-A");
        r.broker.runIds.add("run-B");
        r.write(3);
        check("wait-drop-new.stops", r.thrown != null && r.thrown.getMessage().contains("different primary"),
                String.valueOf(r.thrown));
        check("wait-drop-new.nothing-marked", r.marked.isEmpty(), "" + r.marked);
    }

    static void repeatedPartialFailures() {
        Run r = new Run();
        r.broker.refuse.put(1, Map.of(3, "OOM"));
        r.broker.refuse.put(2, Map.of(1, "OOM"));
        r.broker.refuse.put(3, Map.of(0, "LOADING"));
        r.write(8);
        check("repeated.no-error", r.thrown == null, String.valueOf(r.thrown));
        check("repeated.all-marked", r.marked.size() == 8, "" + r.marked);
        invariants("repeated", r, 8);
    }

    static void classifiers() {
        check("id.ok", EcdlStreamWriter.isStreamId("1794882952638-0"), "");
        check("id.not-error", !EcdlStreamWriter.isStreamId("OOM command"), "");
        check("id.not-null", !EcdlStreamWriter.isStreamId(null), "");
        check("id.not-exception", !EcdlStreamWriter.isStreamId(new RuntimeException("1-1")), "");
        check("id.not-partial", !EcdlStreamWriter.isStreamId("12-"), "");
        check("transient.oom", EcdlStreamWriter.isTransient("OOM command not allowed"), "");
        check("transient.not-wrongtype", !EcdlStreamWriter.isTransient("WRONGTYPE x"), "");
        check("failover.readonly", EcdlStreamWriter.isFailover("READONLY You can't write"), "");
        check("failover.not-oom", !EcdlStreamWriter.isFailover("OOM"), "");
    }

    static void infoParsing() {
        String info = "# Server\r\nredis_version:7.2.4\r\nrun_id:abc123\r\n";
        check("info.run_id", "abc123".equals(JedisBroker.field(info, "run_id")), JedisBroker.field(info, "run_id"));
        check("info.missing", "".equals(JedisBroker.field(info, "role")), "");
    }

    public static void main(String[] args) {
        normalBatch();
        emptyBatch();
        partialOomKeepsOrder();
        oomLastingThenClears();
        transientRefusals();
        permanentRefusalStops();
        readonlyMeansFailover();
        dropThenSamePrimaryContinues();
        dropThenNewPrimaryStops();
        reconnectToReplicaStops();
        startOnReplicaRefused();
        noReplicaAckStops();
        lateReplicaAckMarks();
        waitDisabledForSingleNode();
        dropDuringWaitSamePrimary();
        dropDuringWaitNewPrimary();
        repeatedPartialFailures();
        classifiers();
        infoParsing();
        if (!failures.isEmpty()) {
            System.err.println("EcdlStreamWriterTest: " + failures.size() + " FAILED, " + passed + " passed");
            failures.forEach(f -> System.err.println("  FAIL " + f));
            System.exit(1);
        }
        System.out.println("EcdlStreamWriterTest: " + passed + " checks passed");
    }
}
