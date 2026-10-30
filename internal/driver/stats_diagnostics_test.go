package driver

import (
	"context"
	"github.com/hashicorp/nomad/client/lib/cpustats"
	"nomad-driver-boxlite/internal/boxlite"
	"strconv"
	"strings"
	"testing"
	"time"
)

func waitStatsAttr(t *testing.T, d *Driver, id, key, want string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		s, e := d.InspectTask(id)
		if e != nil {
			t.Fatal(e)
		}
		if s.DriverAttributes[key] == want {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	s, _ := d.InspectTask(id)
	t.Fatalf("attribute %s want %s got %s", key, want, s.DriverAttributes[key])
}
func TestTaskStatsGapsRemainMissingAndRecoveryMeasuredZero(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	d.compute = cpustats.Compute{TotalCompute: 1000, NumCores: 1}
	cfg := taskConfig(t)
	started(t, d, cfg)
	f.mu.Lock()
	f.metricsMode = "error"
	f.mu.Unlock()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	ch, e := d.TaskStats(ctx, cfg.ID, 250*time.Millisecond)
	if e != nil {
		t.Fatal(e)
	}
	waitStatsAttr(t, d, cfg.ID, "metrics_current_gap", "native_metrics_error")
	select {
	case <-ch:
		t.Fatal("fabricated sample on HTTP503")
	default:
	}
	f.mu.Lock()
	f.metricsMode = "missing"
	f.mu.Unlock()
	waitStatsAttr(t, d, cfg.ID, "metrics_current_gap", "native_fields_missing")
	select {
	case <-ch:
		t.Fatal("fabricated sample for missing native fields")
	default:
	}
	f.mu.Lock()
	f.metricsMode = "invalid"
	f.mu.Unlock()
	waitStatsAttr(t, d, cfg.ID, "metrics_current_gap", "native_sample_invalid")
	select {
	case <-ch:
		t.Fatal("fabricated sample for invalid native CPU")
	default:
	}
	f.mu.Lock()
	f.metricsMode = "zero"
	f.mu.Unlock()
	select {
	case sample := <-ch:
		if sample == nil || sample.ResourceUsage.CpuStats.TotalTicks != 0 || sample.ResourceUsage.MemoryStats.RSS != 0 {
			t.Fatal("actual measured zero rejected")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("sample did not resume")
	}
	waitStatsAttr(t, d, cfg.ID, "metrics_current_gap", "")
	s, _ := d.InspectTask(cfg.ID)
	n, _ := strconv.Atoi(s.DriverAttributes["metrics_samples_skipped"])
	if n < 3 {
		t.Fatal("skips not counted")
	}
	if s.DriverAttributes["metrics_warmup_skipped"] != "1" || s.DriverAttributes["metrics_last_delivered_ns"] == "0" {
		t.Fatal("warmup/delivery diagnostic missing")
	}
	d.eventsMu.Lock()
	missing, resumed := 0, 0
	for _, event := range d.history {
		if strings.Contains(event.Message, "PRIVATE_ERROR_TOKEN") {
			t.Fatal("raw native error leaked")
		}
		if strings.Contains(event.Message, "metrics_sample_missing ") {
			missing++
		}
		if strings.Contains(event.Message, "metrics_samples_resumed ") {
			resumed++
		}
	}
	d.eventsMu.Unlock()
	if missing != 1 || resumed != 1 {
		t.Fatalf("not rate-limited/paired: missing=%d resumed=%d", missing, resumed)
	}
	cancel()
	for range ch {
	}
	waitStatsAttr(t, d, cfg.ID, "metrics_streams_active", "0")
	waitStatsAttr(t, d, cfg.ID, "metrics_last_stream_end", "consumer_context_cancelled")
}
func TestStatsGapNoticeRateLimit(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	started(t, d, cfg)
	task, _ := d.get(cfg.ID)
	now := time.Unix(1000, 0)
	id := d.statsStarted(task, time.Second)
	d.statsSkipped(task, id, "native_metrics_error", 503, now)
	d.statsSkipped(task, id, "native_metrics_error", 503, now.Add(10*time.Second))
	d.statsDelivered(task, id, now.Add(20*time.Second), true)
	d.statsSkipped(task, id, "consumer_backpressure", 0, now.Add(30*time.Second))
	d.statsDelivered(task, id, now.Add(40*time.Second), false)
	d.statsSkipped(task, id, "native_metrics_timeout", 0, now.Add(time.Minute+time.Second))
	d.eventsMu.Lock()
	defer d.eventsMu.Unlock()
	missing, resumed := 0, 0
	for _, e := range d.history {
		if strings.Contains(e.Message, "metrics_sample_missing ") {
			missing++
		}
		if strings.Contains(e.Message, "metrics_samples_resumed ") {
			resumed++
		}
	}
	if missing != 2 || resumed != 1 {
		t.Fatalf("notice rate incorrect: %d/%d", missing, resumed)
	}
	task.mu.Lock()
	defer task.mu.Unlock()
	if task.stats.Skipped != 4 || task.stats.NoticesSuppressed != 2 {
		t.Fatal("suppressed notices hid counters")
	}
}
func TestMeasuredZeroIsNotMissing(t *testing.T) {
	cpu := 0.0
	mem := uint64(0)
	s := measuredUsage(&boxlite.Metrics{CPUPercent: &cpu, MemoryBytes: &mem}, cpustats.Compute{TotalCompute: 1000, NumCores: 1}, time.Now())
	if s == nil || len(s.ResourceUsage.CpuStats.Measured) == 0 || len(s.ResourceUsage.MemoryStats.Measured) == 0 {
		t.Fatal("valid zero was replaced by missing")
	}
}
