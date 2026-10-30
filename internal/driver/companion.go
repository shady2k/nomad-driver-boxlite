package driver

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"github.com/hashicorp/nomad/plugins/drivers"
	"io"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func ownedFile(path string) error {
	st, e := os.Lstat(path)
	if e != nil {
		return e
	}
	if !st.Mode().IsRegular() || st.Mode().Perm()&0022 != 0 {
		return fmt.Errorf("unsafe operator companion asset")
	}
	return nil
}
func (d *Driver) configurePolicy(c Config) error {
	if len(c.CompanionCommand) == 0 {
		return nil
	}
	if len(c.CompanionCommand) > 8 || !filepath.IsAbs(c.CompanionCommand[0]) || !filepath.IsAbs(c.GuestBootstrapFile) {
		return fmt.Errorf("operator companion and budget configuration required")
	}
	if e := ownedFile(c.GuestBootstrapFile); e != nil {
		return e
	}
	raw, e := os.ReadFile(c.GuestBootstrapFile)
	if e != nil {
		return e
	}
	d.guestCommand = []string{"/usr/bin/python3", "-I", "-c", string(raw)}
	for _, arg := range c.CompanionCommand[1:] {
		if strings.HasPrefix(arg, "/") {
			if e := ownedFile(arg); e != nil {
				return e
			}
		}
	}
	return nil
}
func (d *Driver) companionEnvelope(cfg *drivers.TaskConfig, tc TaskConfig) (map[string]any, error) {
	if cfg.Resources == nil || cfg.Resources.Ports == nil {
		return nil, fmt.Errorf("assigned session port required")
	}
	port := 0
	host := ""
	for _, p := range *cfg.Resources.Ports {
		if p.Label == "session" {
			if port != 0 {
				return nil, fmt.Errorf("duplicate session port")
			}
			port = p.Value
			host = p.HostIP
		}
	}
	if port < 1 || port > 65535 {
		return nil, fmt.Errorf("session port outside operator range")
	}
	return map[string]any{"request": tc.WorkerRequest, "image": tc.Image, "cpus": tc.CPUs, "memory_mib": tc.MemoryMiB, "disk_gib": tc.DiskGiB, "logical_id": tc.LogicalID, "job_id": cfg.JobID, "namespace": cfg.Namespace, "node_id": cfg.NodeID, "task_name": cfg.Name, "allocation_id": cfg.AllocID, "port": port, "host": host}, nil
}
func (d *Driver) validateCompanion(cfg *drivers.TaskConfig, tc *TaskConfig) error {
	if !reflect.DeepEqual(tc.Command, d.guestCommand) || !tc.SingleAllocation || tc.StdioMode != "external" || !tc.RestrictedNetwork || len(tc.WorkerRequest) > 15500 || tc.WorkerRequest == "" {
		return fmt.Errorf("worker requires fixed guest bootstrap, identity, network and explicit request")
	}
	input, e := d.companionEnvelope(cfg, *tc)
	if e != nil {
		return e
	}
	input["operation"] = "validate"
	raw, _ := json.Marshal(input)
	ctx, cancel := context.WithTimeout(d.ctx, 12*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, d.config.CompanionCommand[0], d.config.CompanionCommand[1:]...)
	cmd.Env = []string{"HOME=" + os.Getenv("HOME"), "PATH=/usr/bin:/bin", "LC_ALL=C"}
	cmd.Stdin = bytes.NewReader(raw)
	cmd.Stderr = io.Discard
	output, e := cmd.Output()
	if e != nil || len(output) > 20000 {
		return fmt.Errorf("operator companion rejected request contract")
	}
	var response struct {
		Request json.RawMessage `json:"request"`
		SpecID  string          `json:"spec_id"`
	}
	if json.Unmarshal(output, &response) != nil || !validKey(response.SpecID) {
		return fmt.Errorf("invalid companion validation response")
	}
	tc.WorkerRequest = string(response.Request)
	tc.specID = response.SpecID
	return nil
}
func companionFingerprint(pid int) string {
	cmd := exec.Command("/bin/ps", "-p", strconv.Itoa(pid), "-o", "lstart=")
	cmd.Env = []string{"PATH=/usr/bin:/bin", "LC_ALL=C"}
	out, e := cmd.Output()
	if e != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}
