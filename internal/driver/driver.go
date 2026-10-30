// Package driver is a generic Nomad-to-BoxLite bridge, not an agent controller.
package driver

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/hashicorp/nomad/client/lib/cpustats"
	"io"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"sync"
	"syscall"
	"time"

	log "github.com/hashicorp/go-hclog"
	cstructs "github.com/hashicorp/nomad/client/structs"
	"github.com/hashicorp/nomad/plugins/base"
	"github.com/hashicorp/nomad/plugins/drivers"
	"github.com/hashicorp/nomad/plugins/drivers/fsisolation"
	"github.com/hashicorp/nomad/plugins/shared/hclspec"
	pstructs "github.com/hashicorp/nomad/plugins/shared/structs"
	"nomad-driver-boxlite/internal/boxlite"
)

const Version = "0.1.0-prototype"

type Config struct {
	GuestFeatures      []string `codec:"guest_features"`
	RuntimeStorageDir  string   `codec:"runtime_storage_dir"`
	DiskFreeFloorMiB   int      `codec:"disk_free_floor_mib"`
	Endpoint           string   `codec:"endpoint"`
	StateDir           string   `codec:"state_dir"`
	APIKeyFile         string   `codec:"api_key_file"`
	APIKeyCommand      []string `codec:"api_key_command"`
	StartTimeout       string   `codec:"start_timeout"`
	OutboundAllowNet   []string `codec:"outbound_allow_net"`
	OutboundAllowAll   bool     `codec:"outbound_allow_all"`
	AllowExternalStdio bool     `codec:"allow_external_stdio"`
	PolicyEndpoint     string   `codec:"policy_endpoint"`
	PolicyNamespace    string   `codec:"policy_namespace"`
	PolicyPath         string   `codec:"policy_path"`
	CompanionCommand   []string `codec:"companion_command"`
	GuestBootstrapFile string   `codec:"guest_bootstrap_file"`
}
type TaskConfig struct {
	Image             string   `codec:"image"`
	Command           []string `codec:"command"`
	CPUs              int      `codec:"guest_cpus"`
	MemoryMiB         int      `codec:"memory_mib"`
	DiskGiB           int      `codec:"disk_gib"`
	StdioMode         string   `codec:"stdio_mode"`
	RestrictedNetwork bool     `codec:"restricted_network"`
	SingleAllocation  bool     `codec:"single_allocation"`
	LogicalID         string   `codec:"logical_id"`
	WorkerRequest     string   `codec:"worker_request"`
	specID            string
}
type task struct {
	mu               sync.Mutex
	config           *drivers.TaskConfig
	state            *State
	phase, lastError string
	stats            statsDiagnostics
	exit             *drivers.ExitResult
	done             chan struct{}
	cancel           context.CancelFunc
	stream           *boxlite.Stream
	outputDone       chan struct{}
	nativeExit       *int
	stdout, stderr   io.WriteCloser
}
type Driver struct {
	drivers.DriverExecTaskNotSupported
	logger                     log.Logger
	ctx                        context.Context
	cancel                     context.CancelFunc
	mu                         sync.Mutex
	tasks                      map[string]*task
	client                     *boxlite.Client
	store                      *Store
	config                     Config
	startTimeout, pollInterval time.Duration
	guestCommand               []string
	compute                    cpustats.Compute
	eventsMu                   sync.Mutex
	history                    []*drivers.TaskEvent
	subscribers                map[chan *drivers.TaskEvent]bool
}

var _ drivers.DriverPlugin = (*Driver)(nil)

