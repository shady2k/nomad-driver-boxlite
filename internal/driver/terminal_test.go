package driver

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestTerminalSummaryOwnershipAndServerEvent(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	started(t, d, cfg)
	task, _ := d.get(cfg.ID)
	dir := d.companionDir(task)
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	receipt := map[string]any{"version": 1, "allocation_id": "wrong", "vm_id": task.state.VMID, "summary": map[string]string{"stage": "repo_clone", "exception_type": "TimeoutError", "reason": "deadline_exceeded", "deadline_seconds": "120"}}
	write := func() {
		raw, _ := json.Marshal(receipt)
		if err := os.WriteFile(filepath.Join(dir, "terminal-summary.json"), raw, 0600); err != nil {
			t.Fatal(err)
		}
	}
	write()
	task.mu.Lock()
	d.publishTerminalSummary(task)
	task.mu.Unlock()
	if len(task.state.TerminalSummary) != 0 {
		t.Fatal("wrong allocation accepted")
	}
	receipt["allocation_id"] = task.state.AllocID
	write()
	task.mu.Lock()
	d.publishTerminalSummary(task)
	task.mu.Unlock()
	if task.state.TerminalSummary["stage"] != "repo_clone" {
		t.Fatal("summary not persisted")
	}
	d.eventsMu.Lock()
	defer d.eventsMu.Unlock()
	found := false
	for _, event := range d.history {
		if event.Annotations["phase"] == "terminal_failure_summary" {
			found = true
		}
	}
	if !found {
		t.Fatal("summary not forwarded to Nomad task event")
	}
}
