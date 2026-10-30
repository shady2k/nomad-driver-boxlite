package driver

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	log "github.com/hashicorp/go-hclog"
	plugin "github.com/hashicorp/go-plugin"
	"github.com/hashicorp/nomad/plugins/base"
	"github.com/hashicorp/nomad/plugins/drivers"
)

// Exercise the actual pinned Nomad plugin gRPC adapters, not only direct Go calls.
func TestOfficialSDKGRPCLifecycleContract(t *testing.T) {
	f := newRuntime(t)
	d := New(context.Background(), log.NewNullLogger())
	d.pollInterval = 5 * time.Millisecond
	t.Cleanup(func() { _ = d.Shutdown(t.Context()) })
	conn, server := plugin.TestPluginGRPCConn(t, true, map[string]plugin.Plugin{base.PluginTypeBase: &base.PluginBase{Impl: d}, base.PluginTypeDriver: drivers.NewDriverPlugin(d, log.NewNullLogger())})
	// Stop server before Close sends controller.Shutdown; avoid two concurrent
	// upstream go-plugin Stop calls touching its broker without a lock.
	defer conn.Close()
	defer server.Stop()
	raw, e := conn.Dispense(base.PluginTypeDriver)
	if e != nil {
		t.Fatal(e)
	}
	client := raw.(drivers.DriverPlugin)
	info, e := client.PluginInfo()
	if e != nil || info.Name != "boxlite" || info.PluginVersion != Version {
		t.Fatalf("%+v %v", info, e)
	}
	if _, e = client.ConfigSchema(); e != nil {
		t.Fatal(e)
	}
	if _, e = client.TaskConfigSchema(); e != nil {
		t.Fatal(e)
	}
	var encoded []byte
	e = base.MsgPackEncode(&encoded, Config{Endpoint: f.server.URL, StateDir: filepath.Join(t.TempDir(), "state"), PolicyEndpoint: f.server.URL, PolicyNamespace: "boxlite-prime", PolicyPath: "nomad/jobs", StartTimeout: "2s"})
	if e != nil {
		t.Fatal(e)
	}
	if e = client.SetConfig(&base.Config{ApiVersion: "0.1.0", PluginConfig: encoded}); e != nil {
		t.Fatal(e)
	}
	cfg := taskConfig(t)
	h, _, e := client.StartTask(cfg)
	if e != nil {
		t.Fatal(e)
	}
	s := stateOf(t, h)
	if s.VMID == "" {
		t.Fatal("wire lost stable VM binding")
	}
	if _, e = client.InspectTask(cfg.ID); e != nil {
		t.Fatal(e)
	}
	if e = client.StopTask(cfg.ID, time.Second, ""); e != nil {
		t.Fatal(e)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
	defer cancel()
	wait, e := client.WaitTask(ctx, cfg.ID)
	if e != nil {
		t.Fatal(e)
	}
	select {
	case r := <-wait:
		if r == nil || r.ExitCode != 143 {
			t.Fatalf("%+v", r)
		}
	case <-ctx.Done():
		t.Fatal("wire wait did not return terminal code")
	}
	if e = client.DestroyTask(cfg.ID, false); e != nil {
		t.Fatal(e)
	}
}
