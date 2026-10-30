package boxlite

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/gorilla/websocket"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestOriginIsNodeLocalAndNoCredentialsInURL(t *testing.T) {
	for _, u := range []string{"http://192.0.2.20:8100", "http://localhost:8100", "http://user:secret@127.0.0.1:8100", "http://127.0.0.1:8100/?token=x"} {
		if _, e := New(u, "", time.Second); e == nil {
			t.Fatal("accepted unsafe origin", u)
		}
	}
}
func TestCreatePinnedWireShapeAndBearer(t *testing.T) {
	key := "TEST-CREDENTIAL-DO-NOT-PERSIST"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+key {
			t.Error("missing credential")
		}
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		for _, k := range []string{"name", "image", "cpus", "memory_mib", "disk_size_gb", "cmd", "network", "auto_resume", "auto_delete"} {
			if _, ok := body[k]; !ok {
				t.Error("missing wire field", k)
			}
		}
		for _, k := range []string{"security", "volumes", "rootfs_path", "secrets"} {
			if _, ok := body[k]; ok {
				t.Error("unsafe field", k)
			}
		}
		fmt.Fprint(w, `{"box_id":"vm123","name":"owned","status":"configured","cpus":2,"memory_mib":2048}`)
	}))
	defer srv.Close()
	c, _ := New(srv.URL, key, time.Second)
	b, e := c.Create(context.Background(), Create{Name: "owned", Image: "pinned", CPUs: 2, MemoryMiB: 2048, DiskGiB: 12, Command: []string{"true"}})
	if e != nil || b.ID != "vm123" {
		t.Fatalf("%+v %v", b, e)
	}
}
func TestTypedErrorRedactsCredential(t *testing.T) {
	key := "NEVER-LOG-THIS-API-KEY"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(403)
		fmt.Fprintf(w, `{"error":{"message":"denied %s","type":"PermissionError","code":"permission_denied","request_id":"r123"}}`, key)
	}))
	defer srv.Close()
	c, _ := New(srv.URL, key, time.Second)
	_, e := c.Get(context.Background(), "vm")
	a, ok := e.(*APIError)
	if !ok || a.Code != "permission_denied" || a.RequestID != "r123" || a.Uncertain || strings.Contains(e.Error(), key) {
		t.Fatalf("%v", e)
	}
}
func TestUncertainPostIsNeverRetried(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(503)
		fmt.Fprint(w, `{"error":{"code":"busy","type":"TimeoutError","message":"uncertain"}}`)
	}))
	defer srv.Close()
	c, _ := New(srv.URL, "", time.Second)
	_, e := c.Start(context.Background(), "vm")
	if !IsUncertain(e) || calls.Load() != 1 {
		t.Fatalf("%v calls=%d", e, calls.Load())
	}
}
func TestRedirectNeverForwardsAuthorization(t *testing.T) {
	var stolen atomic.Int32
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { stolen.Add(1) }))
	defer other.Close()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, other.URL, 302) }))
	defer srv.Close()
	c, _ := New(srv.URL, "key", time.Second)
	if e := c.Probe(context.Background()); e == nil || stolen.Load() != 0 {
		t.Fatal("followed redirect")
	}
}
func TestNativeAttachOutputAndExit(t *testing.T) {
	var upgrade websocket.Upgrader
	done := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer close(done)
		if r.URL.Path != "/v1/boxes/vm/attach" || r.URL.Query().Get("stdin") != "0" {
			t.Error("wrong native route")
		}
		h := http.Header{}
		h.Set("x-boxlite-execution-id", "exec1")
		conn, e := upgrade.Upgrade(w, r, h)
		if e != nil {
			t.Error(e)
			return
		}
		defer conn.Close()
		_ = conn.WriteMessage(websocket.BinaryMessage, append([]byte{1}, []byte("hello")...))
		_ = conn.WriteMessage(websocket.BinaryMessage, append([]byte{2}, []byte("warning")...))
		_ = conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"exit","exit_code":7}`))
		_, _, _ = conn.ReadMessage()
	}))
	defer srv.Close()
	c, _ := New(srv.URL, "", time.Second)
	s, e := c.Attach(context.Background(), "vm", "")
	if e != nil {
		t.Fatal(e)
	}
	var out, errout bytes.Buffer
	code, e := s.Read(context.Background(), &out, &errout)
	if e != nil || code != 7 || out.String() != "hello" || errout.String() != "warning" || s.ExecutionID != "exec1" {
		t.Fatalf("code=%d err=%v out=%q stderr=%q", code, e, out.String(), errout.String())
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("attach leaked")
	}
}
func TestOversizedResponseBounded(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, strings.Repeat("x", MaxResponse+1))
	}))
	defer srv.Close()
	c, _ := New(srv.URL, "", time.Second)
	if _, e := c.Get(context.Background(), "vm"); e == nil {
		t.Fatal("unbounded response accepted")
	}
}

func TestWritableAttachExplicitlySelectsNativeStdin(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("stdin") != "1" {
			t.Error("writable attach did not opt in")
		}
		c, e := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if e != nil {
			return
		}
		defer c.Close()
		typ, b, e := c.ReadMessage()
		if e != nil || typ != websocket.BinaryMessage || string(b) != "guest-only-input\n" {
			t.Error("native stdin frame mismatch")
		}
		c.WriteMessage(websocket.TextMessage, []byte(`{"type":"exit","exit_code":0}`))
	}))
	defer srv.Close()
	c, e := New(srv.URL, "", time.Second)
	if e != nil {
		t.Fatal(e)
	}
	s, e := c.AttachStdin(context.Background(), "vm", "execution", true)
	if e != nil {
		t.Fatal(e)
	}
	if e = s.Conn.WriteMessage(websocket.BinaryMessage, []byte("guest-only-input\n")); e != nil {
		t.Fatal(e)
	}
	code, e := s.Read(context.Background(), io.Discard, io.Discard)
	if e != nil || code != 0 {
		t.Fatalf("%d %v", code, e)
	}
}

func TestAttachFailurePreservesUnderlyingTypedCause(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(500)
		fmt.Fprint(w, `{"error":{"code":"storage_error","type":"StorageError","message":"registry token failed private-key","request_id":"req-fixture"}}`)
	}))
	defer srv.Close()
	c, e := New(srv.URL, "private-key", time.Second)
	if e != nil {
		t.Fatal(e)
	}
	_, e = c.Attach(context.Background(), "vm", "")
	var api *APIError
	if !errors.As(e, &api) || api.Code != "storage_error" || api.Type != "StorageError" || api.RequestID != "req-fixture" || !api.Uncertain || strings.Contains(e.Error(), "private-key") {
		t.Fatalf("cause/redaction lost: %v", e)
	}
}

func TestGuestCapabilityDiscoveryIsExplicit(t *testing.T) {
	for _, fixture := range []struct {
		body string
		want bool
	}{{`{"capabilities":{"guest_privileged_default":true}}`, true}, {`{"capabilities":{}}`, false}} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/v1/config" {
				t.Error(r.URL.Path)
			}
			fmt.Fprint(w, fixture.body)
		}))
		c, _ := New(srv.URL, "", time.Second)
		got, e := c.GuestPrivilegedDefault(context.Background())
		srv.Close()
		if e != nil || got != fixture.want {
			t.Fatal(got, e)
		}
	}
}
