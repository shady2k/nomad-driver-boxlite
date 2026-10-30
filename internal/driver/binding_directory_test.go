package driver

import (
	"os"
	"syscall"
	"testing"
	"time"
)

type bindingInfo struct {
	mode     os.FileMode
	uid, gid uint32
}

func (b bindingInfo) Name() string       { return "fixture" }
func (b bindingInfo) Size() int64        { return 0 }
func (b bindingInfo) Mode() os.FileMode  { return b.mode }
func (b bindingInfo) ModTime() time.Time { return time.Time{} }
func (b bindingInfo) IsDir() bool        { return b.mode.IsDir() }
func (b bindingInfo) Sys() any           { return &syscall.Stat_t{Uid: b.uid, Gid: b.gid} }
func TestBindingDirectoryLinuxNomadOwnership(t *testing.T) {
	nobody := bindingInfo{os.ModeDir | 0777, 65534, 65534}
	root := bindingInfo{os.ModeDir | 0700, 0, 0}
	cases := []struct {
		name, path, alloc, platform string
		uid                         int
		st, parent                  bindingInfo
		want                        bool
	}{
		{"linux-nomad", "/trusted/abc/alloc", "abc", "linux", 0, nobody, root, true},
		{"darwin-owned", "/trusted/abc/alloc", "abc", "darwin", 501, bindingInfo{os.ModeDir | 0700, 501, 20}, root, true},
		{"darwin-foreign", "/trusted/abc/alloc", "abc", "darwin", 0, nobody, root, false},
		{"nonroot", "/trusted/abc/alloc", "abc", "linux", 501, nobody, root, false},
		{"wrong-alloc", "/trusted/other/alloc", "abc", "linux", 0, nobody, root, false},
		{"wrong-basename", "/trusted/abc/else", "abc", "linux", 0, nobody, root, false},
		{"public-parent", "/trusted/abc/alloc", "abc", "linux", 0, nobody, bindingInfo{os.ModeDir | 0755, 0, 0}, false},
		{"foreign-parent", "/trusted/abc/alloc", "abc", "linux", 0, nobody, bindingInfo{os.ModeDir | 0700, 65534, 65534}, false},
		{"symlink", "/trusted/abc/alloc", "abc", "linux", 0, bindingInfo{os.ModeSymlink | 0777, 65534, 65534}, root, false},
		{"parent-symlink", "/trusted/abc/alloc", "abc", "linux", 0, nobody, bindingInfo{os.ModeSymlink | 0700, 0, 0}, false},
		{"unexpected-owner", "/trusted/abc/alloc", "abc", "linux", 0, bindingInfo{os.ModeDir | 0777, 1234, 1234}, root, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := bindingDirectoryAllowed(c.path, c.alloc, c.st, c.parent, c.uid, c.platform); got != c.want {
				t.Fatalf("got %v want %v", got, c.want)
			}
		})
	}
}
