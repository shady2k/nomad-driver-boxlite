package driver

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"github.com/hashicorp/nomad/plugins/drivers"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type NodeBudget struct {
	MaxVMs            int `json:"max_vms"`
	MaxGuestCPUs      int `json:"max_guest_cpus"`
	MaxReservationMiB int `json:"max_reservation_mib"`
	OverheadMiB       int `json:"memory_overhead_mib"`
	CPUMHzPerVCPU     int `json:"cpu_mhz_per_vcpu"`
	DefaultDiskGiB    int `json:"default_disk_gib"`
	MaxDiskGiB        int `json:"max_disk_gib"`
}

func (b NodeBudget) validate() error {
	if b.MaxVMs <= 0 || b.MaxGuestCPUs <= 0 || b.MaxReservationMiB <= 0 || b.OverheadMiB <= 0 || b.CPUMHzPerVCPU <= 0 || b.DefaultDiskGiB <= 0 || b.MaxDiskGiB <= 0 || b.DefaultDiskGiB > b.MaxDiskGiB {
		return fmt.Errorf("all budget fields must be positive; default disk must fit maximum")
	}
	return nil
}
func (d *Driver) policyError(code string) error {
	return fmt.Errorf("boxlite_policy_error code=%s variable=%s:%s node-policy=real-NodeID; no VM create", code, d.config.PolicyNamespace, d.config.PolicyPath)
}
func (d *Driver) readNodePolicy(cfg *drivers.TaskConfig) (NodeBudget, uint64, error) {
	var zero NodeBudget
	if d.config.PolicyEndpoint == "" || d.config.PolicyNamespace == "" || d.config.PolicyPath == "" || cfg.NodeID == "" {
		return zero, 0, d.policyError("policy_source_not_configured")
	}
	token := cfg.Env["NOMAD_TOKEN"]
	parts := strings.Split(token, ".")
	if len(parts) != 3 || len(token) > 16384 {
		return zero, 0, d.policyError("signed_workload_identity_missing")
	}
	raw, e := base64.RawURLEncoding.DecodeString(parts[1])
	if e != nil {
		return zero, 0, d.policyError("identity_invalid")
	}
	var claims struct {
		Namespace    string `json:"nomad_namespace"`
		AllocationID string `json:"nomad_allocation_id"`
		Task         string `json:"nomad_task"`
		Job          string `json:"nomad_job_id"`
	}
	if json.Unmarshal(raw, &claims) != nil || claims.Namespace != cfg.Namespace || claims.Namespace != d.config.PolicyNamespace || claims.AllocationID != cfg.AllocID || claims.Task != cfg.Name || claims.Job != cfg.JobID {
		return zero, 0, d.policyError("identity_binding_mismatch")
	}
	// Claims above are untrusted until Nomad verifies this exact JWT on the HTTP read.
	endpoint, e := url.Parse(d.config.PolicyEndpoint)
	if e != nil || endpoint.Scheme != "http" || net.ParseIP(endpoint.Hostname()) == nil || !net.ParseIP(endpoint.Hostname()).IsLoopback() || endpoint.User != nil || endpoint.Path != "" || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		return zero, 0, d.policyError("invalid_local_policy_endpoint")
	}
	ctx, cancel := context.WithTimeout(d.ctx, 5*time.Second)
	defer cancel()
	req, e := http.NewRequestWithContext(ctx, "GET", endpoint.String()+"/v1/var/"+d.config.PolicyPath+"?namespace="+url.QueryEscape(d.config.PolicyNamespace), nil)
	if e != nil {
		return zero, 0, d.policyError("invalid_policy_path")
	}
	req.Header.Set("X-Nomad-Token", token)
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 5 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	res, e := client.Do(req)
	if e != nil {
		return zero, 0, d.policyError("server_unreachable")
	}
	defer res.Body.Close()
	if res.StatusCode == 404 {
		return zero, 0, d.policyError("node_budget_not_configured")
	}
	if res.StatusCode == 403 || res.StatusCode == 401 {
		return zero, 0, d.policyError("identity_not_authorized")
	}
	if res.StatusCode != 200 {
		return zero, 0, d.policyError("policy_read_failed")
	}
	body, e := io.ReadAll(io.LimitReader(res.Body, 65537))
	if e != nil || len(body) > 65536 {
		return zero, 0, d.policyError("policy_response_invalid")
	}
	var variable struct {
		Items       map[string]string
		ModifyIndex uint64
	}
	if json.Unmarshal(body, &variable) != nil {
		return zero, 0, d.policyError("policy_response_invalid")
	}
	var catalog struct {
		Version int                   `json:"version"`
		Nodes   map[string]NodeBudget `json:"nodes"`
	}
	dec := json.NewDecoder(strings.NewReader(variable.Items["boxlite_node_budgets"]))
	dec.DisallowUnknownFields()
	if dec.Decode(&catalog) != nil || catalog.Version != 1 || catalog.Nodes == nil || dec.Decode(&struct{}{}) != io.EOF {
		return zero, 0, d.policyError("policy_malformed")
	}
	b, ok := catalog.Nodes[cfg.NodeID]
	if !ok {
		return zero, 0, d.policyError("node_budget_not_configured")
	}
	if b.validate() != nil {
		return zero, 0, d.policyError("policy_invalid")
	}
	return b, variable.ModifyIndex, nil
}
