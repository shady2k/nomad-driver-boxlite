package driver

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

type State struct {
	StartupCleanupEligible                                                        bool
	Version                                                                       int `json:"version"`
	Key, TaskID, AllocID, TaskName, Name, VMID, ExecutionID, OriginHash, SpecHash string
	ImmutableHash                                                                 string
	StdioMode                                                                     string
	Image                                                                         string
	CPUs, MemoryMiB                                                               int
	CreateIssued, StartIssued, Destroyed                                          bool
	ReservationMiB                                                                int
	PolicyModifyIndex                                                             uint64
	CompanionIssued                                                               bool
	StartupCleanupPending, StartupCreateRejected                                  bool
	TerminalSummary                                                               map[string]string
	Phase                                                                         string
	StartedAt                                                                     time.Time
}
type Store struct{ Root string }

func NewStore(root string) (*Store, error) {
	if !filepath.IsAbs(root) {
		return nil, fmt.Errorf("state_dir must be absolute")
	}
	if e := os.MkdirAll(root, 0700); e != nil {
		return nil, e
	}
	s, e := os.Lstat(root)
	if e != nil {
		return nil, e
	}
	if !s.IsDir() || s.Mode().Perm() != 0700 || s.Sys().(*syscall.Stat_t).Uid != uint32(os.Getuid()) {
		return nil, fmt.Errorf("state_dir must be an owned non-symlink directory mode0700")
	}
	return &Store{root}, nil
}
func digest(s string) string            { v := sha256.Sum256([]byte(s)); return hex.EncodeToString(v[:]) }
func taskKey(alloc, name string) string { return digest(alloc + "\x00" + name) }
func randomName(key string) (string, error) {
	var b [16]byte
	if _, e := rand.Read(b[:]); e != nil {
		return "", e
	}
	return "nomad-" + key[:16] + "-" + hex.EncodeToString(b[:]), nil
}
func validKey(k string) bool { b, e := hex.DecodeString(k); return e == nil && len(b) == 32 }
func (s *Store) Lock(key string) (func(), error) {
	if !validKey(key) {
		return nil, fmt.Errorf("invalid task identity key")
	}
	fd, e := syscall.Open(filepath.Join(s.Root, key+".lock"), syscall.O_RDWR|syscall.O_CREAT|syscall.O_NOFOLLOW, 0600)
	if e != nil {
		return nil, e
	}
	var st syscall.Stat_t
	if e = syscall.Fstat(fd, &st); e != nil || st.Uid != uint32(os.Getuid()) || st.Mode&0777 != 0600 || st.Mode&syscall.S_IFMT != syscall.S_IFREG {
		syscall.Close(fd)
		return nil, fmt.Errorf("unsafe state lock")
	}
	if e = syscall.Flock(fd, syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		syscall.Close(fd)
		return nil, fmt.Errorf("task is owned by another driver operation")
	}
	return func() { syscall.Flock(fd, syscall.LOCK_UN); syscall.Close(fd) }, nil
}
func (s *Store) Read(key string) (*State, error) {
	if !validKey(key) {
		return nil, fmt.Errorf("invalid task key")
	}
	fd, e := syscall.Open(filepath.Join(s.Root, key+".json"), syscall.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if e != nil {
		return nil, e
	}
	f := os.NewFile(uintptr(fd), "state")
	defer f.Close()
	st, e := f.Stat()
	if e != nil {
		return nil, e
	}
	if !st.Mode().IsRegular() || st.Mode().Perm() != 0600 || st.Sys().(*syscall.Stat_t).Uid != uint32(os.Getuid()) || st.Size() > 65536 {
		return nil, fmt.Errorf("unsafe task state")
	}
	var v State
	dec := json.NewDecoder(f)
	dec.DisallowUnknownFields()
	if e = dec.Decode(&v); e != nil {
		return nil, e
	}
	if v.Version != 1 || v.Key != key || v.Name == "" {
		return nil, fmt.Errorf("state identity/version mismatch")
	}
	return &v, nil
}
func (s *Store) Write(v *State) error {
	if !validKey(v.Key) {
		return errors.New("invalid state key")
	}
	b, e := json.Marshal(v)
	if e != nil {
		return e
	}
	if len(b) > 65536 {
		return errors.New("state too large")
	}
	f, e := os.CreateTemp(s.Root, ".intent-")
	if e != nil {
		return e
	}
	name := f.Name()
	defer os.Remove(name)
	if e = f.Chmod(0600); e == nil {
		_, e = f.Write(b)
	}
	if e == nil {
		e = f.Sync()
	}
	closeErr := f.Close()
	if e != nil {
		return e
	}
	if closeErr != nil {
		return closeErr
	}
	if e = os.Rename(name, filepath.Join(s.Root, v.Key+".json")); e != nil {
		return e
	}
	dir, e := os.Open(s.Root)
	if e != nil {
		return e
	}
	defer dir.Close()
	return dir.Sync()
}