func New(ctx context.Context, l log.Logger) *Driver {
	ctx, cancel := context.WithCancel(ctx)
	return &Driver{logger: l, ctx: ctx, cancel: cancel, tasks: map[string]*task{}, subscribers: map[chan *drivers.TaskEvent]bool{}, pollInterval: time.Second}
}
func (d *Driver) PluginInfo() (*base.PluginInfoResponse, error) {
	return &base.PluginInfoResponse{Type: base.PluginTypeDriver, PluginApiVersions: []string{"0.1.0"}, PluginVersion: Version, Name: "boxlite"}, nil
}
func (d *Driver) ConfigSchema() (*hclspec.Spec, error) {
	return hclspec.NewObject(map[string]*hclspec.Spec{
		"guest_features":      hclspec.NewAttr("guest_features", "list(string)", false),
		"runtime_storage_dir": hclspec.NewAttr("runtime_storage_dir", "string", false), "disk_free_floor_mib": hclspec.NewAttr("disk_free_floor_mib", "number", false),
		"endpoint": hclspec.NewAttr("endpoint", "string", true), "state_dir": hclspec.NewAttr("state_dir", "string", true), "api_key_file": hclspec.NewAttr("api_key_file", "string", false), "api_key_command": hclspec.NewAttr("api_key_command", "list(string)", false),
		"start_timeout": hclspec.NewAttr("start_timeout", "string", false), "outbound_allow_net": hclspec.NewAttr("outbound_allow_net", "list(string)", false), "outbound_allow_all": hclspec.NewAttr("outbound_allow_all", "bool", false), "allow_external_stdio": hclspec.NewAttr("allow_external_stdio", "bool", false), "policy_endpoint": hclspec.NewAttr("policy_endpoint", "string", true), "policy_namespace": hclspec.NewAttr("policy_namespace", "string", true), "policy_path": hclspec.NewAttr("policy_path", "string", true), "companion_command": hclspec.NewAttr("companion_command", "list(string)", false), "guest_bootstrap_file": hclspec.NewAttr("guest_bootstrap_file", "string", false)}), nil
}
func (d *Driver) TaskConfigSchema() (*hclspec.Spec, error) {
	return hclspec.NewObject(map[string]*hclspec.Spec{
		"image": hclspec.NewAttr("image", "string", true), "command": hclspec.NewAttr("command", "list(string)", false), "guest_cpus": hclspec.NewAttr("guest_cpus", "number", true), "memory_mib": hclspec.NewAttr("memory_mib", "number", true), "disk_gib": hclspec.NewAttr("disk_gib", "number", true), "stdio_mode": hclspec.NewAttr("stdio_mode", "string", false), "restricted_network": hclspec.NewAttr("restricted_network", "bool", false), "single_allocation": hclspec.NewAttr("single_allocation", "bool", false), "logical_id": hclspec.NewAttr("logical_id", "string", false), "worker_request": hclspec.NewAttr("worker_request", "string", false)}), nil
}
func (d *Driver) SetConfig(c *base.Config) error {
	if c.AgentConfig != nil {
		d.compute = c.AgentConfig.Compute()
	}
	var cfg Config
	if e := base.MsgPackDecode(c.PluginConfig, &cfg); e != nil {
		return e
	}
	return d.configure(cfg)
}
func (d *Driver) configure(cfg Config) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if len(d.tasks) > 0 {
		return fmt.Errorf("cannot reconfigure while tasks are owned")
	}

	for _, feature := range cfg.GuestFeatures {
		switch feature {
		case "docker", "tun", "netem", "writable-cgroups":
		default:
			return fmt.Errorf("unsupported verified guest feature")
		}
	}
	if cfg.DiskFreeFloorMiB < 0 || (cfg.RuntimeStorageDir == "" && cfg.DiskFreeFloorMiB != 0) {
		return fmt.Errorf("disk free floor requires explicit runtime storage directory")
	}
	if cfg.RuntimeStorageDir != "" {
		canonical, e := filepath.EvalSymlinks(cfg.RuntimeStorageDir)
		if e != nil || !filepath.IsAbs(cfg.RuntimeStorageDir) || canonical != filepath.Clean(cfg.RuntimeStorageDir) {
			return fmt.Errorf("runtime storage directory must be existing absolute canonical path")
		}
	}
	if cfg.OutboundAllowAll && len(cfg.OutboundAllowNet) > 0 {
		return fmt.Errorf("choose operator allow-all or allowlist, not both")
	}
	timeout := 15 * time.Minute
	if cfg.StartTimeout != "" {
		v, e := time.ParseDuration(cfg.StartTimeout)
		if e != nil || v <= 0 {
			return fmt.Errorf("invalid start_timeout")
		}
		timeout = v
	}
	key := ""
	if cfg.APIKeyFile != "" && len(cfg.APIKeyCommand) > 0 {
		return fmt.Errorf("choose one node credential provider")
	}
	if len(cfg.APIKeyCommand) > 0 {
		if len(cfg.APIKeyCommand) > 16 || !filepath.IsAbs(cfg.APIKeyCommand[0]) {
			return fmt.Errorf("operator credential command requires absolute executable")
		}
		ctx, cancel := context.WithTimeout(d.ctx, 12*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, cfg.APIKeyCommand[0], cfg.APIKeyCommand[1:]...)
		cmd.Env = []string{"HOME=" + os.Getenv("HOME"), "PATH=/opt/homebrew/bin:/usr/bin:/bin"}
		cmd.Stderr = io.Discard
		pipe, e := cmd.StdoutPipe()
		if e != nil {
			return fmt.Errorf("node credential provider unavailable")
		}
		if e = cmd.Start(); e != nil {
			return fmt.Errorf("node credential provider unavailable")
		}
		raw, e := io.ReadAll(io.LimitReader(pipe, 8193))
		if len(raw) > 8192 {
			cmd.Process.Kill()
		}
		wait := cmd.Wait()
		if e != nil || wait != nil || len(raw) > 8192 {
			return fmt.Errorf("node credential provider failed; no credential cached")
		}
		key = string(bytes.TrimSpace(raw))
		if key == "" {
			return fmt.Errorf("node credential provider returned empty key")
		}
	}
	if cfg.APIKeyFile != "" {
		s, e := os.Lstat(cfg.APIKeyFile)
		if e != nil {
			return fmt.Errorf("API key file unavailable")
		}
		if !s.Mode().IsRegular() || s.Mode().Perm() != 0600 || s.Sys().(*syscall.Stat_t).Uid != uint32(os.Getuid()) {
			return fmt.Errorf("API key file must be owned regular0600, no symlink")
		}
		b, e := os.ReadFile(cfg.APIKeyFile)
		if e != nil {
			return fmt.Errorf("API key file unreadable")
		}
		key = string(b)
		key = regexp.MustCompile(`\s+$`).ReplaceAllString(key, "")
		if key == "" {
			return fmt.Errorf("empty API key file")
		}
	}
	client, e := boxlite.New(cfg.Endpoint, key, timeout)
	if e != nil {
		return e
	}
	store, e := NewStore(cfg.StateDir)
	if e != nil {
		return e
	}
	if e := d.configurePolicy(cfg); e != nil {
		return e
	}
	d.client = client
	d.store = store
	d.config = cfg
	d.startTimeout = timeout
	return nil
}
func (d *Driver) Capabilities() (*drivers.Capabilities, error) {
	return &drivers.Capabilities{SendSignals: false, Exec: false, FSIsolation: fsisolation.Image, NetIsolationModes: []drivers.NetIsolationMode{drivers.NetIsolationModeHost}, MountConfigs: drivers.MountConfigSupportNone}, nil
}
func (d *Driver) Fingerprint(ctx context.Context) (<-chan *drivers.Fingerprint, error) {
	out := make(chan *drivers.Fingerprint)
	go func() {
		defer close(out)
		tick := time.NewTicker(30 * time.Second)
		defer tick.Stop()
		for {
			fp := &drivers.Fingerprint{Attributes: map[string]*pstructs.Attribute{"driver.boxlite.api": pstructs.NewStringAttribute("v1"), "driver.boxlite.prototype": pstructs.NewBoolAttribute(true)}, Health: drivers.HealthStateUnhealthy}
			if d.client == nil {
				fp.HealthDescription = "node-local BoxLite configuration missing"
			} else {
				c, cancel := context.WithTimeout(ctx, 5*time.Second)
				e := nativeHostHealth()
				if e == nil {
					e = d.client.Probe(c)
				}
				if e == nil {
					enabled, discoveryErr := d.client.GuestPrivilegedDefault(c)
					fp.Attributes["driver.boxlite.guest.privileged"] = pstructs.NewBoolAttribute(discoveryErr == nil && enabled)
					if discoveryErr == nil && enabled {
						// Operator declares only features verified against its pinned
						// firmware; the runtime must independently confirm privilege.
						for _, feature := range d.config.GuestFeatures {
							fp.Attributes["driver.boxlite.guest."+feature] = pstructs.NewBoolAttribute(true)
						}
					}
				}
				cancel()
				if e == nil {
					fp.Health = drivers.HealthStateHealthy
					fp.HealthDescription = "authenticated node-local BoxLite REST reachable (runtime version must be pinned by bootstrap)"
				} else {
					fp.HealthDescription = e.Error()
				}
			}
			select {
			case out <- fp:
			case <-ctx.Done():
				return
			case <-d.ctx.Done():
				return
			}
			select {
			case <-tick.C:
			case <-ctx.Done():
				return
			case <-d.ctx.Done():
				return
			}
		}
	}()
	return out, nil
}
func (d *Driver) emit(t *task, phase string, err error) {
	e := &drivers.TaskEvent{Timestamp: time.Now().UTC(), Message: "boxlite " + phase, Annotations: map[string]string{"layer": "boxlite", "phase": phase}}
	if t != nil {
		e.TaskID = t.config.ID
		e.TaskName = t.config.Name
		e.AllocID = t.config.AllocID
		e.Annotations["vm_id"] = t.state.VMID
	}
	if err != nil {
		e.Message += "; " + d.client.Redact(err.Error())
		var api *boxlite.APIError
		if errors.As(err, &api) {
			e.Annotations["code"] = api.Code
			e.Annotations["request_id"] = api.RequestID
			e.Annotations["http_status"] = strconv.Itoa(api.Status)
			e.Annotations["uncertain"] = strconv.FormatBool(api.Uncertain)
		}
	}
	d.eventsMu.Lock()
	defer d.eventsMu.Unlock()
	d.history = append(d.history, e)
	if len(d.history) > 64 {
		d.history = d.history[len(d.history)-64:]
	}
	for ch := range d.subscribers {
		select {
		case ch <- e:
		default:
		}
	}
}
func (d *Driver) TaskEvents(ctx context.Context) (<-chan *drivers.TaskEvent, error) {
	ch := make(chan *drivers.TaskEvent, 128)
	d.eventsMu.Lock()
	for _, e := range d.history {
		ch <- e
	}
	d.subscribers[ch] = true
	d.eventsMu.Unlock()
	go func() {
		select {
		case <-ctx.Done():
		case <-d.ctx.Done():
		}
		d.eventsMu.Lock()
		delete(d.subscribers, ch)
		close(ch)
		d.eventsMu.Unlock()
	}()
	return ch, nil
}
func (d *Driver) get(id string) (*task, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	t := d.tasks[id]
	if t == nil {
		return nil, drivers.ErrTaskNotFound
	}
	return t, nil
}
func (d *Driver) own(ctx context.Context, s *State) (*boxlite.Box, error) {
	if s.OriginHash != digest(d.client.Origin()) || s.VMID == "" {
		return nil, fmt.Errorf("task authority or VM identity unavailable; no action")
	}
	b, e := d.client.Get(ctx, s.VMID)
	if e != nil {
		return nil, e
	}
	if b.ID != s.VMID || b.Name != s.Name || b.Image != s.Image || b.CPUs != s.CPUs || b.MemoryMiB != s.MemoryMiB {
		return nil, fmt.Errorf("VM ownership/spec mismatch; refusing action")
	}
	return b, nil
}
func (d *Driver) validate(cfg *drivers.TaskConfig) (TaskConfig, boxlite.Create, error) {
	var tc TaskConfig
	if e := cfg.DecodeDriverConfig(&tc); e != nil {
		return tc, boxlite.Create{}, e
	}
	if len(d.config.CompanionCommand) > 0 {
		if len(tc.Command) > 0 {
			return tc, boxlite.Create{}, fmt.Errorf("guest bootstrap command is operator-fixed")
		}
		tc.Command = append([]string{}, d.guestCommand...)
	}
	if cfg.ID == "" || cfg.AllocID == "" || cfg.Name == "" || cfg.User != "" || len(cfg.Mounts) > 0 || len(cfg.Devices) > 0 {
		return tc, boxlite.Create{}, fmt.Errorf("task identity required; host mounts/devices unsupported")
	}
	if !regexp.MustCompile(`^[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}$`).MatchString(tc.Image) || len(tc.Command) == 0 || len(tc.Command) > 128 || tc.CPUs < 1 || tc.CPUs > 255 || tc.MemoryMiB < 1 || tc.DiskGiB < 1 {
		return tc, boxlite.Create{}, fmt.Errorf("pinned image, explicit guest command and valid resources required")
	}
	for _, a := range tc.Command {
		if len(a) > 65536 {
			return tc, boxlite.Create{}, fmt.Errorf("guest argv limit")
		}
	}
	if cfg.Resources == nil || cfg.Resources.NomadResources == nil || int(cfg.Resources.NomadResources.Memory.MemoryMB) < tc.MemoryMiB || int(cfg.Resources.NomadResources.Cpu.CpuShares) < 1 {
		return tc, boxlite.Create{}, fmt.Errorf("Nomad reservation does not cover configured guest resources/accounting overhead")
	}
	n := boxlite.Direction{Mode: "disabled", AllowNet: []string{}}
	if tc.StdioMode == "" {
		tc.StdioMode = "driver"
	}
	if tc.StdioMode != "driver" && tc.StdioMode != "external" {
		return tc, boxlite.Create{}, fmt.Errorf("invalid stdio_mode")
	}
	if tc.StdioMode == "external" && (!d.config.AllowExternalStdio || cfg.AllocDir == "") {
		return tc, boxlite.Create{}, fmt.Errorf("external stdio requires explicit operator permission and Nomad allocation directory")
	}
	spec := boxlite.Create{Image: tc.Image, CPUs: tc.CPUs, MemoryMiB: tc.MemoryMiB, DiskGiB: tc.DiskGiB, Entrypoint: []string{tc.Command[0]}, Command: append([]string{}, tc.Command[1:]...), Network: boxlite.Network{Outbound: n, Inbound: n}, AutoResume: false, AutoDelete: 0}
	if tc.RestrictedNetwork {
		if !d.config.OutboundAllowAll && len(d.config.OutboundAllowNet) == 0 {
			return tc, boxlite.Create{}, fmt.Errorf("operator outbound allowlist absent")
		}
		rules := append([]string{}, d.config.OutboundAllowNet...)
		if d.config.OutboundAllowAll {
			rules = []string{}
		}
		spec.Network.Outbound = boxlite.Direction{Mode: "enabled", AllowNet: rules}
	}
	if len(d.config.CompanionCommand) > 0 {
		if e := d.validateCompanion(cfg, &tc); e != nil {
			return tc, boxlite.Create{}, e
		}
	}
	return tc, spec, nil
}
func (d *Driver) handle(t *task) (*drivers.TaskHandle, error) {
	h := drivers.NewTaskHandle(1)
	h.Config = t.config.Copy()
	h.Config.Env = nil
	h.Config.DeviceEnv = nil
	h.State = drivers.TaskStateRunning
	if t.exit != nil {
		h.State = drivers.TaskStateExited
	}
	s := *t.state
	if e := h.SetDriverState(&s); e != nil {
		return nil, e
	}
	return h, nil
}
func openOutput(path string) (io.WriteCloser, error) {
	if path == "" {
		return nil, fmt.Errorf("Nomad output path required")
	}
	return os.OpenFile(path, os.O_WRONLY|os.O_APPEND|syscall.O_NONBLOCK, 0)
}
func (d *Driver) StartTask(cfg *drivers.TaskConfig) (handle *drivers.TaskHandle, network *drivers.DriverNetwork, startErr error) {
	if d.client == nil {
		return nil, nil, fmt.Errorf("driver not configured")
	}
	tc, spec, e := d.validate(cfg)
	if e != nil {
		return nil, nil, e
	}
	if e := d.claimAllocation(cfg, tc); e != nil {
		return nil, nil, e
	}
	rawSpec, _ := json.Marshal(struct {
		Create        boxlite.Create
		StdioMode     string
		WorkerRequest string
	}{spec, tc.StdioMode, tc.WorkerRequest})
	wantedHash := digest(string(rawSpec))
	immutableHash := immutableTaskHash(tc, spec)
	d.mu.Lock()
	existing := d.tasks[cfg.ID]
	d.mu.Unlock()
	if existing != nil {
		existing.mu.Lock()
		defer existing.mu.Unlock()
		if !matchesImmutableTask(existing.state, immutableHash, wantedHash) || existing.state.AllocID != cfg.AllocID || existing.state.TaskName != cfg.Name {
			return nil, nil, fmt.Errorf("duplicate task ID has different allocation/spec")
		}
		h, e := d.handle(existing)
		return h, nil, e
	}
	key := taskKey(cfg.AllocID, cfg.Name)
	unlock, e := d.store.Lock(key)
	if e != nil {
		return nil, nil, e
	}
	defer unlock()
	s, e := d.store.Read(key)
	fresh := os.IsNotExist(e)
	raw, _ := json.Marshal(struct {
		Create        boxlite.Create
		StdioMode     string
		WorkerRequest string
	}{spec, tc.StdioMode, tc.WorkerRequest})
	sh := digest(string(raw))
	if os.IsNotExist(e) {
		name, e := randomName(key)
		if e != nil {
			return nil, nil, e
		}
		s = &State{Version: 1, Key: key, TaskID: cfg.ID, AllocID: cfg.AllocID, TaskName: cfg.Name, Name: name, OriginHash: digest(d.client.Origin()), SpecHash: sh, ImmutableHash: immutableHash, Image: spec.Image, CPUs: spec.CPUs, MemoryMiB: spec.MemoryMiB, Phase: "intent", StdioMode: tc.StdioMode, StartupCleanupEligible: true}
		if e = d.reserveBudget(s, cfg, tc); e != nil {
			return nil, nil, e
		}
	} else if e != nil {
		return nil, nil, e
	}
	if s.StartupCleanupPending && !s.Destroyed && matchesImmutableTask(s, immutableHash, sh) && s.OriginHash == digest(d.client.Origin()) {
		t := &task{config: cfg.Copy(), state: s, done: make(chan struct{})}
		if err := d.cleanupFailedStartup(t); err != nil {
			return nil, nil, fmt.Errorf("startup cleanup remains pending: %w", err)
		}
		return nil, nil, fmt.Errorf("previous startup failed; resources released, no replay")
	}
	if s.Destroyed || !matchesImmutableTask(s, immutableHash, sh) || s.OriginHash != digest(d.client.Origin()) {
		return nil, nil, fmt.Errorf("durable task identity conflict; no new VM")
	}
	t := &task{config: cfg.Copy(), state: s, phase: s.Phase, done: make(chan struct{})}
	cleanupSafe := true
	defer func() {
		if startErr != nil && fresh && cleanupSafe {
			s.StartupCleanupPending = true
			if err := d.store.Write(s); err != nil {
				startErr = fmt.Errorf("%w; cleanup intent persistence failed: %v", startErr, err)
				return
			}
			if err := d.cleanupFailedStartup(t); err != nil {
				startErr = fmt.Errorf("%w; startup cleanup pending: %v", startErr, err)
			}
		}
	}()

	ctx, cancel := context.WithTimeout(d.ctx, d.startTimeout)
	defer cancel()
	if s.CreateIssued && s.VMID == "" {
		b, e := d.client.Get(ctx, s.Name)
		if e != nil {
			return nil, nil, fmt.Errorf("create outcome unresolved; no POST replay: %w", e)
		}
		if b.Name != s.Name || b.Image != s.Image || b.CPUs != s.CPUs || b.MemoryMiB != s.MemoryMiB {
			return nil, nil, fmt.Errorf("uncertain-create ownership mismatch")
		}
		s.VMID = b.ID
		if e = d.store.Write(s); e != nil {
			return nil, nil, e
		}
	}
	if !s.CreateIssued {
		if _, e = d.client.Get(ctx, s.Name); !boxlite.IsNotFound(e) {
			if e != nil {
				return nil, nil, fmt.Errorf("VM name absence unconfirmed; no create: %w", e)
			}
			return nil, nil, fmt.Errorf("private VM name already exists before create; refusing adoption")
		}
		s.CreateIssued = true
		s.Phase = "create_issued"
		if e = d.store.Write(s); e != nil {
			return nil, nil, e
		}
		spec.Name = s.Name
		d.emit(t, "create_issued", nil)
		cleanupSafe = false // a lost response cannot authorize destruction
		b, e := d.client.Create(ctx, spec)
		if e != nil {
			var api *boxlite.APIError
			if errors.As(e, &api) && !api.Uncertain && api.Status >= 400 && api.Status < 500 {
				s.StartupCreateRejected = true
				cleanupSafe = true
			}
			d.emit(t, "create_uncertain_or_failed", e)
			return nil, nil, e
		}
		if b.ID == "" || b.Name != s.Name || b.Image != s.Image || b.CPUs != s.CPUs || b.MemoryMiB != s.MemoryMiB {
			return nil, nil, fmt.Errorf("create response ownership mismatch")
		}
		s.VMID = b.ID
		cleanupSafe = true
		s.Phase = "created"
		if e = d.store.Write(s); e != nil {
			return nil, nil, e
		}
		d.emit(t, "created", nil)
	}
	b, e := d.own(ctx, s)
	if e != nil {
		return nil, nil, e
	}
	if s.StartIssued {
		return d.recoverKnown(t, b)
	}
	if b.Status != "configured" {
		return nil, nil, fmt.Errorf("never-started VM has unexpected status %s; no replay", b.Status)
	}
	if e = d.attach(ctx, t); e != nil {
		d.emit(t, "attach_boot_failed", e)
		return nil, nil, e
	}
	if s.StdioMode == "external" {
		d.closeOutput(t)
		t.stream = nil
		t.stdout = nil
		t.stderr = nil
	}
	s.StartIssued = true
	s.StartedAt = time.Now().UTC()
	s.Phase = "start_issued"
	if e = d.store.Write(s); e != nil {
		d.closeOutput(t)
		return nil, nil, e
	}
	d.emit(t, "start_issued", nil)
	cleanupSafe = false
	_, e = d.client.Start(ctx, s.VMID)
	if e != nil {
		d.closeOutput(t)
		d.emit(t, "start_outcome_uncertain", e)
		return nil, nil, e
	}
	cleanupSafe = true
	s.Phase = "running"
	if e = d.store.Write(s); e != nil {
		d.closeOutput(t)
		return nil, nil, e
	}
	if s.StdioMode == "external" {
		if e = d.publishBinding(t); e != nil {
			return nil, nil, e
		}
	}
	if e = d.startCompanion(t, tc); e != nil {
		d.emit(t, "companion_failed", e)
		return nil, nil, e
	}
	d.register(t)
	t.mu.Lock()
	h, e := d.handle(t)
	t.mu.Unlock()
	return h, nil, e
}
func (d *Driver) attach(ctx context.Context, t *task) error {
	if t.state.StdioMode == "external" {
		stream, e := d.client.Attach(ctx, t.state.VMID, t.state.ExecutionID)
		if e != nil {
			return e
		}
		t.stream = stream
		t.state.ExecutionID = stream.ExecutionID
		return nil
	}
	out, e := openOutput(t.config.StdoutPath)
	if e != nil {
		return e
	}
	errout, e := openOutput(t.config.StderrPath)
	if e != nil {
		out.Close()
		return e
	}
	stream, e := d.client.Attach(ctx, t.state.VMID, t.state.ExecutionID)
	if e != nil {
		out.Close()
		errout.Close()
		return e
	}
	t.stdout = out
	t.stderr = errout
	t.stream = stream
	t.state.ExecutionID = stream.ExecutionID
	return nil
}
func (d *Driver) closeOutput(t *task) {
	if t.stream != nil {
		t.stream.Conn.Close()
	}
	if t.stdout != nil {
		t.stdout.Close()
	}
	if t.stderr != nil {
		t.stderr.Close()
	}
}
func (d *Driver) register(t *task) {
	t.outputDone = make(chan struct{})
	t.phase = t.state.Phase
	d.mu.Lock()
	d.tasks[t.config.ID] = t
	d.mu.Unlock()
	ctx, cancel := context.WithCancel(d.ctx)
	t.cancel = cancel
	d.emit(t, "vm_running_not_application_ready", nil)
	if t.stream == nil {
		close(t.outputDone)
	}
	if t.stream != nil {
		go func() {
			defer close(t.outputDone)
			code, e := t.stream.Read(ctx, t.stdout, t.stderr)
			if e == nil {
				t.mu.Lock()
				t.nativeExit = &code
				t.mu.Unlock()
			}
			if e != nil && ctx.Err() == nil {
				t.mu.Lock()
				if t.exit != nil {
					t.mu.Unlock()
					return
				}
				t.lastError = e.Error()
				t.mu.Unlock()
				d.emit(t, "output_stream_lost", e)
			}
		}()
	}
	go func() {
		tick := time.NewTicker(d.pollInterval)
		defer tick.Stop()
		defer d.closeOutput(t)
		for {
			select {
			case <-ctx.Done():
				return
			case <-tick.C:
			}
			t.mu.Lock()
			c, stop := context.WithTimeout(ctx, 10*time.Second)
			b, e := d.own(c, t.state)
			stop()
			if e != nil {
				if ctx.Err() != nil {
					t.mu.Unlock()
					return
				}
				if t.lastError != e.Error() {
					t.lastError = e.Error()
					d.emit(t, "observation_unavailable_vm_state_unknown", e)
				}
				t.mu.Unlock()
				continue
			}
			t.lastError = ""
			if t.state.CompanionIssued {
				d.publishTerminalSummary(t)
				if t.state.StartupCleanupEligible && bootstrapFailureBeforeACP(t.state.TerminalSummary) && d.noApplicationReadyReceipt(t) {
					unlock, err := d.store.Lock(t.state.Key)
					if err == nil {
						t.state.StartupCleanupPending = true
						err = d.store.Write(t.state)
						if err == nil {
							err = d.cleanupFailedStartup(t)
						}
						unlock()
						if err == nil {
							t.mu.Unlock()
							return
						}
					}
					d.emit(t, "bootstrap_cleanup_pending", err)
				}
			}
			if b.Status == "stopped" {
				t.mu.Unlock()
				select {
				case <-t.outputDone:
				case <-time.After(2 * time.Second):
					d.emit(t, "output_drain_unconfirmed", nil)
				case <-ctx.Done():
					return
				}
				t.mu.Lock()
				if e := d.stopCompanion(t); e != nil {
					d.emit(t, "companion_cleanup_unconfirmed", e)
				}
				d.finished(t, b)
				t.mu.Unlock()
				return
			}
			t.mu.Unlock()
		}
	}()
}

