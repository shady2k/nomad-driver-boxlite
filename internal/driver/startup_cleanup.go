package driver

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

// Caller holds the durable task lock and (if registered) t.mu. Never delete
// disks or replay create/start. A failed observation keeps the reservation.
func (d *Driver) cleanupFailedStartup(t *task) error {
	s := t.state
	if s.Destroyed {
		return nil
	}
	if !s.StartupCleanupPending || s.OriginHash != digest(d.client.Origin()) {
		return fmt.Errorf("startup cleanup authority unavailable")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if s.VMID == "" {
		if s.CreateIssued {
			if !s.StartupCreateRejected {
				return fmt.Errorf("create outcome uncertain; preserve reservation")
			}
			boxes, err := d.client.List(ctx)
			if err != nil {
				return err
			}
			for _, b := range boxes {
				if b.Name == s.Name {
					return fmt.Errorf("rejected create name exists; no action")
				}
			}
		}
	} else {
		b, err := d.own(ctx, s)
		if err != nil {
			return err
		}
		if b.Status != "stopped" {
			if _, err = d.client.Stop(ctx, s.VMID); err != nil {
				return err
			}
			b, err = d.own(ctx, s)
			if err != nil {
				return err
			}
		}
		if b.Status != "stopped" || (b.PID != nil && *b.PID != 0) {
			return fmt.Errorf("startup stop unconfirmed; preserve reservation")
		}
		if err = d.stopCompanion(t); err != nil {
			return err
		}
		d.finished(t, b)
	}
	d.closeOutput(t)
	s.Destroyed = true
	s.StartupCleanupPending = false
	s.Phase = "startup_failed_disks_preserved"
	if err := d.store.Write(s); err != nil {
		s.Destroyed = false
		s.StartupCleanupPending = true
		return err
	}
	d.emit(t, "startup_cleanup_complete_disks_preserved", nil)
	return nil
}

// Only receipts emitted before Prime/ACP session creation authorize automatic
// bootstrap cleanup. Unknown/runtime/transport failures must not kill a turn.
func bootstrapFailureBeforeACP(summary map[string]string) bool {
	if summary["reason"] != "bootstrap_failed" && summary["reason"] != "command_failed" && summary["reason"] != "deadline_exceeded" {
		return false
	}
	switch summary["stage"] {
	case "node_config", "payload_identity", "envconsul_start", "allocation_context", "child_config", "child_payload", "rest_credential", "repo_clone", "repo_checkout", "initial_command", "prime_daemon_start", "acp_server_start":
		return true
	}
	return false
}

// An unsafe/unreadable ready receipt is unknown, never proof of idle/bootstrap.
func (d *Driver) noApplicationReadyReceipt(t *task) bool {
	fd, err := syscall.Open(filepath.Join(d.companionDir(t), "application-ready.json"), syscall.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if os.IsNotExist(err) {
		return true
	}
	if err != nil {
		return false
	}
	f := os.NewFile(uintptr(fd), "application-ready")
	defer f.Close()
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() || st.Mode().Perm() != 0600 || st.Size() > 4096 {
		return false
	}
	var receipt struct {
		Version      int    `json:"version"`
		AllocationID string `json:"allocation_id"`
		VMID         string `json:"vm_id"`
	}
	if json.NewDecoder(f).Decode(&receipt) != nil {
		return false
	}
	// Any receipt, even mismatched, forbids automatic cleanup pending diagnosis.
	return false
}
