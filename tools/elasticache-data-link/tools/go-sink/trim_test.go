package main

import (
	"context"
	"reflect"
	"testing"

	"github.com/redis/go-redis/v9"
)

func TestSplitList(t *testing.T) {
	cases := map[string][]string{
		"":                            nil,
		" , ,":                        nil,
		"a":                           {"a"},
		" a , b ,, c ":                {"a", "b", "c"},
		"data:__debezium-heartbeat.x": {"data:__debezium-heartbeat.x"},
	}
	for in, want := range cases {
		if got := splitList(in); !reflect.DeepEqual(got, want) {
			t.Errorf("splitList(%q) = %v, want %v", in, got, want)
		}
	}
}

func TestIntegrationTrimCapsAStreamAndToleratesAMissingOne(t *testing.T) {
	c, ctx := testValkey(t), context.Background()
	key := "itest:data:__debezium-heartbeat.trim"
	c.Del(ctx, key)
	for i := 0; i < 5000; i++ {
		c.XAdd(ctx, &redis.XAddArgs{Stream: key, Values: map[string]interface{}{"v": i}})
	}
	trimOnce(ctx, c, []string{key, "itest:does-not-exist"}, 100)
	n, err := c.XLen(ctx, key).Result()
	if err != nil {
		t.Fatal(err)
	}
	// Approximate trimming keeps at least maxLen and drops whole nodes, so the
	// length ends up near, not exactly at, the cap -- but far below 5000.
	if n < 100 || n > 1000 {
		t.Fatalf("after trim: %d entries, want about 100", n)
	}
	c.Del(ctx, key)
}