// Caller owns t.mu. Actual native exit code is required; no fabricated zero.
func (d *Driver) finished(t *task, b *boxlite.Box) {
	d.publishTerminalSummary(t)
	if t.exit != nil {
		return
	}
	r := &drivers.ExitResult{}
	if b.ExitCode == nil && t.nativeExit != nil {
		r.ExitCode = *t.nativeExit
	} else if b.ExitCode == nil {
		r.ExitCode = 1
		r.Err = fmt.Errorf("VM stopped; native command exit code unavailable")
	} else {
		r.ExitCode = *b.ExitCode
	}
	t.exit = r
	t.phase = "exited"
	t.state.Phase = "exited"
	close(t.done)
	d.emit(t, "vm_stopped", r.Err)
}
func (d *Driver) recoverKnown(t *task, b *boxlite.Box) (*drivers.TaskHandle, *drivers.DriverNetwork, error) {
	if t.state.Phase == "start_issued" {
		t.state.Phase = "start_outcome_uncertain"
		d.emit(t, "start_outcome_uncertain_no_replay", nil)
	}
	if b.Status == "configured" {
		return nil, nil, fmt.Errorf("start outcome unresolved; never replay start")
	}
	if b.Status == "stopped" {
		d.finished(t, b)
		d.mu.Lock()
		d.tasks[t.config.ID] = t
		d.mu.Unlock()
		h, e := d.handle(t)
		return h, nil, e
	}
	if b.Status != "running" {
		return nil, nil, fmt.Errorf("unsupported observed VM state %s", b.Status)
	}
	ctx, cancel := context.WithTimeout(d.ctx, 10*time.Second)
	defer cancel()
	if t.state.StdioMode != "external" {
		if e := d.attach(ctx, t); e != nil {
			return nil, nil, e
		}
	} else if e := d.publishBinding(t); e != nil {
		return nil, nil, e
	}
	if len(d.config.CompanionCommand) > 0 {
		_, alive, e := d.companionPID(t)
		if e != nil {
			return nil, nil, e
		}
		if !alive {
			if t.state.CompanionIssued {
				return nil, nil, fmt.Errorf("original companion lost; recovery will not start a replacement")
			}
			tc, _, err := d.validate(t.config)
			if err != nil {
				return nil, nil, fmt.Errorf("incomplete bootstrap recovery requires original driver config: %w", err)
			}
			if err = d.startCompanion(t, tc); err != nil {
				return nil, nil, err
			}
		}
	}
	d.register(t)
	t.mu.Lock()
	h, e := d.handle(t)
	t.mu.Unlock()
	return h, nil, e
}
func (d *Driver) RecoverTask(h *drivers.TaskHandle) error {
	if h == nil || h.Version != 1 || h.Config == nil {
		return fmt.Errorf("unsupported/missing recovery handle")
	}
	if _, e := d.get(h.Config.ID); e == nil {
		return nil
	}
	var hs State
	if e := h.GetDriverState(&hs); e != nil {
		return fmt.Errorf("recovery driver-state decode failed: %w", e)
	}
	key := taskKey(h.Config.AllocID, h.Config.Name)
	if hs.Key != key {
		return fmt.Errorf("recovery allocation binding mismatch")
	}
	unlock, e := d.store.Lock(key)
	if e != nil {
		return e
	}
	defer unlock()
	s, e := d.store.Read(key)
	if e != nil {
		return e
	}
	if s.Destroyed || s.TaskID != h.Config.ID || s.AllocID != h.Config.AllocID || s.TaskName != h.Config.Name || hs.VMID != s.VMID || hs.Name != s.Name || hs.SpecHash != s.SpecHash {
		return fmt.Errorf("recovery durable ownership mismatch")
	}
	if s.StartupCleanupPending {
		t := &task{config: h.Config.Copy(), state: s, done: make(chan struct{})}
		if err := d.cleanupFailedStartup(t); err != nil {
			return fmt.Errorf("recovery startup cleanup pending: %w", err)
		}
		return fmt.Errorf("startup failed; cleanup completed without replay")
	}
	ctx, cancel := context.WithTimeout(d.ctx, 10*time.Second)
	defer cancel()
	b, e := d.own(ctx, s)
	if e != nil {
		return e
	}
	t := &task{config: h.Config.Copy(), state: s, phase: s.Phase, done: make(chan struct{})}
	_, _, e = d.recoverKnown(t, b)
	if e == nil {
		d.emit(t, "recovered_existing_vm_no_start", nil)
	}
	return e
}
func (d *Driver) WaitTask(ctx context.Context, id string) (<-chan *drivers.ExitResult, error) {
	t, e := d.get(id)
	if e != nil {
		return nil, e
	}
	out := make(chan *drivers.ExitResult, 1)
	go func() {
		defer close(out)
		select {
		case <-t.done:
			if t.outputDone != nil {
				select {
				case <-t.outputDone:
				case <-ctx.Done():
					return
				case <-d.ctx.Done():
					return
				}
			}
			t.mu.Lock()
			out <- t.exit.Copy()
			t.mu.Unlock()
		case <-ctx.Done():
		case <-d.ctx.Done():
		}
	}()
	return out, nil
}
func (d *Driver) InspectTask(id string) (*drivers.TaskStatus, error) {
	t, e := d.get(id)
	if e != nil {
		return nil, e
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	s := drivers.TaskStateRunning
	if t.exit != nil {
		s = drivers.TaskStateExited
	}
	attrs := map[string]string{"vm_id": t.state.VMID, "execution_id": t.state.ExecutionID, "phase": t.phase, "runtime_observation_error": t.lastError, "application_readiness": "not provided by generic driver", "metrics_scope": "host_vm_shim_cpu_and_rss; guest_used_ram_unavailable; shared_runtime_and_companion_excluded"}
	for key, value := range t.stats.attributes() {
		attrs[key] = value
	}
	return &drivers.TaskStatus{ID: id, Name: t.config.Name, State: s, StartedAt: t.state.StartedAt, ExitResult: t.exit.Copy(), DriverAttributes: attrs}, nil
}
func (d *Driver) StopTask(id string, timeout time.Duration, signal string) error {
	if signal != "" && signal != "SIGTERM" {
		return fmt.Errorf("custom kill signal unsupported by native VM-stop mapping")
	}
	t, e := d.get(id)
	if e != nil {
		return e
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	unlock, e := d.store.Lock(t.state.Key)
	if e != nil {
		return e
	}
	defer unlock()
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	ctx, cancel := context.WithTimeout(d.ctx, timeout)
	defer cancel()
	b, e := d.own(ctx, t.state)
	if e != nil {
		return e
	}
	if b.Status != "stopped" {
		d.emit(t, "stop_requested", nil)
		if _, e = d.client.Stop(ctx, t.state.VMID); e != nil {
			d.emit(t, "stop_unconfirmed", e)
			return e
		}
		b, e = d.own(ctx, t.state)
		if e != nil {
			return e
		}
	}
	if b.Status != "stopped" {
		return fmt.Errorf("stop acknowledged but VM not stopped; state unknown")
	}
	if e = d.stopCompanion(t); e != nil {
		return e
	}
	d.finished(t, b)
	t.state.Phase = "stopped"
	if e = d.store.Write(t.state); e != nil {
		return e
	}
	if t.cancel != nil {
		t.cancel()
	}
	return nil
}
func (d *Driver) DestroyTask(id string, force bool) error {
	t, e := d.get(id)
	if e != nil {
		return e
	}
	t.mu.Lock()
	running := t.exit == nil
	t.mu.Unlock()
	if running {
		if !force {
			return fmt.Errorf("refusing destroy while VM may be running")
		}
		if e = d.StopTask(id, 30*time.Second, ""); e != nil {
			return e
		}
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	unlock, e := d.store.Lock(t.state.Key)
	if e != nil {
		return e
	}
	defer unlock()
	t.state.Destroyed = true
	t.state.Phase = "destroyed_disks_preserved"
	if e = d.store.Write(t.state); e != nil {
		return e
	}
	if t.cancel != nil {
		t.cancel()
	}
	d.mu.Lock()
	delete(d.tasks, id)
	d.mu.Unlock()
	d.emit(t, "destroyed_disks_preserved", nil)
	return nil
}
func (d *Driver) SignalTask(id, signal string) error {
	return fmt.Errorf("cooperative signal API is not implemented; use native VM StopTask")
}
func (d *Driver) TaskStats(ctx context.Context, id string, interval time.Duration) (<-chan *cstructs.TaskResourceUsage, error) {
	t, err := d.get(id)
	if err != nil {
		return nil, err
	}
	if d.compute.NumCores <= 0 || d.compute.TotalCompute <= 0 {
		return nil, fmt.Errorf("host CPU compute unavailable; cannot report measured MHz")
	}
	if interval < 250*time.Millisecond {
		interval = time.Second
	}
	ch := make(chan *cstructs.TaskResourceUsage, 1)
	stream := d.statsStarted(t, interval)
	go func() {
		defer close(ch)
		reason := "stream_finished"
		defer func() { d.statsStopped(t, stream, reason) }()
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		warmed, delivered := false, false
		for {
			select {
			case <-ctx.Done():
				reason = "consumer_context_cancelled"
				return
			case <-d.ctx.Done():
				reason = "driver_shutdown"
				return
			case <-t.done:
				reason = "task_finished"
				return
			case <-ticker.C:
			}
			sampleCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
			t.mu.Lock()
			state := *t.state
			t.mu.Unlock()
			stage := "ownership_probe"
			_, e := d.own(sampleCtx, &state)
			var m *boxlite.Metrics
			if e == nil {
				stage = "native_metrics"
				m, e = d.client.Metrics(sampleCtx, state.VMID)
			}
			expired := errors.Is(sampleCtx.Err(), context.DeadlineExceeded)
			cancel()
			if ctx.Err() != nil {
				reason = "consumer_context_cancelled"
				return
			}
			if d.ctx.Err() != nil {
				reason = "driver_shutdown"
				return
			}
			if e != nil {
				category := stage + "_error"
				if expired {
					category = stage + "_timeout"
				}
				status := 0
				var api *boxlite.APIError
				if errors.As(e, &api) {
					status = api.Status
				}
				d.statsSkipped(t, stream, category, status, time.Now())
				continue
			}
			if !warmed {
				warmed = true
				d.statsSkipped(t, stream, "cpu_delta_warmup", 0, time.Now())
				continue
			}
			now := time.Now()
			usage := measuredUsage(m, d.compute, now)
			if usage == nil {
				category := "native_sample_invalid"
				if m == nil || m.CPUPercent == nil || m.MemoryBytes == nil {
					category = "native_fields_missing"
				}
				d.statsSkipped(t, stream, category, 0, now)
				continue
			}
			d.statsMeasured(t, now)
			select {
			case ch <- usage:
				d.statsDelivered(t, stream, now, !delivered)
				delivered = true
			default:
				d.statsSkipped(t, stream, "consumer_backpressure", 0, now)
			}
		}
	}()
	return ch, nil
}
func (d *Driver) Shutdown(context.Context) error { d.cancel(); return nil } // VM lifetime belongs to Nomad, not plugin process.

// publishBinding shares only public VM/execution identity with a trusted sibling
// application task. No credentials, socket, guest command or control plane.
func (d *Driver) publishBinding(t *task) error {
	if !regexp.MustCompile(`^[a-zA-Z0-9_-]+$`).MatchString(t.config.Name) {
		return fmt.Errorf("invalid binding task name")
	}
	dir := t.config.TaskDir().SharedAllocDir
	if e := validateBindingDirectory(dir, t.config.AllocID); e != nil {
		return e
	}
	b, e := json.Marshal(map[string]string{"allocation_id": t.config.AllocID, "task": t.config.Name, "vm_id": t.state.VMID, "vm_name": t.state.Name, "execution_id": t.state.ExecutionID, "image": t.state.Image})
	if e != nil {
		return e
	}
	f, e := os.CreateTemp(dir, ".boxlite-binding-")
	if e != nil {
		return e
	}
	name := f.Name()
	defer os.Remove(name)
	if _, e = f.Write(b); e != nil {
		f.Close()
		return e
	}
	if e = f.Sync(); e != nil {
		f.Close()
		return e
	}
	if e = f.Close(); e != nil {
		return e
	}
	return os.Rename(name, filepath.Join(dir, "boxlite-"+t.config.Name+".json"))
}

// Native BoxLite 0.10.8 measures the per-VM host shim, not guest-used RAM.
func measuredUsage(m *boxlite.Metrics, compute cpustats.Compute, now time.Time) *cstructs.TaskResourceUsage {
	if m == nil || m.CPUPercent == nil || m.MemoryBytes == nil || compute.NumCores <= 0 || compute.TotalCompute <= 0 {
		return nil
	}
	cpu := *m.CPUPercent
	if cpu < 0 || math.IsNaN(cpu) || math.IsInf(cpu, 0) {
		return nil
	}
	return &cstructs.TaskResourceUsage{Timestamp: now.UnixNano(), ResourceUsage: &cstructs.ResourceUsage{
		CpuStats:    &cstructs.CpuStats{Percent: cpu, TotalTicks: cpu / 100 * float64(compute.TotalCompute) / float64(compute.NumCores), Measured: []string{"Percent", "TotalTicks"}},
		MemoryStats: &cstructs.MemoryStats{RSS: *m.MemoryBytes, Measured: []string{"RSS"}},
	}}
}

// Operator egress may be updated live; it is not an immutable workload identity.
// Guest image/resources/command, requested network mode and worker request remain
// bound. Legacy states retain the original strict hash until an explicit migration.
func immutableTaskHash(tc TaskConfig, spec boxlite.Create) string {
	spec.Network = boxlite.Network{}
	raw, _ := json.Marshal(struct {
		Create                   boxlite.Create
		StdioMode, WorkerRequest string
		OperatorNetworkRequested bool
	}{spec, tc.StdioMode, tc.WorkerRequest, tc.RestrictedNetwork})
	return digest(string(raw))
}
func matchesImmutableTask(s *State, immutable, legacy string) bool {
	if s.ImmutableHash != "" {
		return s.ImmutableHash == immutable
	}
	return s.SpecHash == legacy
}
