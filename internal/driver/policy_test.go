package driver

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestVariablePolicyFailClosedAndHotChange(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	b, index, e := d.readNodePolicy(cfg)
	if e != nil || b.MaxVMs != 4 || index != 42 {
		t.Fatalf("%+v %d %v", b, index, e)
	}
	for _, status := range []int{404, 403, 503} {
		f.mu.Lock()
		f.policyStatus = status
		f.mu.Unlock()
		if _, _, e = d.readNodePolicy(cfg); e == nil {
			t.Fatalf("status %d accepted", status)
		}
	}
	f.mu.Lock()
	f.policyStatus = 0
	f.mu.Unlock()
	for _, body := range []string{`{}`, `{"Items":{"boxlite_node_budgets":"invalid"}}`, `{"Items":{"boxlite_node_budgets":"{\"version\":1,\"nodes\":{}}"}}`} {
		f.mu.Lock()
		f.policyBody = body
		f.mu.Unlock()
		if _, _, e = d.readNodePolicy(cfg); e == nil {
			t.Fatal("malformed/missing accepted")
		}
	}
	for _, limit := range []int{0, -1, 2} {
		p := map[string]any{"version": 1, "nodes": map[string]any{"node-test": NodeBudget{MaxVMs: limit, MaxGuestCPUs: 8, MaxReservationMiB: 24576, OverheadMiB: 1024, CPUMHzPerVCPU: 1000, DefaultDiskGiB: 12, MaxDiskGiB: 12}}}
		raw, _ := json.Marshal(p)
		v, _ := json.Marshal(map[string]any{"Items": map[string]string{"boxlite_node_budgets": string(raw)}, "ModifyIndex": 99})
		f.mu.Lock()
		f.policyBody = string(v)
		f.mu.Unlock()
		b, index, e = d.readNodePolicy(cfg)
		if limit <= 0 && e == nil {
			t.Fatal("nonpositive accepted")
		}
		if limit == 2 && (e != nil || b.MaxVMs != 2 || index != 99) {
			t.Fatal("hot policy not read")
		}
	}
}
func TestSignedIdentityBindingAndServerVerification(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	for _, token := range []string{"", "00000000-0000-0000-0000-000000000000", strings.Replace(testJWT(cfg), "verified-by-test-server", "forged", 1)} {
		cfg.Env["NOMAD_TOKEN"] = token
		if _, _, e := d.readNodePolicy(cfg); e == nil {
			t.Fatal("invalid identity accepted")
		}
	}
	cfg.Env["NOMAD_TOKEN"] = testJWT(cfg)
	cfg.AllocID = "other-allocation"
	if _, _, e := d.readNodePolicy(cfg); e == nil {
		t.Fatal("foreign allocation identity accepted")
	}
}
func TestRecoveryAndStopDoNotReadVariable(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	h := started(t, d, cfg)
	d.cancel()
	f.awaitDetached(t, stateOf(t, h).VMID)
	f.mu.Lock()
	f.policyStatus = 503
	f.mu.Unlock()
	d2 := setupDriver(t, f, d.store.Root)
	if e := d2.RecoverTask(h); e != nil {
		t.Fatal(e)
	}
	if e := d2.StopTask(cfg.ID, 0, ""); e != nil {
		t.Fatal(e)
	}
}