func (d *Driver) companionDir(t *task) string {
	return filepath.Join(d.store.Root, "companions", t.state.Key)
}
func (d *Driver) companionPID(t *task) (int, bool, error) {
	dir := d.companionDir(t)
	fd, e := syscall.Open(filepath.Join(dir, "owner.lock"), syscall.O_RDWR|syscall.O_NOFOLLOW, 0)
	if os.IsNotExist(e) {
		return 0, false, nil
	}
	if e != nil {
		return 0, false, e
	}
	defer syscall.Close(fd)
	if e = syscall.Flock(fd, syscall.LOCK_EX|syscall.LOCK_NB); e == nil {
		syscall.Flock(fd, syscall.LOCK_UN)
		return 0, false, nil
	}
	var rec struct {
		PID   int    `json:"pid"`
		Start string `json:"start"`
	}
	p := filepath.Join(dir, "process.json")
	if e = ownedFile(p); e != nil {
		return 0, false, e
	}
	raw, e := os.ReadFile(p)
	if e != nil {
		return 0, false, e
	}
	if json.Unmarshal(raw, &rec) != nil || rec.PID < 2 || rec.Start == "" || companionFingerprint(rec.PID) != rec.Start {
		return 0, false, fmt.Errorf("companion process ownership unresolved")
	}
	return rec.PID, true, nil
}
func (d *Driver) startCompanion(t *task, tc TaskConfig) error {
	if len(d.config.CompanionCommand) == 0 {
		return nil
	}
	if _, alive, e := d.companionPID(t); e != nil {
		return e
	} else if alive {
		return nil
	}
	if t.state.CompanionIssued {
		return fmt.Errorf("original companion lost; no replacement ACP session")
	}
	dir := d.companionDir(t)
	if e := os.MkdirAll(dir, 0700); e != nil {
		return e
	}
	st, e := os.Lstat(dir)
	if e != nil || !st.IsDir() || st.Mode().Perm() != 0700 {
		return fmt.Errorf("unsafe companion state")
	}
	envelope, e := d.companionEnvelope(t.config, tc)
	if e != nil {
		return e
	}
	envelope["operation"] = "run"
	envelope["companion_dir"] = dir
	envelope["binding"] = map[string]string{"allocation_id": t.config.AllocID, "task": t.config.Name, "vm_id": t.state.VMID, "vm_name": t.state.Name, "execution_id": t.state.ExecutionID, "image": t.state.Image}
	raw, _ := json.Marshal(envelope)
	t.state.CompanionIssued = true
	if e = d.store.Write(t.state); e != nil {
		return e
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
	cmd := exec.Command(d.config.CompanionCommand[0], d.config.CompanionCommand[1:]...)
	cmd.Env = []string{"HOME=" + os.Getenv("HOME"), "PATH=/usr/bin:/bin", "LC_ALL=C"}
	cmd.Stdin = bytes.NewReader(raw)
	cmd.Stdout = out
	cmd.Stderr = errout
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	e = cmd.Start()
	out.Close()
	errout.Close()
	if e != nil {
		return e
	}
	startedAt := time.Now()
	go func() {
		e := cmd.Wait()
		if e != nil {
			t.mu.Lock()
			d.publishTerminalSummary(t)
			d.emit(t, "companion_exited", fmt.Errorf("fixed companion exited; exit_code=%d elapsed_seconds=%.3f", cmd.ProcessState.ExitCode(), time.Since(startedAt).Seconds()))
			t.mu.Unlock()
		}
	}()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if _, alive, e := d.companionPID(t); e == nil && alive {
			d.emit(t, "companion_started", nil)
			return nil
		}
		time.Sleep(20 * time.Millisecond)
	}
	return fmt.Errorf("companion launch ownership unconfirmed")
}
func (d *Driver) stopCompanion(t *task) error {
	if len(d.config.CompanionCommand) == 0 {
		return nil
	}
	pid, alive, e := d.companionPID(t)
	if e != nil || !alive {
		return e
	}
	if e = syscall.Kill(pid, syscall.SIGTERM); e != nil && e != syscall.ESRCH {
		return e
	}
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		_, alive, e = d.companionPID(t)
		if e != nil {
			return e
		}
		if !alive {
			return nil
		}
		time.Sleep(50 * time.Millisecond)
	}
	return fmt.Errorf("owned companion cleanup unconfirmed")
}

