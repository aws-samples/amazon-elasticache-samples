// Command blis_l1 replays a routed Mooncake request stream through BLIS's per-instance
// KV cache (sim/kv: vLLM-like block prefix cache, optional vLLM-native CPU offload
// tier) and writes each request's GPU and CPU prefix-hit tokens.
//
// It drives the cache directly, one request at a time: lookup, allocate, mirror to
// CPU, release. There is no scheduler, batching or decode, so blocks of earlier
// requests are never pinned while a later one is served (the same simplification as
// synth_from_mooncake.py). BLIS's TraceV2 format carries one prefix group per
// request and cannot express Mooncake's prefix tree, which is why the DES is not used.
//
// Input (JSON lines, in serving order): {"line", "node", "input_length", "hash_ids"}.
// Token ids are derived from (hash id, offset), so equal hash ids give equal blocks.
//
// Build inside a module that requires github.com/inference-sim/inference-sim (a
// replace directive to a local clone works); see examples/xcheck/README.md.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sort"

	"github.com/inference-sim/inference-sim/sim"
	"github.com/inference-sim/inference-sim/sim/kv"
)

type request struct {
	Line        int     `json:"line"`
	Node        string  `json:"node"`
	InputLength int     `json:"input_length"`
	HashIDs     []int64 `json:"hash_ids"`
}

type outcome struct {
	Line         int    `json:"line"`
	Node         string `json:"node"`
	InputTokens  int    `json:"input_tokens"`
	GPUHitTokens int64  `json:"gpu_hit_tokens"`
	CPUHitTokens int64  `json:"cpu_hit_tokens"`
	AllocOK      bool   `json:"alloc_ok"`
}

type cache interface {
	GetCachedBlocks(tokens []sim.TokenID) []int64
	AllocateKVBlocks(req *sim.Request, startIndex, endIndex int64, cachedBlocks []int64) bool
	ReleaseKVBlocks(req *sim.Request)
}

const traceBlock = 512

func tokens(r request) []sim.TokenID {
	out := make([]sim.TokenID, r.InputLength)
	for i := range out {
		b := i / traceBlock
		if b < len(r.HashIDs) {
			out[i] = sim.TokenID(r.HashIDs[b]*traceBlock + int64(i%traceBlock))
		} else {
			// Past the hash ids: request-unique tokens (negative, never equal to a block).
			out[i] = sim.TokenID(-(int64(r.Line)*1_000_003 + int64(i) + 1))
		}
	}
	return out
}

func main() {
	in := flag.String("in", "", "routed requests (JSON lines)")
	outPath := flag.String("out", "", "per-request outcomes (JSON lines)")
	gpuTokens := flag.Int64("gpu-tokens", 0, "GPU KV pool per instance, tokens")
	block := flag.Int64("block", 16, "GPU block size, tokens")
	cpuBytes := flag.Int64("cpu-bytes", 0, "vLLM-native CPU offload tier per instance, bytes (0 = off)")
	bytesPerToken := flag.Int64("kv-bytes-per-token", 192512, "KV bytes per token (CPU block size)")
	flag.Parse()
	if *in == "" || *outPath == "" || *gpuTokens < *block {
		fmt.Fprintln(os.Stderr, "need -in, -out and -gpu-tokens >= -block")
		os.Exit(2)
	}
	fh, err := os.Open(*in)
	if err != nil {
		panic(err)
	}
	defer fh.Close()
	of, err := os.Create(*outPath)
	if err != nil {
		panic(err)
	}
	defer of.Close()
	w := bufio.NewWriter(of)
	defer w.Flush()

	caches := map[string]cache{}
	newCache := func() cache {
		gpu := kv.NewKVCacheState(*gpuTokens / *block, *block)
		if *cpuBytes == 0 {
			return gpu
		}
		return kv.NewOffloadCache(gpu, sim.KVOffloadConfig{
			Enabled: true, CPUBytesToUse: *cpuBytes, PerBlockBytes: *block * *bytesPerToken,
			BlockSize: *block, BlocksPerChunk: 1, TokensPerHash: *block,
			EvictionPolicy: "lru", OffloadPromptOnly: true,
		})
	}
	sc := bufio.NewScanner(fh)
	sc.Buffer(make([]byte, 1<<20), 1<<28)
	stats := map[string][3]int64{}
	n := 0
	for sc.Scan() {
		var r request
		if err := json.Unmarshal(sc.Bytes(), &r); err != nil {
			panic(err)
		}
		c, ok := caches[r.Node]
		if !ok {
			c = newCache()
			caches[r.Node] = c
		}
		req := &sim.Request{ID: fmt.Sprintf("r%d", r.Line), InputTokens: tokens(r), State: sim.StateQueued}
		cached := c.GetCachedBlocks(req.InputTokens)
		gpuHit := int64(len(cached)) * *block
		cpuHit := int64(0)
		if oc, isOffload := c.(*kv.OffloadCache); isOffload {
			if end, more := oc.ReloadablePrefixEnd(req, gpuHit); more {
				cpuHit = end - gpuHit
			}
		}
		okAlloc := c.AllocateKVBlocks(req, gpuHit, req.InputLen(), cached)
		if oc, isOffload := c.(*kv.OffloadCache); isOffload && okAlloc {
			oc.MirrorToCPU([]*sim.Request{req})
		}
		c.ReleaseKVBlocks(req)
		if !okAlloc {
			gpuHit, cpuHit = 0, 0
		}
		o := outcome{r.Line, r.Node, r.InputLength, gpuHit, cpuHit, okAlloc}
		b, _ := json.Marshal(o)
		w.Write(b)
		w.WriteByte('\n')
		s := stats[r.Node]
		s[0] += int64(r.InputLength)
		s[1] += gpuHit
		s[2] += cpuHit
		stats[r.Node] = s
		n++
	}
	if err := sc.Err(); err != nil {
		panic(err)
	}
	nodes := make([]string, 0, len(stats))
	for k := range stats {
		nodes = append(nodes, k)
	}
	sort.Strings(nodes)
	for _, k := range nodes {
		s := stats[k]
		fmt.Printf("%s: input %d gpu_hit %.4f cpu_hit %.4f\n", k, s[0],
			float64(s[1])/float64(s[0]), float64(s[2])/float64(s[0]))
	}
	fmt.Printf("requests %d\n", n)
}
