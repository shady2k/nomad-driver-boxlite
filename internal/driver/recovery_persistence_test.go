package driver

import (
	"github.com/hashicorp/go-msgpack/v2/codec"
	nstructs "github.com/hashicorp/nomad/nomad/structs"
	"github.com/hashicorp/nomad/plugins/drivers"
	"io"
	"path/filepath"
	"testing"
)

func TestRecoveryAfterActualNomadStateSerialization(t *testing.T) {
	f := newRuntime(t)
	dir := filepath.Join(t.TempDir(), "state")
	d := setupDriver(t, f, dir)
	cfg := taskConfig(t)
	h := started(t, d, cfg)
	state := stateOf(t, h)
	// Same codec used by Nomad boltdd.Bucket.Put. The SDK's rawDriverConfig
	// is unexported and deliberately absent after this persistence boundary.
	var raw []byte
	if e := codec.NewEncoderBytes(&raw, nstructs.MsgpackHandle).Encode(h); e != nil {
		t.Fatal(e)
	}
	var disk drivers.TaskHandle
	if e := codec.NewDecoderBytes(raw, nstructs.MsgpackHandle).Decode(&disk); e != nil {
		t.Fatal(e)
	}
	var tc TaskConfig
	if e := disk.Config.DecodeDriverConfig(&tc); e != io.EOF {
		t.Fatalf("expected lost private raw config EOF, got %v", e)
	}
	if len(disk.DriverState) == 0 {
		t.Fatal("opaque driver state must survive")
	}
	_ = d.Shutdown(t.Context())
	f.awaitDetached(t, state.VMID)
	next := setupDriver(t, f, dir)
	if e := next.RecoverTask(&disk); e != nil {
		t.Fatal(e)
	}
	status, e := next.InspectTask(cfg.ID)
	if e != nil || status.DriverAttributes["vm_id"] != state.VMID {
		t.Fatalf("wrong recovered VM: %v %v", status, e)
	}
	creates, starts, _, _ := f.counts()
	if creates != 1 || starts != 1 {
		t.Fatal("recovery repeated VM lifecycle")
	}
}

func TestOperatorNetworkChangeDoesNotReplaceTask(t *testing.T) {
	f := newRuntime(t)
	d := setupDriver(t, f, "")
	cfg := taskConfig(t)
	var tc TaskConfig
	_ = cfg.DecodeDriverConfig(&tc)
	tc.RestrictedNetwork = true
	_ = cfg.EncodeConcreteDriverConfig(tc)
	d.config.OutboundAllowNet = []string{"example.net"}
	first := stateOf(t, started(t, d, cfg))
	d.config.OutboundAllowNet = nil
	d.config.OutboundAllowAll = true
	next := stateOf(t, started(t, d, cfg))
	if next.VMID != first.VMID {
		t.Fatal("operator policy replaced VM")
	}
	tc.Image = "registry.example/other@sha256:" + first.Image[len(first.Image)-64:]
	_ = cfg.EncodeConcreteDriverConfig(tc)
	if _, _, e := d.StartTask(cfg); e == nil {
		t.Fatal("changed immutable image accepted")
	}
	creates, starts, _, _ := f.counts()
	if creates != 1 || starts != 1 {
		t.Fatal("policy change repeated VM lifecycle")
	}
}
