package driver

// Opt-in only: requires an explicitly provisioned, authenticated isolated runtime.
import (
	"context"
	"encoding/json"
	log "github.com/hashicorp/go-hclog"
	ns "github.com/hashicorp/nomad/nomad/structs"
	"github.com/hashicorp/nomad/plugins/drivers"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestLiveIsolated(t *testing.T) {
	root := os.Getenv("BOXLITE_DRIVER_LIVE_DIR")
	phase := os.Getenv("BOXLITE_DRIVER_LIVE_PHASE")
	if os.Getenv("BOXLITE_TEST_ENDPOINT") == "" || root == "" || os.Getenv("BOXLITE_NOMAD_ENDPOINT") == "" || os.Getenv("BOXLITE_TEST_IMAGE") == "" {
		t.Skip("explicit isolated runtime required")
	}
	d := New(context.Background(), log.NewNullLogger())
	defer d.Shutdown(context.Background())
	if e := d.configure(Config{Endpoint: os.Getenv("BOXLITE_TEST_ENDPOINT"), StateDir: filepath.Join(root, "state"), APIKeyFile: filepath.Join(root, "api-key"), PolicyEndpoint: os.Getenv("BOXLITE_NOMAD_ENDPOINT"), PolicyNamespace: "boxlite-prime", PolicyPath: "nomad/jobs", StartTimeout: "15m"}); e != nil {
		t.Fatal(e)
	}
	var h drivers.TaskHandle
	if phase == "recover" {
		b, e := os.ReadFile(filepath.Join(root, "handle.json"))
		if e != nil {
			t.Fatal(e)
		}
		if e = json.Unmarshal(b, &h); e != nil {
			t.Fatal(e)
		}
		if e = d.RecoverTask(&h); e != nil {
			t.Fatal(e)
		}
		s := stateOf(t, &h)
		t.Log("recovered same VM", s.VMID)
		if e = d.StopTask(h.Config.ID, 30*time.Second, ""); e != nil {
			t.Fatal(e)
		}
	} else {
		cmd := []string{"/usr/bin/python3", "-c", "import sys;print('bridge-stdout',flush=True);print('bridge-stderr',file=sys.stderr,flush=True);open('/tmp/bridge-owned-marker','w').write('isolated');sys.exit(0)"}
		if phase == "start-recovery" {
			cmd = []string{"/usr/bin/python3", "-c", "import time;print('bridge-recovery-start',flush=True);time.sleep(240)"}
		}
		cfg := &drivers.TaskConfig{ID: "live-" + phase, AllocID: "isolated-" + phase, Name: "synthetic", StdoutPath: filepath.Join(root, phase+".stdout"), StderrPath: filepath.Join(root, phase+".stderr"), Resources: &drivers.Resources{NomadResources: &ns.AllocatedTaskResources{Cpu: ns.AllocatedCpuResources{CpuShares: 1000}, Memory: ns.AllocatedMemoryResources{MemoryMB: 1536}}}}
		for _, p := range []string{cfg.StdoutPath, cfg.StderrPath} {
			if e := os.WriteFile(p, nil, 0600); e != nil {
				t.Fatal(e)
			}
		}
		if e := cfg.EncodeConcreteDriverConfig(TaskConfig{Image: os.Getenv("BOXLITE_TEST_IMAGE"), Command: cmd, CPUs: 1, MemoryMiB: 512, DiskGiB: 12}); e != nil {
			t.Fatal(e)
		}
		hp, _, e := d.StartTask(cfg)
		if e != nil {
			t.Fatal(e)
		}
		h = *hp
		s := stateOf(t, &h)
		t.Log("created VM", s.VMID)
		b, _ := json.Marshal(&h)
		if e = os.WriteFile(filepath.Join(root, phase+"-handle.json"), b, 0600); e != nil {
			t.Fatal(e)
		}
		if phase == "start-recovery" {
			if e = os.WriteFile(filepath.Join(root, "handle.json"), b, 0600); e != nil {
				t.Fatal(e)
			}
			time.Sleep(2 * time.Second)
			return
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	ch, e := d.WaitTask(ctx, h.Config.ID)
	if e != nil {
		t.Fatal(e)
	}
	select {
	case r := <-ch:
		t.Logf("terminal %+v", r)
		if phase == "success" && (r == nil || r.ExitCode != 0 || r.Err != nil) {
			t.Fatal("expected actual zero exit")
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	v, e := d.InspectTask(h.Config.ID)
	if e != nil {
		t.Fatal(e)
	}
	t.Logf("inspect state=%s attributes=%v", v.State, v.DriverAttributes)
	if e = d.DestroyTask(h.Config.ID, false); e != nil {
		t.Fatal(e)
	}
}