// publishTerminalSummary bridges a fixed local companion receipt into Nomad's
// server-retained task events. Caller holds task mutex. Raw logs are not copied.
func (d *Driver) publishTerminalSummary(t *task) {
	if len(t.state.TerminalSummary) > 0 {
		return
	}
	path := filepath.Join(d.companionDir(t), "terminal-summary.json")
	fd, e := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if e != nil {
		return
	}
	f := os.NewFile(uintptr(fd), "terminal-summary")
	defer f.Close()
	st, e := f.Stat()
	if e != nil || !st.Mode().IsRegular() || st.Mode().Perm() != 0600 || st.Sys().(*syscall.Stat_t).Uid != uint32(os.Getuid()) || st.Size() > 4096 {
		return
	}
	var receipt struct {
		Version      int               `json:"version"`
		AllocationID string            `json:"allocation_id"`
		VMID         string            `json:"vm_id"`
		Timestamp    float64           `json:"timestamp"`
		Summary      map[string]string `json:"summary"`
	}
	if json.NewDecoder(f).Decode(&receipt) != nil || receipt.Version != 1 || receipt.AllocationID != t.state.AllocID || (receipt.VMID != "" && receipt.VMID != t.state.VMID) {
		return
	}
	allowed := map[string]bool{"stage": true, "exception_type": true, "error_type": true, "reason": true, "deadline_seconds": true, "elapsed_seconds": true, "exit_code": true, "signal": true}
	safe := map[string]string{}
	for k, v := range receipt.Summary {
		if !allowed[k] || len(v) > 160 || strings.ContainsAny(v, "\r\n") {
			return
		}
		if k == "stage" && !map[string]bool{"startup": true, "repo_clone": true, "repo_checkout": true, "initial_command": true, "prime_daemon_start": true, "acp_server_start": true, "node_config": true, "payload_identity": true, "envconsul_start": true, "allocation_context": true, "child_config": true, "child_payload": true, "rest_credential": true, "entry": true, "unknown": true}[v] {
			return
		}
		if (k == "exception_type" || k == "error_type") && !map[string]bool{"TimeoutError": true, "RuntimeError": true, "ValueError": true, "OSError": true, "FileNotFoundError": true, "PermissionError": true, "KeyboardInterrupt": true, "BrokenPipeError": true, "ConnectionError": true, "UnknownError": true}[v] {
			return
		}
		if k == "reason" && !map[string]bool{"deadline_exceeded": true, "command_failed": true, "bootstrap_failed": true, "interrupted": true, "transport_failed": true}[v] {
			return
		}
		if k == "deadline_seconds" || k == "elapsed_seconds" || k == "exit_code" || k == "signal" {
			n, err := strconv.ParseFloat(v, 64)
			if err != nil || math.IsNaN(n) || math.IsInf(n, 0) {
				return
			}
		}
		safe[k] = v
	}
	if len(safe) == 0 {
		return
	}
	t.state.TerminalSummary = safe
	if d.store.Write(t.state) != nil {
		return
	}
	raw, _ := json.Marshal(safe)
	d.emit(t, "terminal_failure_summary", fmt.Errorf("summary=%s", string(raw)))
}
