package driver

import (
	"context"
	"fmt"
	"nomad-driver-boxlite/internal/boxlite"
	"strings"
	"sync"
	"testing"
)

func TestAtomicNodeBudgetAndRestart(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	var wg sync.WaitGroup
	var mu sync.Mutex
	accepted := 0
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			s := &State{Version: 1, Key: digest(fmt.Sprint(i)), Name: fmt.Sprint(i), CPUs: 2, MemoryMiB: 2048, Phase: "intent"}
			if d.reserveBudget(s, taskConfig(t), TaskConfig{CPUs: 2, MemoryMiB: 2048, DiskGiB: 12}) == nil {
				mu.Lock()
				accepted++
				mu.Unlock()
			}
		}(i)
	}
	wg.Wait()
	if accepted != 4 {
		t.Fatalf("accepted %d", accepted)
	}
	d2 := setupDriver(t, f, d.store.Root)
	d2.config = d.config
	if e := d2.reserveBudget(&State{Version: 1, Key: digest("next"), Name: "next", CPUs: 1, MemoryMiB: 512}, taskConfig(t), TaskConfig{CPUs: 1, MemoryMiB: 512, DiskGiB: 12}); e == nil {
		t.Fatal("restart lost reservations")
	}
	_ = d2.Shutdown(context.Background())
}
func TestBudgetCountsForeignVMAndOverhead(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	f.mu.Lock()
	f.boxes["foreign"] = boxlite.Box{ID: "foreign", Name: "foreign", Status: "running", CPUs: 8, MemoryMiB: 1024}
	f.mu.Unlock()
	if d.reserveBudget(&State{Version: 1, Key: digest("x"), Name: "x", CPUs: 1, MemoryMiB: 512}, taskConfig(t), TaskConfig{CPUs: 1, MemoryMiB: 512, DiskGiB: 12}) == nil {
		t.Fatal("foreign CPU ignored")
	}
	f.mu.Lock()
	f.boxes["foreign"] = boxlite.Box{ID: "foreign", Name: "foreign", Status: "running", CPUs: 1, MemoryMiB: 23000}
	f.mu.Unlock()
	if d.reserveBudget(&State{Version: 1, Key: digest("x"), Name: "x", CPUs: 1, MemoryMiB: 512}, taskConfig(t), TaskConfig{CPUs: 1, MemoryMiB: 512, DiskGiB: 12}) == nil {
		t.Fatal("RAM overhead ignored")
	}
	f.mu.Lock()
	f.boxes["foreign"] = boxlite.Box{ID: "foreign", Name: "foreign", Status: "stopped", CPUs: 1, MemoryMiB: 23000}
	f.mu.Unlock()
	if e := d.reserveBudget(&State{Version: 1, Key: digest("x"), Name: "x", CPUs: 1, MemoryMiB: 512}, taskConfig(t), TaskConfig{CPUs: 1, MemoryMiB: 512, DiskGiB: 12}); e != nil {
		t.Fatal(e)
	}
}
func TestWorkerCannotSetGuestBootstrapOrHostUser(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	d.config.CompanionCommand = []string{"/operator/fixed"}
	d.guestCommand = []string{"/fixed/bootstrap"}
	if _, _, e := d.validate(cfg); e == nil || !strings.Contains(e.Error(), "operator-fixed") {
		t.Fatal("caller-selected bootstrap accepted")
	}
	d.config.CompanionCommand = nil
	cfg.User = "root"
	if _, _, e := d.validate(cfg); e == nil {
		t.Fatal("host user accepted")
	}
}
