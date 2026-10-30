// boxlite-stdio forwards only the existing main execution stream. VM lifecycle
// remains exclusively in the Nomad task driver. No application/Prime logic.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"github.com/gorilla/websocket"
	"io"
	"nomad-driver-boxlite/internal/boxlite"
	"os"
	"os/signal"
	"regexp"
	"syscall"
	"time"
)

func safeRead(path string, limit int64) ([]byte, error) {
	f, e := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if e != nil {
		return nil, e
	}
	defer f.Close()
	s, e := f.Stat()
	if e != nil {
		return nil, e
	}
	if !s.Mode().IsRegular() || s.Mode().Perm() != 0600 || s.Sys().(*syscall.Stat_t).Uid != uint32(os.Getuid()) {
		return nil, fmt.Errorf("expected owned regular mode0600 file")
	}
	b, e := io.ReadAll(io.LimitReader(f, limit+1))
	if e != nil || int64(len(b)) > limit {
		return nil, fmt.Errorf("private file size limit")
	}
	return b, nil
}
func main() { os.Exit(run()) }
func run() int {
	var binding, keyFile, keyEnv, endpoint, allocation string
	flag.StringVar(&binding, "binding", "", "Nomad driver public binding file")
	flag.StringVar(&allocation, "allocation", "", "expected allocation identity")
	flag.StringVar(&keyFile, "key-file", "", "node-owned API key file")
	flag.StringVar(&keyEnv, "key-env", "", "selected API-key environment variable for this process only")
	flag.StringVar(&endpoint, "endpoint", "", "required literal loopback API origin")
	flag.Parse()
	if flag.NArg() != 0 || allocation == "" || endpoint == "" {
		fmt.Fprintln(os.Stderr, "invalid stdio arguments")
		return 2
	}
	b, e := safeRead(binding, 4096)
	if e != nil {
		fmt.Fprintln(os.Stderr, "binding unavailable")
		return 2
	}
	var v map[string]string
	if json.Unmarshal(b, &v) != nil || v["allocation_id"] != allocation || !regexp.MustCompile(`^[A-Za-z0-9_-]+$`).MatchString(v["vm_id"]) || v["execution_id"] == "" {
		fmt.Fprintln(os.Stderr, "binding identity rejected")
		return 2
	}
	var key []byte
	if keyEnv != "" && keyFile == "" {
		key = []byte(os.Getenv(keyEnv))
	} else {
		key, e = safeRead(keyFile, 8192)
	}
	if e != nil || len(key) == 0 {
		fmt.Fprintln(os.Stderr, "node credential unavailable")
		return 2
	}
	c, e := boxlite.New(endpoint, string(key), 15*time.Second)
	if e != nil {
		fmt.Fprintln(os.Stderr, "node origin rejected")
		return 2
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	stream, e := c.AttachStdin(ctx, v["vm_id"], v["execution_id"], true)
	deadline := time.Now().Add(15 * time.Second)
	for e != nil && time.Now().Before(deadline) && ctx.Err() == nil {
		var api *boxlite.APIError
		if !errors.As(e, &api) || api.Status != 409 {
			break
		}
		time.Sleep(100 * time.Millisecond)
		stream, e = c.AttachStdin(ctx, v["vm_id"], v["execution_id"], true)
	}
	if e != nil {
		fmt.Fprintln(os.Stderr, e)
		return 3
	}
	defer stream.Conn.Close()
	go func() {
		buf := make([]byte, 65536)
		for {
			n, e := os.Stdin.Read(buf)
			if n > 0 {
				if stream.Conn.WriteMessage(websocket.BinaryMessage, buf[:n]) != nil {
					return
				}
			}
			if e != nil {
				if e == io.EOF {
					stream.Conn.WriteJSON(map[string]string{"type": "stdin_eof"})
				}
				return
			}
		}
	}()
	code, e := stream.Read(ctx, os.Stdout, os.Stderr)
	if e != nil {
		fmt.Fprintln(os.Stderr, e)
		return 3
	}
	if code < 0 || code > 255 {
		return 1
	}
	return code
}
