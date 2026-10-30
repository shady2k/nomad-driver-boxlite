package driver

import (
	"context"
	"github.com/hashicorp/nomad/client/lib/cpustats"
	cstructs "github.com/hashicorp/nomad/client/structs"
	"math"
	"nomad-driver-boxlite/internal/boxlite"
	"testing"
	"time"
)

func TestMeasuredUsageScopeAndCPU(t *testing.T) {
	cpu := 250.0
	rss := uint64(123456)
	now := time.Unix(100, 0)
	u := measuredUsage(&boxlite.Metrics{CPUPercent: &cpu, MemoryBytes: &rss}, cpustats.Compute{TotalCompute: 56000, NumCores: 14}, now)
	if u == nil || u.ResourceUsage.CpuStats.TotalTicks != 10000 || u.ResourceUsage.CpuStats.Percent != 250 || u.ResourceUsage.MemoryStats.RSS != rss || u.Timestamp != now.UnixNano() {
		t.Fatalf("unexpected measured usage %#v", u)
	}
	if u.ResourceUsage.MemoryStats.Usage != 0 || len(u.ResourceUsage.MemoryStats.Measured) != 1 {
		t.Fatal("fabricated guest memory")
	}
	for _, value := range []float64{-1, math.NaN(), math.Inf(1)} {
		if measuredUsage(&boxlite.Metrics{CPUPercent: &value, MemoryBytes: &rss}, cpustats.Compute{TotalCompute: 100, NumCores: 1}, now) != nil {
			t.Fatal("invalid CPU emitted")
		}
	}
	if measuredUsage(&boxlite.Metrics{}, cpustats.Compute{TotalCompute: 100, NumCores: 1}, now) != nil {
		t.Fatal("missing metrics emitted")
	}
	if measuredUsage(&boxlite.Metrics{CPUPercent: &cpu, MemoryBytes: &rss}, cpustats.Compute{}, now) != nil {
		t.Fatal("unknown compute fabricated")
	}
}

func TestTaskStatsOwnedStreamAndCancellation(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	d.compute = cpustats.Compute{TotalCompute: 56000, NumCores: 14}
	cfg := taskConfig(t)
	started(t, d, cfg)
	ctx, cancel := context.WithCancel(context.Background())
	ch, e := d.TaskStats(ctx, cfg.ID, 250*time.Millisecond)
	if e != nil {
		t.Fatal(e)
	}
	select {
	case sample := <-ch:
		if sample.ResourceUsage.MemoryStats.RSS != uint64('1')*1024 {
			t.Fatalf("wrong VM metrics: %#v", sample)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no measured sample")
	}
	cancel()
	select {
	case <-ch:
	case <-time.After(time.Second):
		t.Fatal("sampler did not cancel")
	}
	if _, e = d.TaskStats(ctx, "other-task", time.Second); e == nil {
		t.Fatal("unknown task leaked metrics")
	}
	f.mu.Lock()
	for _, request := range f.requests {
		if request == "GET /v1/boxes/vm2/metrics" {
			t.Fatal("cross-VM statistics")
		}
	}
	f.mu.Unlock()
}

func TestTaskStatsTwoVMIsolation(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	d.compute = cpustats.Compute{TotalCompute: 1000, NumCores: 1}
	a := taskConfig(t)
	started(t, d, a)
	b := taskConfig(t)
	b.ID = "task-b"
	b.AllocID = "allocation-b"
	b.JobID = "job-b"
	started(t, d, b)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	first, _ := d.TaskStats(ctx, a.ID, 250*time.Millisecond)
	second, _ := d.TaskStats(ctx, b.ID, 250*time.Millisecond)
	for i, ch := range []<-chan *cstructs.TaskResourceUsage{first, second} {
		select {
		case sample := <-ch:
			if sample.ResourceUsage.MemoryStats.RSS != uint64('1'+i)*1024 {
				t.Fatal("VM metrics mixed")
			}
		case <-time.After(3 * time.Second):
			t.Fatal("missing per-VM metrics")
		}
	}
}
