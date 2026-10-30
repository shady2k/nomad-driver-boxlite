package driver

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func injectRuntime(f *fakeRuntime, match func(*http.Request) bool, code int) {
	f.server.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if match(r) {
			w.WriteHeader(code)
			fmt.Fprint(w, `{"error":{"code":"invalid_argument","message":"injected startup failure"}}`)
			return
		}
		f.serve(w, r)
	})
}
func TestEarlyStartFailureCleansOwnResources(t *testing.T) {
	for _, stage := range []string{"create-rejected", "attach", "binding"} {
		t.Run(stage, func(t *testing.T) {
			f := newRuntime(t)
			d := setupDriver(t, f, "")
			c := taskConfig(t)
			if stage == "create-rejected" {
				injectRuntime(f, func(r *http.Request) bool { return r.Method == "POST" && r.URL.Path == "/v1/boxes" }, 400)
			}
			if stage == "attach" {
				injectRuntime(f, func(r *http.Request) bool { return strings.HasSuffix(r.URL.Path, "/attach") }, 400)
			}
			if stage == "binding" {
				d.config.AllowExternalStdio = true
				c.AllocDir = t.TempDir()
				var tc TaskConfig
				_ = c.DecodeDriverConfig(&tc)
				tc.StdioMode = "external"
				_ = c.EncodeConcreteDriverConfig(tc)
			}
			if _, _, err := d.StartTask(c); err == nil {
				t.Fatal("failure not injected")
			}
			s, err := d.store.Read(taskKey(c.AllocID, c.Name))
			if err != nil || !s.Destroyed || s.StartupCleanupPending {
				t.Fatalf("reservation not released: %+v %v", s, err)
			}
			if _, _, err = d.StartTask(c); err == nil {
				t.Fatal("failed task replayed")
			}
			creates, starts, stops, deletes := f.counts()
			if deletes != 0 {
				t.Fatal("disk deleted")
			}
			if stage == "create-rejected" {
				if creates+starts+stops != 0 {
					t.Fatal("native action after rejection")
				}
			} else if stops != 1 {
				t.Fatalf("stop count=%d", stops)
			}
		})
	}
}
func TestStartupCleanupStopFailureRetriesWithoutReplay(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	c := taskConfig(t)
	injectRuntime(f, func(r *http.Request) bool { return strings.HasSuffix(r.URL.Path, "/attach") }, 400)
	f.failStop = true
	if _, _, err := d.StartTask(c); err == nil {
		t.Fatal("expected error")
	}
	s, _ := d.store.Read(taskKey(c.AllocID, c.Name))
	if s.Destroyed || !s.StartupCleanupPending {
		t.Fatal("uncertain stop released budget")
	}
	f.mu.Lock()
	f.failStop = false
	f.mu.Unlock()
	if _, _, err := d.StartTask(c); err == nil {
		t.Fatal("cleanup retry became task replay")
	}
	s, _ = d.store.Read(s.Key)
	if !s.Destroyed {
		t.Fatal("retry did not release")
	}
	creates, starts, _, deletes := f.counts()
	if creates != 1 || starts != 0 || deletes != 0 {
		t.Fatal("create/start/delete replayed")
	}
}
func TestStartupCleanupUncertainCreateAndStartPreserved(t *testing.T) {
	for _, stage := range []string{"create", "start"} {
		t.Run(stage, func(t *testing.T) {
			f := newRuntime(t)
			d := setupDriver(t, f, "")
			c := taskConfig(t)
			f.loseCreate = stage == "create"
			f.loseStart = stage == "start"
			if _, _, err := d.StartTask(c); err == nil {
				t.Fatal("expected uncertainty")
			}
			s, _ := d.store.Read(taskKey(c.AllocID, c.Name))
			if s.Destroyed || s.StartupCleanupPending {
				t.Fatal("uncertain mutation cleaned")
			}
			_, _, stops, deletes := f.counts()
			if stops+deletes != 0 {
				t.Fatal("uncertain VM touched")
			}
		})
	}
}
func TestStartupCleanupOwnershipMismatchPreserved(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	c := taskConfig(t)
	h := started(t, d, c)
	tt, _ := d.get(c.ID)
	tt.mu.Lock()
	tt.state.StartupCleanupPending = true
	_ = d.store.Write(tt.state)
	tt.mu.Unlock()
	f.mu.Lock()
	b := f.boxes["vm1"]
	b.Name = "foreign"
	f.boxes["vm1"] = b
	f.mu.Unlock()
	tt.mu.Lock()
	err := d.cleanupFailedStartup(tt)
	tt.mu.Unlock()
	if err == nil {
		t.Fatal("foreign VM accepted")
	}
	_, _, stops, deletes := f.counts()
	if stops+deletes != 0 {
		t.Fatal("foreign VM touched")
	}
	_ = h
}
func TestBootstrapFailureReceiptStopsOwnVMAndKeepsDiagnostics(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	c := taskConfig(t)
	started(t, d, c)
	tt, _ := d.get(c.ID)
	tt.mu.Lock()
	dir := d.companionDir(tt)
	_ = os.MkdirAll(dir, 0700)
	receipt := map[string]any{"version": 1, "allocation_id": c.AllocID, "vm_id": tt.state.VMID, "summary": map[string]string{"stage": "child_payload", "reason": "bootstrap_failed", "error_type": "RuntimeError"}}
	raw, _ := json.Marshal(receipt)
	_ = os.WriteFile(filepath.Join(dir, "terminal-summary.json"), raw, 0600)
	tt.state.CompanionIssued = true
	_ = d.store.Write(tt.state)
	tt.mu.Unlock()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		s, _ := d.store.Read(taskKey(c.AllocID, c.Name))
		if s.Destroyed {
			if s.TerminalSummary["stage"] != "child_payload" {
				t.Fatal("diagnostics lost")
			}
			if _, err := os.Stat(filepath.Join(dir, "terminal-summary.json")); err != nil {
				t.Fatal(err)
			}
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("bootstrap cleanup did not complete")
}
func TestRuntimeFailureDoesNotAuthorizeBootstrapCleanup(t *testing.T) {
	for _, stage := range []string{"entry", "unknown", "runtime", "transport"} {
		if bootstrapFailureBeforeACP(map[string]string{"stage": stage, "reason": "bootstrap_failed"}) {
			t.Fatal("runtime failure classified as startup")
		}
	}
}

func TestStartupCleanupRecoveryRetriesOnlyStop(t *testing.T) {
	f := newRuntime(t)
	dir := filepath.Join(t.TempDir(), "state")
	d := setupDriver(t, f, dir)
	c := taskConfig(t)
	injectRuntime(f, func(r *http.Request) bool { return strings.HasSuffix(r.URL.Path, "/attach") }, 400)
	f.failStop = true
	if _, _, err := d.StartTask(c); err == nil {
		t.Fatal("expected startup failure")
	}
	_ = d.Shutdown(nil)
	f.mu.Lock()
	f.failStop = false
	f.mu.Unlock()
	recovered := setupDriver(t, f, dir)
	if _, _, err := recovered.StartTask(c); err == nil {
		t.Fatal("cleanup recovery replayed task")
	}
	s, err := recovered.store.Read(taskKey(c.AllocID, c.Name))
	if err != nil || !s.Destroyed {
		t.Fatalf("cleanup not durable: %v", err)
	}
	creates, starts, _, deletes := f.counts()
	if creates != 1 || starts != 0 || deletes != 0 {
		t.Fatal("recovery replayed mutation")
	}
}

func TestReadyReceiptAndOldSessionForbidAutomaticCleanup(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	c := taskConfig(t)
	started(t, d, c)
	tt, _ := d.get(c.ID)
	tt.mu.Lock()
	dir := d.companionDir(tt)
	_ = os.MkdirAll(dir, 0700)
	_ = os.WriteFile(filepath.Join(dir, "application-ready.json"), []byte(`{"version":1,"allocation_id":"allocation-a","vm_id":"vm1"}`), 0600)
	if d.noApplicationReadyReceipt(tt) {
		t.Fatal("ready session eligible for cleanup")
	}
	tt.state.CompanionIssued = true
	tt.state.TerminalSummary = map[string]string{"stage": "child_payload", "reason": "bootstrap_failed"}
	tt.mu.Unlock()
	time.Sleep(30 * time.Millisecond)
	_, _, stops, _ := f.counts()
	if stops != 0 {
		t.Fatal("ready VM stopped")
	}
	tt.mu.Lock()
	tt.state.StartupCleanupEligible = false
	_ = os.Remove(filepath.Join(dir, "application-ready.json"))
	tt.mu.Unlock()
	time.Sleep(30 * time.Millisecond)
	_, _, stops, _ = f.counts()
	if stops != 0 {
		t.Fatal("legacy session stopped")
	}
}
