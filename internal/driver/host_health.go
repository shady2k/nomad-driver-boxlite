package driver

import "fmt"

func validateKVMVersion(version int) error {
	if version != 12 {
		return fmt.Errorf("unsupported KVM API version %d", version)
	}
	return nil
}
