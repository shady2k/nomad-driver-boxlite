package driver

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	log "github.com/hashicorp/go-hclog"
	nstructs "github.com/hashicorp/nomad/nomad/structs"
	"github.com/hashicorp/nomad/plugins/drivers"
	"nomad-driver-boxlite/internal/boxlite"
)

type fakeRuntime struct {
	mu                                           sync.Mutex
	boxes                                        map[string]boxlite.Box
	conns                                        map[string]*websocket.Conn
	requests                                     []string
	creates, starts, stops, deletes              int
	loseCreate, loseStart, failStop, unreachable bool
	server                                       *httptest.Server
	metricsMode                                  string
	policyBody                                   string
	policyStatus                                 int
}

func newRuntime(t *testing.T) *fakeRuntime {
	f := &fakeRuntime{boxes: map[string]boxlite.Box{}, conns: map[string]*websocket.Conn{}}
	f.server = httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(func() {
		f.mu.Lock()
		for _, c := range f.conns {
			c.Close()
		}
		f.mu.Unlock()
		f.server.Close()
	})
	return f
}
func (f *fakeRuntime) serve(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.requests = append(f.requests, r.Method+" "+r.URL.Path)
	if f.unreachable {
		w.WriteHeader(503)
		fmt.Fprint(w, `{"error":{"type":"TimeoutError","code":"runtime_busy","message":"observe unavailable"}}`)
		return
	}
	if r.URL.Path == "/v1/var/nomad/jobs" {
		if !strings.HasSuffix(r.Header.Get("X-Nomad-Token"), ".verified-by-test-server") {
			w.WriteHeader(403)
			return
		}
		if f.policyStatus != 0 {
			w.WriteHeader(f.policyStatus)
			return
		}
		body := f.policyBody
		if body == "" {
			body = `{"Items":{"boxlite_node_budgets":"{\"version\":1,\"nodes\":{\"node-test\":{\"max_vms\":4,\"max_guest_cpus\":8,\"max_reservation_mib\":24576,\"memory_overhead_mib\":1024,\"cpu_mhz_per_vcpu\":1000,\"default_disk_gib\":12,\"max_disk_gib\":12}}}"},"ModifyIndex":42}`
		}
		fmt.Fprint(w, body)
		return
	}
	if r.URL.Path == "/v1/me" {
		fmt.Fprint(w, `{"principal":{"id":"mock"}}`)
		return
	}
	if r.URL.Path == "/v1/boxes" && r.Method == "GET" {
		boxes := []boxlite.Box{}
		for _, b := range f.boxes {
			boxes = append(boxes, b)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"boxes": boxes})
		return
	}
	if r.URL.Path == "/v1/boxes" && r.Method == "POST" {
		var s boxlite.Create
		if e := json.NewDecoder(r.Body).Decode(&s); e != nil {
			w.WriteHeader(400)
			return
		}
		f.creates++
		id := fmt.Sprintf("vm%d", f.creates)
		f.boxes[id] = boxlite.Box{ID: id, Name: s.Name, Image: s.Image, CPUs: s.CPUs, MemoryMiB: s.MemoryMiB, Status: "configured"}
		if f.loseCreate {
			f.loseCreate = false
			h := w.(http.Hijacker)
			c, _, _ := h.Hijack()
			c.Close()
			return
		}
		w.WriteHeader(201)
		_ = json.NewEncoder(w).Encode(f.boxes[id])
		return
	}
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	if len(parts) < 3 || parts[0] != "v1" || parts[1] != "boxes" {
		w.WriteHeader(404)
		return
	}
	id := parts[2]
	b, ok := f.boxes[id]
	if !ok {
		for k, v := range f.boxes {
			if v.Name == id {
				id = k
				b = v
				ok = true
				break
			}
		}
	}
	if !ok {
		w.WriteHeader(404)
		fmt.Fprint(w, `{"error":{"type":"NotFoundError","code":"not_found","message":"missing"}}`)
		return
	}
	if len(parts) == 3 {
		if r.Method == "DELETE" {
			f.deletes++
			delete(f.boxes, id)
		}
		_ = json.NewEncoder(w).Encode(b)
		return
	}
	if parts[len(parts)-1] == "metrics" {
		if f.metricsMode == "error" {
			w.WriteHeader(503)
			fmt.Fprint(w, `{"error":{"code":"runtime_busy","message":"PRIVATE_ERROR_TOKEN"}}`)
			return
		}
		if f.metricsMode == "missing" {
			fmt.Fprint(w, `{}`)
			return
		}
		if f.metricsMode == "invalid" {
			fmt.Fprint(w, `{"cpu_percent":-1,"memory_bytes":1024}`)
			return
		}
		if f.metricsMode == "zero" {
			fmt.Fprint(w, `{"cpu_percent":0,"memory_bytes":0}`)
			return
		}
		cpu := float64(len(id) * 10)
		memory := uint64(id[len(id)-1]) * 1024
		_ = json.NewEncoder(w).Encode(boxlite.Metrics{CPUPercent: &cpu, MemoryBytes: &memory})
		return
	}
	if parts[len(parts)-1] == "attach" {
		if f.conns[id] != nil {
			w.WriteHeader(409)
			return
		}
		var u websocket.Upgrader
		h := http.Header{}
		h.Set("x-boxlite-execution-id", "main-"+id)
		c, e := u.Upgrade(w, r, h)
		if e != nil {
			return
		}
		f.conns[id] = c
		// Attach boots the VM, but does not run the main command.
		if b.Status == "configured" {
			pid := 900 + f.creates
			b.PID = &pid
			b.Status = "running"
			f.boxes[id] = b
		}
		f.mu.Unlock()
		for {
			if _, _, e = c.ReadMessage(); e != nil {
				break
			}
		}
		f.mu.Lock()
		if f.conns[id] == c {
			delete(f.conns, id)
		}
		c.Close()
		return
	}
	if parts[3] == "start" {
		f.starts++
		b.Status = "running"
		pid := 1000 + f.starts
		b.PID = &pid
		f.boxes[id] = b
		if f.loseStart {
			f.loseStart = false
			c, _, _ := w.(http.Hijacker).Hijack()
			c.Close()
			return
		}
		_ = json.NewEncoder(w).Encode(b)
		return
	}
	if parts[3] == "stop" {
		f.stops++
		if f.failStop {
			w.WriteHeader(503)
			fmt.Fprint(w, `{"error":{"type":"TimeoutError","code":"stop_unknown","message":"not confirmed"}}`)
			return
		}
		zero := 0
		code := 143
		b.Status = "stopped"
		b.PID = &zero
		b.ExitCode = &code
		f.boxes[id] = b
		_ = json.NewEncoder(w).Encode(b)
		return
	}
	w.WriteHeader(404)
}
func (f *fakeRuntime) complete(id string, code int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	b := f.boxes[id]
	zero := 0
	b.Status = "stopped"
	b.PID = &zero
	b.ExitCode = &code
	f.boxes[id] = b
	if c := f.conns[id]; c != nil {
		_ = c.WriteMessage(websocket.BinaryMessage, append([]byte{1}, []byte("native stdout\n")...))
		_ = c.WriteMessage(websocket.BinaryMessage, append([]byte{2}, []byte("native stderr\n")...))
		_ = c.WriteMessage(websocket.TextMessage, []byte(fmt.Sprintf(`{"type":"exit","exit_code":%d}`, code)))
	}
}
func (f *fakeRuntime) counts() (int, int, int, int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.creates, f.starts, f.stops, f.deletes
}
func (f *fakeRuntime) awaitDetached(t *testing.T, id string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		f.mu.Lock()
		c := f.conns[id]
		f.mu.Unlock()
		if c == nil {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("old stream still attached")
}
func setupDriver(t *testing.T, f *fakeRuntime, stateDir string) *Driver {
	t.Helper()
	if stateDir == "" {
		stateDir = filepath.Join(t.TempDir(), "state")
	}
	d := New(context.Background(), log.NewNullLogger())
	if e := d.configure(Config{Endpoint: f.server.URL, StateDir: stateDir, PolicyEndpoint: f.server.URL, PolicyNamespace: "boxlite-prime", PolicyPath: "nomad/jobs", StartTimeout: "2s"}); e != nil {
		t.Fatal(e)
	}
	d.pollInterval = 5 * time.Millisecond
	t.Cleanup(func() { _ = d.Shutdown(context.Background()) })
	return d
}
func taskConfig(t *testing.T) *drivers.TaskConfig {
	t.Helper()
	dir := t.TempDir()
	out := filepath.Join(dir, "stdout")
	err := filepath.Join(dir, "stderr")
	_ = os.WriteFile(out, nil, 0600)
	_ = os.WriteFile(err, nil, 0600)
	cfg := &drivers.TaskConfig{Namespace: "boxlite-prime", NodeID: "node-test", JobID: "job-a", ID: "driver-task-a", AllocID: "allocation-a", Name: "generic", StdoutPath: out, StderrPath: err, Env: map[string]string{"TEST_SECRET_ENV": "must-not-be-in-handle"}, Resources: &drivers.Resources{NomadResources: &nstructs.AllocatedTaskResources{Cpu: nstructs.AllocatedCpuResources{CpuShares: 2000}, Memory: nstructs.AllocatedMemoryResources{MemoryMB: 3072}}}}
	e := cfg.EncodeConcreteDriverConfig(TaskConfig{Image: "registry.example/test@sha256:" + strings.Repeat("a", 64), Command: []string{"/bin/sh", "-c", "echo generic"}, CPUs: 2, MemoryMiB: 2048, DiskGiB: 12})
	if e != nil {
		t.Fatal(e)
	}
	cfg.Env["NOMAD_TOKEN"] = testJWT(cfg)
	return cfg
}
func testJWT(cfg *drivers.TaskConfig) string {
	b, _ := json.Marshal(map[string]string{"nomad_namespace": cfg.Namespace, "nomad_allocation_id": cfg.AllocID, "nomad_job_id": cfg.JobID, "nomad_task": cfg.Name})
	return "header." + base64.RawURLEncoding.EncodeToString(b) + ".verified-by-test-server"
}
func started(t *testing.T, d *Driver, c *drivers.TaskConfig) *drivers.TaskHandle {
	t.Helper()
	c.Env["NOMAD_TOKEN"] = testJWT(c)
	d.config.PolicyNamespace = c.Namespace
	h, _, e := d.StartTask(c)
	if e != nil {
		t.Fatal(e)
	}
	return h
}
func stateOf(t *testing.T, h *drivers.TaskHandle) State {
	t.Helper()
	var s State
	if e := h.GetDriverState(&s); e != nil {
		t.Fatal(e)
	}
	return s
}
func TestStartWaitNativeLogsExitAndPreserveDisks(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	h := started(t, d, cfg)
	s := stateOf(t, h)
	if h.Config.Env != nil {
		t.Fatal("environment persisted")
	}
	events, _ := d.TaskEvents(t.Context())
	ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
	defer cancel()
	wait, e := d.WaitTask(ctx, cfg.ID)
	if e != nil {
		t.Fatal(e)
	}
	f.complete(s.VMID, 7)
	select {
	case r := <-wait:
		if r == nil || r.ExitCode != 7 || r.Err != nil {
			t.Fatalf("%+v", r)
		}
	case <-ctx.Done():
		t.Fatal("no terminal result")
	}
	out, _ := os.ReadFile(cfg.StdoutPath)
	errout, _ := os.ReadFile(cfg.StderrPath)
	if string(out) != "native stdout\n" || string(errout) != "native stderr\n" {
		t.Fatalf("logs %q %q", out, errout)
	}
	if e = d.DestroyTask(cfg.ID, false); e != nil {
		t.Fatal(e)
	}
	_, _, _, del := f.counts()
	if del != 0 {
		t.Fatal("destroy deleted disk/VM")
	}
	seen := false
	for i := 0; i < 8; i++ {
		select {
		case e := <-events:
			if e.Annotations["vm_id"] == s.VMID {
				seen = true
			}
		default:
		}
	}
	if !seen {
		t.Fatal("no native VM identity events")
	}
}
func TestDuplicateStartSameVMAndChangedSpecRejected(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	a := stateOf(t, started(t, d, cfg))
	b := stateOf(t, started(t, d, cfg))
	if a.VMID != b.VMID {
		t.Fatal("duplicate VM")
	}
	c, s, _, _ := f.counts()
	if c != 1 || s != 1 {
		t.Fatalf("create=%d start=%d", c, s)
	}
	cfg.AllocID = "another-allocation"
	if _, _, e := d.StartTask(cfg); e == nil {
		t.Fatal("reused task ID across allocations")
	}
}
func TestRecoverDriverRestartSameIDNoCreateStart(t *testing.T) {
	f := newRuntime(t)
	dir := filepath.Join(t.TempDir(), "state")
	d := setupDriver(t, f, dir)
	cfg := taskConfig(t)
	h := started(t, d, cfg)
	s := stateOf(t, h)
	_ = d.Shutdown(t.Context())
	f.awaitDetached(t, s.VMID)
	next := setupDriver(t, f, dir)
	if e := next.RecoverTask(h); e != nil {
		t.Fatal(e)
	}
	v, e := next.InspectTask(cfg.ID)
	if e != nil || v.DriverAttributes["vm_id"] != s.VMID {
		t.Fatalf("%+v %v", v, e)
	}
	c, start, _, _ := f.counts()
	if c != 1 || start != 1 {
		t.Fatal("recovery repeated lifecycle POST")
	}
}
func TestLostCreateResponseReconcilesPrivateNameNeverReposts(t *testing.T) {
	f := newRuntime(t)
	f.loseCreate = true
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	if _, _, e := d.StartTask(cfg); e == nil || !boxlite.IsUncertain(e) {
		t.Fatalf("wanted uncertain error: %v", e)
	}
	h := started(t, d, cfg)
	if stateOf(t, h).VMID == "" {
		t.Fatal("no adopted stable VM")
	}
	c, s, _, _ := f.counts()
	if c != 1 || s != 1 {
		t.Fatalf("create=%d start=%d", c, s)
	}
}
func TestLostStartResponseNeverStartsAgain(t *testing.T) {
	f := newRuntime(t)
	f.loseStart = true
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	if _, _, e := d.StartTask(cfg); e == nil || !boxlite.IsUncertain(e) {
		t.Fatalf("%v", e)
	}
	f.awaitDetached(t, "vm1")
	h := started(t, d, cfg)
	if stateOf(t, h).VMID != "vm1" {
		t.Fatal("changed VM")
	}
	c, s, _, _ := f.counts()
	if c != 1 || s != 1 {
		t.Fatal("uncertain start replayed")
	}
}
func TestStopRejectsForeignOwnershipAndDestroyCannotDeleteIt(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	s := stateOf(t, started(t, d, cfg))
	f.mu.Lock()
	b := f.boxes[s.VMID]
	b.Name = "foreign"
	f.boxes[s.VMID] = b
	f.mu.Unlock()
	if e := d.StopTask(cfg.ID, time.Second, ""); e == nil {
		t.Fatal("foreign stop allowed")
	}
	if e := d.DestroyTask(cfg.ID, true); e == nil {
		t.Fatal("foreign force destroy allowed")
	}
	_, _, stop, del := f.counts()
	if stop != 0 || del != 0 {
		t.Fatal("touched foreign VM")
	}
}
func TestUncertainStopIsNotSuccessOrVMDeath(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	started(t, d, cfg)
	f.mu.Lock()
	f.failStop = true
	f.mu.Unlock()
	if e := d.StopTask(cfg.ID, time.Second, ""); e == nil || !boxlite.IsUncertain(e) {
		t.Fatalf("%v", e)
	}
	v, e := d.InspectTask(cfg.ID)
	if e != nil || v.State != drivers.TaskStateRunning || v.ExitResult != nil {
		t.Fatal("reported unconfirmed stop as death")
	}
}
func TestRuntimeUnreachableRemainsUnknownNotTerminal(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	started(t, d, cfg)
	f.mu.Lock()
	f.unreachable = true
	f.mu.Unlock()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		v, _ := d.InspectTask(cfg.ID)
		if v.DriverAttributes["runtime_observation_error"] != "" {
			if v.State != drivers.TaskStateRunning || v.ExitResult != nil {
				t.Fatal("fabricated terminal state")
			}
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("unreachable cause not surfaced")
}
func TestStopDestroyAndTombstoneNoReuse(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	started(t, d, cfg)
	if e := d.DestroyTask(cfg.ID, false); e == nil {
		t.Fatal("destroyed running VM")
	}
	if e := d.StopTask(cfg.ID, time.Second, "SIGUSR1"); e == nil {
		t.Fatal("pretended custom signal support")
	}
	if e := d.StopTask(cfg.ID, time.Second, ""); e != nil {
		t.Fatal(e)
	}
	if e := d.DestroyTask(cfg.ID, false); e != nil {
		t.Fatal(e)
	}
	if _, _, e := d.StartTask(cfg); e == nil {
		t.Fatal("recreated destroyed allocation")
	}
	_, _, stops, del := f.counts()
	if stops != 1 || del != 0 {
		t.Fatal("unexpected destructive operations")
	}
}
func TestReservationValidationBeforeREST(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	cfg.Resources.NomadResources.Memory.MemoryMB = 2048
	if _, _, e := d.StartTask(cfg); e == nil {
		t.Fatal("missing memory overhead accepted")
	}
	cfg.Resources.NomadResources.Memory.MemoryMB = 3072
	cfg.Resources.NomadResources.Cpu.CpuShares = 1000
	if _, _, e := d.StartTask(cfg); e == nil {
		t.Fatal("under-reserved CPU accounting accepted")
	}
	c, s, _, _ := f.counts()
	if c != 0 || s != 0 {
		t.Fatal("invalid task reached REST")
	}
}
func TestRecoveryRejectsForgedHandleBinding(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	h := started(t, d, cfg)
	s := stateOf(t, h)
	_ = d.Shutdown(t.Context())
	f.awaitDetached(t, s.VMID)
	next := setupDriver(t, f, d.config.StateDir)
	s.VMID = "foreign-id"
	_ = h.SetDriverState(&s)
	if e := next.RecoverTask(h); e == nil {
		t.Fatal("forged recovery ownership")
	}
}
func TestNodeSecretNeverInHandleOrState(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	key := "SENSITIVE-NODE-API-CREDENTIAL"
	p := filepath.Join(t.TempDir(), "api-key")
	_ = os.WriteFile(p, []byte(key), 0600)
	if e := d.configure(Config{Endpoint: f.server.URL, StateDir: d.config.StateDir, APIKeyFile: p, PolicyEndpoint: f.server.URL, PolicyNamespace: "boxlite-prime", PolicyPath: "nomad/jobs"}); e != nil {
		t.Fatal(e)
	}
	cfg := taskConfig(t)
	h := started(t, d, cfg)
	if strings.Contains(string(h.DriverState), key) {
		t.Fatal("key in task handle")
	}
	stateFiles, _ := filepath.Glob(filepath.Join(d.config.StateDir, "*.json"))
	for _, p := range stateFiles {
		raw, _ := os.ReadFile(p)
		if strings.Contains(string(raw), key) || strings.Contains(string(raw), "must-not-be-in-handle") {
			t.Fatal("secret persisted")
		}
	}
}
func TestUnsupportedCapabilitiesHonest(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	caps, _ := d.Capabilities()
	if caps.SendSignals || caps.Exec {
		t.Fatal("advertised unsupported capability")
	}
	if _, e := d.TaskStats(t.Context(), "task", time.Second); e == nil {
		t.Fatal("fabricated metrics")
	}
}

func TestStateLockAndSymlinkFailClosed(t *testing.T) {
	root := filepath.Join(t.TempDir(), "state")
	s, e := NewStore(root)
	if e != nil {
		t.Fatal(e)
	}
	key := taskKey("a", "b")
	unlock, e := s.Lock(key)
	if e != nil {
		t.Fatal(e)
	}
	if other, e := s.Lock(key); e == nil {
		other()
		t.Fatal("concurrent action lock accepted")
	}
	unlock()
	target := filepath.Join(t.TempDir(), "outside")
	_ = os.WriteFile(target, []byte(`{}`), 0600)
	if e = os.Symlink(target, filepath.Join(root, key+".json")); e != nil {
		t.Fatal(e)
	}
	if _, e = s.Read(key); e == nil {
		t.Fatal("followed external state symlink")
	}
}
func TestUnacknowledgedCreateAbsentNeverReposted(t *testing.T) {
	f := newRuntime(t)
	f.loseCreate = true
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	_, _, _ = d.StartTask(cfg)
	f.mu.Lock()
	delete(f.boxes, "vm1")
	f.mu.Unlock() // Simulate an action whose commit cannot be found, not permission to replay.
	if _, _, e := d.StartTask(cfg); e == nil {
		t.Fatal("missing uncertain create was replayed")
	}
	c, _, _, _ := f.counts()
	if c != 1 {
		t.Fatal("uncertain POST duplicated")
	}
}
func TestNodeCredentialPermissionsRejected(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	p := filepath.Join(t.TempDir(), "bad-key")
	_ = os.WriteFile(p, []byte("canary"), 0644)
	if e := d.configure(Config{Endpoint: f.server.URL, StateDir: d.config.StateDir, APIKeyFile: p, PolicyEndpoint: f.server.URL, PolicyNamespace: "boxlite-prime", PolicyPath: "nomad/jobs"}); e == nil {
		t.Fatal("world-readable node credential accepted")
	}
}

// Task argv must replace BOTH image entrypoint and cmd. Otherwise an inherited
// application bootstrap receives the synthetic command as its own arguments.
func TestExplicitGuestArgvOverridesImageEntrypoint(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	c := taskConfig(t)
	tc, s, e := d.validate(c)
	if e != nil {
		t.Fatal(e)
	}
	if len(s.Entrypoint) != 1 || s.Entrypoint[0] != tc.Command[0] {
		t.Fatalf("entrypoint not overridden: %+v", s)
	}
	if strings.Join(s.Command, "\x00") != strings.Join(tc.Command[1:], "\x00") {
		t.Fatal("cmd argv changed")
	}
	b, e := json.Marshal(s)
	if e != nil {
		t.Fatal(e)
	}
	var wire map[string]json.RawMessage
	if e = json.Unmarshal(b, &wire); e != nil {
		t.Fatal(e)
	}
	if _, ok := wire["entrypoint"]; !ok {
		t.Fatal("entrypoint missing on REST wire")
	}
}

func TestExternalStdioBindingAndRecoveryDoesNotStealStream(t *testing.T) {
	f := newRuntime(t)
	root := t.TempDir()
	d := setupDriver(t, f, filepath.Join(root, "state"))
	d.config.AllowExternalStdio = true
	c := taskConfig(t)
	c.AllocDir = filepath.Join(root, "alloc")
	if e := os.MkdirAll(filepath.Join(c.AllocDir, "alloc"), 0700); e != nil {
		t.Fatal(e)
	}
	tc, _, _ := d.validate(c)
	tc.StdioMode = "external"
	if e := c.EncodeConcreteDriverConfig(tc); e != nil {
		t.Fatal(e)
	}
	h := started(t, d, c)
	s := stateOf(t, h)
	f.awaitDetached(t, s.VMID)
	binding, e := os.ReadFile(filepath.Join(c.AllocDir, "alloc", "boxlite-"+c.Name+".json"))
	if e != nil {
		t.Fatal(e)
	}
	var v map[string]string
	if json.Unmarshal(binding, &v) != nil || v["vm_id"] != s.VMID || v["allocation_id"] != c.AllocID {
		t.Fatal("public binding mismatch")
	}
	_ = d.Shutdown(t.Context())
	next := setupDriver(t, f, filepath.Join(root, "state"))
	next.config.AllowExternalStdio = true
	if e = next.RecoverTask(h); e != nil {
		t.Fatal(e)
	}
	f.mu.Lock()
	n := 0
	for _, r := range f.requests {
		if strings.Contains(r, "/attach") {
			n++
		}
	}
	f.mu.Unlock()
	if n != 1 {
		t.Fatalf("external recovery stole application stream, attaches=%d", n)
	}
	if e = next.StopTask(c.ID, time.Second, ""); e != nil {
		t.Fatal(e)
	}
}
func TestExternalStdioAndRestrictedNetworkRequireOperatorGrant(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	c := taskConfig(t)
	tc, _, _ := d.validate(c)
	tc.StdioMode = "external"
	c.EncodeConcreteDriverConfig(tc)
	if _, _, e := d.validate(c); e == nil {
		t.Fatal("ungranted external stdio")
	}
	tc.StdioMode = "driver"
	tc.RestrictedNetwork = true
	c.EncodeConcreteDriverConfig(tc)
	if _, _, e := d.validate(c); e == nil {
		t.Fatal("empty node network allowlist")
	}
	d.config.OutboundAllowNet = []string{"example.net:443"}
	_, s, e := d.validate(c)
	if e != nil {
		t.Fatal(e)
	}
	if s.Network.Inbound.Mode != "disabled" || s.Network.Outbound.Mode != "enabled" || len(s.Network.Outbound.AllowNet) != 1 {
		t.Fatal("operator policy not preserved")
	}
}

func TestNodeCredentialCommandIsPrivateAndFailClosed(t *testing.T) {
	t.Setenv("UNRELATED_SECRET", "must-not-be-forwarded")
	f := newRuntime(t)
	d := New(context.Background(), log.NewNullLogger())
	defer d.Shutdown(t.Context())
	cfg := Config{Endpoint: f.server.URL, StateDir: filepath.Join(t.TempDir(), "state"), PolicyEndpoint: f.server.URL, PolicyNamespace: "boxlite-prime", PolicyPath: "nomad/jobs", APIKeyCommand: []string{"/bin/sh", "-c", `test -z "$UNRELATED_SECRET" || exit 9; printf node-provider-fixture-key`}}
	if e := d.configure(cfg); e != nil {
		t.Fatal(e)
	}
	c := taskConfig(t)
	h := started(t, d, c)
	raw, _ := json.Marshal(h)
	if strings.Contains(string(raw), "node-provider-fixture-key") {
		t.Fatal("credential leaked into handle")
	}
	bad := New(context.Background(), log.NewNullLogger())
	defer bad.Shutdown(t.Context())
	cfg.StateDir = filepath.Join(t.TempDir(), "state")
	cfg.APIKeyCommand = []string{"/bin/sh", "-c", "printf sensitive-error >&2; exit 7"}
	if e := bad.configure(cfg); e == nil || strings.Contains(e.Error(), "sensitive-error") {
		t.Fatal("provider failure not private/fail closed")
	}
}

func TestSingleAllocationFenceSurvivesDriverRestart(t *testing.T) {
	f := newRuntime(t)
	stateDir := filepath.Join(t.TempDir(), "state")
	d := setupDriver(t, f, stateDir)
	cfg := taskConfig(t)
	cfg.Namespace = "sessions"
	cfg.JobID = "worker/dispatch-123-abc"
	cfg.TaskGroupName = "vm"
	var tc TaskConfig
	if err := cfg.DecodeDriverConfig(&tc); err != nil {
		t.Fatal(err)
	}
	tc.SingleAllocation = true
	if err := cfg.EncodeConcreteDriverConfig(tc); err != nil {
		t.Fatal(err)
	}
	started(t, d, cfg)
	// The fence remains when the first driver is gone; a replacement alloc must
	// not reach BoxLite POST, including after the original VM has stopped.
	d.cancel()
	d2 := setupDriver(t, f, stateDir)
	replacement := cfg.Copy()
	replacement.ID = "replacement-task"
	replacement.AllocID = "replacement-allocation"
	if _, _, err := d2.StartTask(replacement); err == nil || !strings.Contains(err.Error(), "original allocation allocation-a") {
		t.Fatalf("replacement not fenced: %v", err)
	}
	f.mu.Lock()
	n := f.creates
	f.mu.Unlock()
	if n != 1 {
		t.Fatalf("duplicate VM creates=%d", n)
	}
	if err := d2.claimAllocation(cfg, tc); err != nil {
		t.Fatalf("original identity rejected: %v", err)
	}
	other := cfg.Copy()
	other.JobID = "worker/dispatch-456-def"
	other.AllocID = "independent-allocation"
	if err := d2.claimAllocation(other, tc); err != nil {
		t.Fatalf("independent dispatch rejected: %v", err)
	}
}

func TestSingleAllocationMissingIdentityFailsBeforeCreate(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	var tc TaskConfig
	_ = cfg.DecodeDriverConfig(&tc)
	tc.SingleAllocation = true
	_ = cfg.EncodeConcreteDriverConfig(tc)
	if _, _, err := d.StartTask(cfg); err == nil {
		t.Fatal("missing logical identity accepted")
	}
	if f.creates != 0 {
		t.Fatal("VM created before identity check")
	}
}

func TestLogicalSessionAcrossDispatchIDsIsFenced(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	cfg.Namespace = "sessions"
	cfg.JobID = "worker/dispatch-first"
	cfg.TaskGroupName = "vm"
	var tc TaskConfig
	_ = cfg.DecodeDriverConfig(&tc)
	tc.SingleAllocation = true
	tc.LogicalID = "shared-session-uuid"
	_ = cfg.EncodeConcreteDriverConfig(tc)
	if err := d.claimAllocation(cfg, tc); err != nil {
		t.Fatal(err)
	}
	other := cfg.Copy()
	other.JobID = "worker/dispatch-second"
	other.AllocID = "different-alloc"
	if err := d.claimAllocation(other, tc); err == nil {
		t.Fatal("same logical session accepted under another dispatch")
	}
	if f.creates != 0 {
		t.Fatal("claim issued VM create")
	}
}

func TestOperatorFullEgressIsExplicitAndInboundStillDisabled(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	c := taskConfig(t)
	var tc TaskConfig
	if e := c.DecodeDriverConfig(&tc); e != nil {
		t.Fatal(e)
	}
	tc.RestrictedNetwork = true
	if e := c.EncodeConcreteDriverConfig(tc); e != nil {
		t.Fatal(e)
	}
	d.config.OutboundAllowAll = true
	_, spec, e := d.validate(c)
	if e != nil {
		t.Fatal(e)
	}
	if spec.Network.Outbound.Mode != "enabled" || len(spec.Network.Outbound.AllowNet) != 0 || spec.Network.Inbound.Mode != "disabled" {
		t.Fatal("operator full egress not mapped to native policy")
	}
	d.config.OutboundAllowAll = false
	if _, _, e = d.validate(c); e == nil {
		t.Fatal("empty policy silently became unrestricted")
	}
}
