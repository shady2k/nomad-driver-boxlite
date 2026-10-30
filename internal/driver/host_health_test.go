package driver

import "testing"

func TestKVMAPIVersionValidation(t *testing.T) {
	if validateKVMVersion(12) != nil {
		t.Fatal("KVM API12 rejected")
	}
	for _, n := range []int{-1, 0, 11, 13} {
		if validateKVMVersion(n) == nil {
			t.Fatalf("invalid version %d accepted", n)
		}
	}
}
