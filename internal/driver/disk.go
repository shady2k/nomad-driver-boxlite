package driver

import (
	"fmt"
	"syscall"
)

// Physical free space includes retained disks. This is an admission guard, not a
// quota or a second scheduler: Nomad accounts aggregate requested disk capacity.
func physicalDiskBudget(path string, guestGiB, floorMiB int) error {
	var st syscall.Statfs_t
	if e := syscall.Statfs(path, &st); e != nil {
		return fmt.Errorf("runtime_disk_observation_unavailable: %w", e)
	}
	if guestGiB < 1 || floorMiB < 0 || uint64(guestGiB) > (^uint64(0))/1073741824 || uint64(floorMiB) > (^uint64(0))/1048576 {
		return fmt.Errorf("invalid_runtime_disk_budget")
	}
	available := uint64(st.Bavail) * uint64(st.Bsize)
	guest := uint64(guestGiB) * 1024 * 1024 * 1024
	floor := uint64(floorMiB) * 1024 * 1024
	if guest > ^uint64(0)-floor {
		return fmt.Errorf("invalid_runtime_disk_budget")
	}
	required := guest + floor
	if available < required {
		return fmt.Errorf("runtime_disk_free_space_insufficient: available_mib=%d required_mib=%d", available/1048576, required/1048576)
	}
	return nil
}
