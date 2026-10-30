package driver

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"

	"github.com/hashicorp/nomad/plugins/drivers"
)

// claimAllocation is a durable, node-local one-allocation-per-logical-task fence.
// It is intentionally retained after stop/destroy: a stopped session is never a
// fresh session. This is not a fleet-wide lock or a replacement for scheduling.
func (d *Driver) claimAllocation(cfg *drivers.TaskConfig, tc TaskConfig) error {
	if !tc.SingleAllocation {
		return nil
	}
	if cfg.Namespace == "" || cfg.JobID == "" || cfg.TaskGroupName == "" || cfg.Name == "" || cfg.AllocID == "" {
		return fmt.Errorf("single_allocation requires Nomad namespace/job/group/task/allocation identity")
	}
	logicalID := tc.LogicalID
	if logicalID == "" {
		logicalID = cfg.JobID
	}
	if len(logicalID) > 512 || !regexp.MustCompile(`^[A-Za-z0-9._:/-]+$`).MatchString(logicalID) {
		return fmt.Errorf("invalid logical_id")
	}
	key := digest(cfg.Namespace + "\x00" + logicalID + "\x00" + cfg.TaskGroupName + "\x00" + cfg.Name)
	store, err := NewStore(filepath.Join(d.store.Root, "logical-claims"))
	if err != nil {
		return err
	}
	unlock, err := store.Lock(key)
	if err != nil {
		return err
	}
	defer unlock()
	old, err := store.Read(key)
	if err == nil {
		if old.AllocID != cfg.AllocID {
			return fmt.Errorf("logical task already owned by original allocation %s; refusing replacement allocation %s and VM create", old.AllocID, cfg.AllocID)
		}
		return nil
	}
	if !os.IsNotExist(err) {
		return err
	}
	return store.Write(&State{Version: 1, Key: key, Name: key, AllocID: cfg.AllocID, TaskName: cfg.Name, TaskID: cfg.ID, Phase: "logical_allocation_claim"})
}
