package driver

import (
	"path/filepath"
	"testing"
)

func TestPhysicalDiskBudget(t *testing.T) {
	if e := physicalDiskBudget(t.TempDir(), 1, 0); e != nil {
		t.Fatal(e)
	}
	if e := physicalDiskBudget(t.TempDir(), 1, 1<<50); e == nil {
		t.Fatal("accepted insufficient physical free space")
	}
	if e := physicalDiskBudget(filepath.Join(t.TempDir(), "missing"), 1, 0); e == nil {
		t.Fatal("unknown disk treated free")
	}
}
