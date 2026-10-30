package driver

// BoxLite owns Hypervisor.framework setup on macOS. The authenticated REST
// probe and actual VM acceptance remain required; no Linux KVM requirement.
func nativeHostHealth() error { return nil }
