package driver

import (
	"context"
	"fmt"
	"github.com/hashicorp/nomad/plugins/drivers"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// Kernel flock + durable intent serialize admission across plugin process recovery.
// Nomad alone selects placement. Policy is local to this REST owner, not a fleet quota.
func (d *Driver) reserveBudget(s *State, cfg *drivers.TaskConfig, tc TaskConfig) error {
	fd, e := syscall.Open(filepath.Join(d.store.Root, "node-budget.lock"), syscall.O_CREAT|syscall.O_RDWR|syscall.O_NOFOLLOW, 0600)
	if e != nil {
		return e
	}
	defer syscall.Close(fd)
	if e = syscall.Flock(fd, syscall.LOCK_EX); e != nil {
		return e
	}
	defer syscall.Flock(fd, syscall.LOCK_UN)
	policy, index, e := d.readNodePolicy(cfg)
	if e != nil {
		return e
	}
	if tc.DiskGiB > policy.MaxDiskGiB {
		return d.policyError("disk_budget_exceeded")
	}
	if tc.CPUs > policy.MaxGuestCPUs || tc.MemoryMiB > policy.MaxReservationMiB || policy.OverheadMiB > policy.MaxReservationMiB-tc.MemoryMiB {
		return d.policyError("requested_resources_exceed_budget")
	}
	if policy.CPUMHzPerVCPU > int(^uint(0)>>1)/tc.CPUs || int(cfg.Resources.NomadResources.Cpu.CpuShares) < tc.CPUs*policy.CPUMHzPerVCPU || int(cfg.Resources.NomadResources.Memory.MemoryMB)-tc.MemoryMiB < policy.OverheadMiB {
		return d.policyError("reservation_does_not_cover_guest_and_overhead")
	}
	if d.config.RuntimeStorageDir != "" {
		// Nomad's group EphemeralDisk is shared allocation state, not part of
		// AllocatedTaskResources. The trusted jobspec supplies its reservation;
		// this independent guard checks actual storage including retained disks.

		if e := physicalDiskBudget(d.config.RuntimeStorageDir, tc.DiskGiB, d.config.DiskFreeFloorMiB); e != nil {
			return e
		}
	}
	s.ReservationMiB = int(cfg.Resources.NomadResources.Memory.MemoryMB)
	s.PolicyModifyIndex = index
	ctx, cancel := context.WithTimeout(d.ctx, 10*time.Second)
	defer cancel()
	boxes, e := d.client.List(ctx)
	if e != nil {
		return fmt.Errorf("budget runtime observation unavailable: %w", e)
	}
	count, cpu, mem := 1, s.CPUs, s.ReservationMiB
	known := map[string]bool{}
	reservations := map[string]int{}
	entries, e := os.ReadDir(d.store.Root)
	if e != nil {
		return e
	}
	states := []*State{}
	for _, f := range entries {
		key := strings.TrimSuffix(f.Name(), ".json")
		if !validKey(key) || key == s.Key {
			continue
		}
		other, e := d.store.Read(key)
		if e != nil {
			return e
		}
		if other.Destroyed {
			continue
		}
		states = append(states, other)
		if other.VMID != "" && other.ReservationMiB > 0 {
			reservations[other.VMID] = other.ReservationMiB
		}
	}
	add := func(c, m int) error {
		if c < 1 || m < 1 || count >= policy.MaxVMs || cpu > policy.MaxGuestCPUs-c || mem > policy.MaxReservationMiB-m {
			return d.policyError("node_budget_exceeded")
		}
		count++
		cpu += c
		mem += m
		return nil
	}
	for _, b := range boxes {
		known[b.ID] = true
		known[b.Name] = true
		if b.Status == "stopped" && b.PID == nil || b.Status == "failed" && b.PID == nil {
			continue
		}
		if b.CPUs < 1 || b.MemoryMiB < 1 {
			return fmt.Errorf("budget runtime resources unknown")
		}
		m := reservations[b.ID]
		if m == 0 {
			if b.MemoryMiB > policy.MaxReservationMiB || policy.OverheadMiB > policy.MaxReservationMiB-b.MemoryMiB {
				return d.policyError("node_budget_exceeded")
			}
			m = b.MemoryMiB + policy.OverheadMiB
		}
		if e = add(b.CPUs, m); e != nil {
			return e
		}
	}
	for _, other := range states {
		if !known[other.VMID] && !known[other.Name] {
			// Missing issued create is uncertain, not free budget. Never reclaim on timeout.
			if other.CPUs < 1 || other.MemoryMiB < 1 {
				return fmt.Errorf("budget pending resources invalid")
			}
			m := other.ReservationMiB
			if m == 0 {
				if other.MemoryMiB > policy.MaxReservationMiB || policy.OverheadMiB > policy.MaxReservationMiB-other.MemoryMiB {
					return d.policyError("node_budget_exceeded")
				}
				m = other.MemoryMiB + policy.OverheadMiB
			}
			if e = add(other.CPUs, m); e != nil {
				return e
			}
		}
	}
	if count > policy.MaxVMs || cpu > policy.MaxGuestCPUs || mem > policy.MaxReservationMiB {
		return d.policyError("node_budget_exceeded")
	}
	return d.store.Write(s)
}
