package driver

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"syscall"
)

// Nomad on Linux chowns the shared alloc directory to nobody. Root may
// publish public binding metadata there only beneath its private allocation
// directory. Do not chown Nomad's directory or accept arbitrary foreign owners.
func validateBindingDirectory(dir, allocID string) error {
	st, err := os.Lstat(dir)
	if err != nil {
		return fmt.Errorf("unsafe Nomad shared allocation directory")
	}
	parent, err := os.Lstat(filepath.Dir(dir))
	if err != nil {
		return fmt.Errorf("unsafe Nomad shared allocation parent")
	}
	if !bindingDirectoryAllowed(dir, allocID, st, parent, os.Getuid(), runtime.GOOS) {
		return fmt.Errorf("unsafe Nomad shared allocation directory")
	}
	return nil
}
func bindingDirectoryAllowed(dir, allocID string, st, parent os.FileInfo, uid int, platform string) bool {
	if !st.IsDir() || st.Mode()&os.ModeSymlink != 0 {
		return false
	}
	owner, ok := st.Sys().(*syscall.Stat_t)
	if !ok {
		return false
	}
	if owner.Uid == uint32(uid) {
		return true
	}
	if platform != "linux" || uid != 0 || owner.Uid != 65534 || owner.Gid != 65534 {
		return false
	}
	if filepath.Base(dir) != "alloc" || allocID == "" || filepath.Base(filepath.Dir(dir)) != allocID {
		return false
	}
	if !parent.IsDir() || parent.Mode()&os.ModeSymlink != 0 || parent.Mode().Perm()&0077 != 0 {
		return false
	}
	p, ok := parent.Sys().(*syscall.Stat_t)
	return ok && p.Uid == 0
}
