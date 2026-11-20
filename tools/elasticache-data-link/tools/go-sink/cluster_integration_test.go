package main

// Cluster-mode behaviour against a real Valkey cluster. Skipped unless
// ECDL_TEST_VALKEY_CLUSTER names one node, e.g.
//
//	for p in 7001 7002 7003; do valkey-server --port $p --cluster-enabled yes \
//	  --cluster-config-file nodes-$p.conf --daemonize yes; done
//	valkey-cli --cluster create 127.0.0.1:7001 127.0.0.1:7002 127.0.0.1:7003 \
//	  --cluster-replicas 0 --cluster-yes
//	ECDL_TEST_VALKEY_CLUSTER=127.0.0.1:7001 go test -run Cluster ./...

import (
	"context"
	"fmt"
	"io"
	"os"
	"sync/atomic"
	"testing"

	"github.com/redis/go-redis/v9"
)

// nodeDown fails every pipeline sent to one node address.
type nodeDown struct {
	down  atomic.Bool
	addr  string
	calls atomic.Int64
}

func (h *nodeDown) DialHook(next redis.DialHook) redis.DialHook          { return next }
func (h *nodeDown) ProcessHook(next redis.ProcessHook) redis.ProcessHook { return next }
func (h *nodeDown) ProcessPipelineHook(next redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return func(ctx context.Context, cmds []redis.Cmder) error {
		if h.down.Load() {
			h.calls.Add(1)
			for _, c := range cmds {
				c.SetErr(io.EOF)
			}
			return io.EOF
		}
		return next(ctx, cmds)
	}
}

func TestIntegrationClusterOneShardDownDoesNotHoldTheOthers(t *testing.T) { // Y10
	addr := os.Getenv("ECDL_TEST_VALKEY_CLUSTER")
	if addr == "" {
		t.Skip("ECDL_TEST_VALKEY_CLUSTER not set")
	}
	ctx := context.Background()
	noSleep(t)
	hooks := map[string]*nodeDown{}
	var first string
	c := redis.NewClusterClient(&redis.ClusterOptions{Addrs: []string{addr}, MaxRetries: -1, MaxRedirects: 1,
		NewClient: func(o *redis.Options) *redis.Client {
			cl := redis.NewClient(o)
			h := &nodeDown{addr: o.Addr}
			hooks[o.Addr] = h
			cl.AddHook(h)
			return cl
		}})
	defer c.Close()
	if err := c.ForEachMaster(ctx, func(ctx context.Context, n *redis.Client) error { return n.FlushDB(ctx).Err() }); err != nil {
		t.Fatal(err)
	}
	if err := loadScripts(ctx, c); err != nil {
		t.Fatal(err)
	}
	// 30 keys spread over the three shards; take the node owning the first.
	var ops []targetOp
	owner := map[string]string{}
	for i := 0; i < 30; i++ {
		k := fmt.Sprintf("itest:c:%d", i)
		n, err := c.MasterForKey(ctx, k)
		if err != nil {
			t.Fatal(err)
		}
		owner[k] = n.Options().Addr
		if first == "" {
			first = owner[k]
		}
		ops = append(ops, targetOp{id: fmt.Sprintf("%d-0", 100+i), version: fmt.Sprintf("%d-0", 100+i), key: k,
			fields: map[string]interface{}{"v": "1"}})
	}
	hooks[first].down.Store(true)
	res := writeOps(ctx, c, ops)
	onDown, confirmedElsewhere := 0, 0
	for i, op := range ops {
		switch {
		case owner[op.key] == first:
			onDown++
			if res.cmds[i] != nil {
				t.Fatalf("%s on the failed shard was confirmed", op.key)
			}
			if !targetWide(res.errs[i]) {
				t.Fatalf("%s: error %v is not classed as target-wide", op.key, res.errs[i])
			}
		case res.cmds[i] != nil:
			confirmedElsewhere++
		default:
			t.Fatalf("%s on a healthy shard (%s) was not confirmed: %v", op.key, owner[op.key], res.errs[i])
		}
	}
	if onDown == 0 || confirmedElsewhere == 0 {
		t.Fatalf("keys not spread: on failed shard %d, elsewhere %d", onDown, confirmedElsewhere)
	}
	if got := len(res.held()); got != onDown {
		t.Fatalf("held %d, want the %d on the failed shard", got, onDown)
	}
	// The shard comes back: holding finishes the rest.
	hooks[first].down.Store(false)
	res = holdBatch(ctx, c, "itest", ops, res)
	if res.unconfirmed() != 0 {
		t.Fatalf("%d still unconfirmed after recovery", res.unconfirmed())
	}
}
