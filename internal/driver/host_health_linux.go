package driver

import (
	"fmt"
	"syscall"
)

// Health probe only: KVM_GET_API_VERSION does not create a VM or vCPU.
func nativeHostHealth() error {
	fd, err := syscall.Open("/dev/kvm", syscall.O_RDWR|syscall.O_CLOEXEC, 0)
	if err != nil {
		return fmt.Errorf("BoxLite Linux KVM unavailable to driver identity: %w", err)
	}
	defer syscall.Close(fd)
	version, _, errno := syscall.Syscall(syscall.SYS_IOCTL, uintptr(fd), uintptr(0xAE00), 0)
	if errno != 0 {
		return fmt.Errorf("KVM_GET_API_VERSION failed: %w", errno)
	}
	return validateKVMVersion(int(version))
}
