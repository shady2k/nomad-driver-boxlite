// Package boxlite implements the pinned local BoxLite 0.10.8 REST contract.
package boxlite

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

const MaxResponse = 1 << 20

type APIError struct {
	Status                         int
	Code, Type, Message, RequestID string
	Uncertain                      bool
}

func (e *APIError) Error() string {
	return fmt.Sprintf("boxlite status=%d code=%s type=%s request_id=%s: %s", e.Status, e.Code, e.Type, e.RequestID, e.Message)
}
func IsNotFound(err error) bool  { var e *APIError; return errors.As(err, &e) && e.Status == 404 }
func IsUncertain(err error) bool { var e *APIError; return errors.As(err, &e) && e.Uncertain }

type Box struct {
	ID        string `json:"box_id"`
	Name      string `json:"name"`
	Status    string `json:"status"`
	Image     string `json:"image"`
	CPUs      int    `json:"cpus"`
	MemoryMiB int    `json:"memory_mib"`
	PID       *int   `json:"pid,omitempty"`
	ExitCode  *int   `json:"exit_code,omitempty"`
}
type Direction struct {
	Mode     string   `json:"mode"`
	AllowNet []string `json:"allow_net"`
}
type Network struct {
	Outbound Direction `json:"outbound"`
	Inbound  Direction `json:"inbound"`
}
type Create struct {
	Name       string   `json:"name"`
	Image      string   `json:"image"`
	CPUs       int      `json:"cpus"`
	MemoryMiB  int      `json:"memory_mib"`
	DiskGiB    int      `json:"disk_size_gb"`
	Entrypoint []string `json:"entrypoint"`
	Command    []string `json:"cmd"`
	Network    Network  `json:"network"`
	AutoResume bool     `json:"auto_resume"`
	AutoDelete int      `json:"auto_delete"`
}
type Metrics struct {
	CPUPercent  *float64 `json:"cpu_percent"`
	MemoryBytes *uint64  `json:"memory_bytes"`
}

type Client struct {
	base *url.URL
	key  string
	HTTP *http.Client
	WS   *websocket.Dialer
}

func New(base, key string, timeout time.Duration) (*Client, error) {
	u, err := url.Parse(base)
	if err != nil || u.Scheme != "http" && u.Scheme != "https" || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Path != "" && u.Path != "/" {
		return nil, fmt.Errorf("invalid node-local BoxLite origin")
	}
	host := u.Hostname()
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() {
		return nil, fmt.Errorf("prototype requires a node-local loopback endpoint; remote fleet accounting unsupported")
	}
	u.Path = ""
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.Proxy = nil
	return &Client{base: u, key: key, HTTP: &http.Client{Transport: tr, Timeout: timeout, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, WS: &websocket.Dialer{HandshakeTimeout: timeout, Proxy: nil}}, nil
}
func (c *Client) Origin() string { return c.base.String() }
func (c *Client) Redact(s string) string {
	if c.key != "" {
		return strings.ReplaceAll(s, c.key, "[REDACTED]")
	}
	return s
}
func (c *Client) request(ctx context.Context, method, path string, body, out any) error {
	var r io.Reader
	if body != nil {
		b, e := json.Marshal(body)
		if e != nil {
			return e
		}
		r = bytes.NewReader(b)
	}
	req, e := http.NewRequestWithContext(ctx, method, c.base.String()+path, r)
	if e != nil {
		return e
	}
	if c.key != "" {
		req.Header.Set("Authorization", "Bearer "+c.key)
	}
	req.Header.Set("Content-Type", "application/json")
	res, e := c.HTTP.Do(req)
	if e != nil {
		return &APIError{Code: "runtime_unreachable", Message: c.Redact(e.Error()), Uncertain: method != "GET"}
	}
	defer res.Body.Close()
	raw, e := io.ReadAll(io.LimitReader(res.Body, MaxResponse+1))
	if e != nil || len(raw) > MaxResponse {
		return &APIError{Status: res.StatusCode, Code: "response_unreadable", Message: "bounded response unavailable", Uncertain: method != "GET"}
	}
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		var env struct {
			Error struct {
				Message   string `json:"message"`
				Type      string `json:"type"`
				Code      string `json:"code"`
				RequestID string `json:"request_id"`
			} `json:"error"`
		}
		_ = json.Unmarshal(raw, &env)
		message := env.Error.Message
		if message == "" {
			message = "non-success REST response (body omitted)"
		}
		return &APIError{Status: res.StatusCode, Code: env.Error.Code, Type: env.Error.Type, Message: c.Redact(message), RequestID: c.Redact(env.Error.RequestID), Uncertain: method != "GET" && (res.StatusCode >= 500 || res.StatusCode == 408)}
	}
	if out != nil && json.Unmarshal(raw, out) != nil {
		return &APIError{Status: res.StatusCode, Code: "invalid_response", Message: "response does not match JSON contract", Uncertain: method != "GET"}
	}
	return nil
}
func (c *Client) Probe(ctx context.Context) error {
	var m map[string]any
	return c.request(ctx, "GET", "/v1/me", nil, &m)
}

// GuestPrivilegedDefault is an operator capability; callers cannot select it
// in Create. Old runtimes without discovery return an error, never an assumed true.
func (c *Client) GuestPrivilegedDefault(ctx context.Context) (bool, error) {
	var out struct {
		Capabilities struct {
			GuestPrivilegedDefault bool `json:"guest_privileged_default"`
		} `json:"capabilities"`
	}
	e := c.request(ctx, "GET", "/v1/config", nil, &out)
	return out.Capabilities.GuestPrivilegedDefault, e
}
func (c *Client) Get(ctx context.Context, id string) (*Box, error) {
	var b Box
	e := c.request(ctx, "GET", "/v1/boxes/"+url.PathEscape(id), nil, &b)
	return &b, e
}
func (c *Client) List(ctx context.Context) ([]Box, error) {
	var out struct {
		Boxes []Box `json:"boxes"`
	}
	if e := c.request(ctx, "GET", "/v1/boxes", nil, &out); e != nil {
		return nil, e
	}
	return out.Boxes, nil
}
func (c *Client) Create(ctx context.Context, s Create) (*Box, error) {
	var b Box
	e := c.request(ctx, "POST", "/v1/boxes", s, &b)
	return &b, e
}
func (c *Client) Start(ctx context.Context, id string) (*Box, error) {
	var b Box
	e := c.request(ctx, "POST", "/v1/boxes/"+url.PathEscape(id)+"/start", nil, &b)
	return &b, e
}
func (c *Client) Stop(ctx context.Context, id string) (*Box, error) {
	var b Box
	e := c.request(ctx, "POST", "/v1/boxes/"+url.PathEscape(id)+"/stop", nil, &b)
	return &b, e
}
func (c *Client) Metrics(ctx context.Context, id string) (*Metrics, error) {
	var m Metrics
	e := c.request(ctx, "GET", "/v1/boxes/"+url.PathEscape(id)+"/metrics", nil, &m)
	return &m, e
}

type Stream struct {
	Conn        *websocket.Conn
	ExecutionID string
	client      *Client
}

func (c *Client) Attach(ctx context.Context, id, executionID string) (*Stream, error) {
	return c.AttachStdin(ctx, id, executionID, false)
}

// AttachStdin uses the official native execution stream; it does not create,
// start, exec or stop a VM. A caller must already hold trusted task authority.
func (c *Client) AttachStdin(ctx context.Context, id, executionID string, writable bool) (*Stream, error) {
	u := *c.base
	u.Scheme = "ws"
	if c.base.Scheme == "https" {
		u.Scheme = "wss"
	}
	u.Path = "/v1/boxes/" + id + "/attach"
	if executionID != "" {
		u.Path = "/v1/boxes/" + id + "/executions/" + executionID + "/attach"
	}
	u.RawQuery = "stdin=0"
	if writable {
		u.RawQuery = "stdin=1"
	}
	h := http.Header{}
	if c.key != "" {
		h.Set("Authorization", "Bearer "+c.key)
	}
	conn, res, e := c.WS.DialContext(ctx, u.String(), h)
	if e != nil {
		status := 0
		if res != nil {
			status = res.StatusCode
			raw, _ := io.ReadAll(io.LimitReader(res.Body, MaxResponse+1))
			res.Body.Close()
			if len(raw) <= MaxResponse {
				var body struct {
					Error struct {
						Code, Type, Message string
						RequestID           string `json:"request_id"`
					} `json:"error"`
				}
				if json.Unmarshal(raw, &body) == nil && body.Error.Message != "" {
					return nil, &APIError{Status: status, Code: body.Error.Code, Type: body.Error.Type, Message: c.Redact(body.Error.Message), RequestID: c.Redact(body.Error.RequestID), Uncertain: status >= 500}
				}
			}
		}
		return nil, &APIError{Status: status, Code: "attach_unavailable", Message: c.Redact(e.Error()), Uncertain: status == 0 || status >= 500}
	}
	if executionID == "" {
		executionID = res.Header.Get("x-boxlite-execution-id")
	}
	if executionID == "" {
		conn.Close()
		return nil, &APIError{Code: "attach_contract", Message: "missing native execution identity"}
	}
	conn.SetReadLimit(MaxResponse)
	return &Stream{conn, executionID, c}, nil
}

// Read uses native binary stream tags. It never executes host or guest commands.
func (s *Stream) Read(ctx context.Context, stdout, stderr io.Writer) (int, error) {
	finished := make(chan struct{})
	defer close(finished)
	defer s.Conn.Close()
	go func() {
		select {
		case <-ctx.Done():
			s.Conn.Close()
		case <-finished:
		}
	}()
	for {
		kind, b, e := s.Conn.ReadMessage()
		if e != nil {
			return 0, &APIError{Code: "output_stream_lost", Message: s.client.Redact(e.Error())}
		}
		switch kind {
		case websocket.BinaryMessage:
			if len(b) == 0 {
				continue
			}
			var w io.Writer
			switch b[0] {
			case 1:
				w = stdout
			case 2:
				w = stderr
			default:
				return 0, &APIError{Code: "attach_contract", Message: "unknown output stream tag"}
			}
			if _, e = w.Write(b[1:]); e != nil {
				return 0, e
			}
		case websocket.TextMessage:
			var m struct {
				Type     string `json:"type"`
				ExitCode *int   `json:"exit_code"`
				Message  string `json:"message"`
			}
			if json.Unmarshal(b, &m) != nil {
				return 0, &APIError{Code: "attach_contract", Message: "invalid native control frame"}
			}
			if m.Type == "exit" && m.ExitCode != nil {
				return *m.ExitCode, nil
			}
			if m.Type == "error" {
				return 0, &APIError{Code: "execution_stream_error", Message: s.client.Redact(m.Message)}
			}
		}
	}
}
