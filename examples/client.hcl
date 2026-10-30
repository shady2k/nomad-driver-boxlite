# Operator example; paths/provider must be selected during node bootstrap.
telemetry {
  publish_allocation_metrics = true
}
plugin "nomad-driver-boxlite" {
  config {
    endpoint = "http://127.0.0.1:REPLACE_WITH_OPERATOR_PORT"
    state_dir = "/ABSOLUTE/OPERATOR-SELECTED/PRIVATE/driver-state"
    api_key_command = ["/ABSOLUTE/EXISTING/APPROVED/credential-provider"]
    policy_endpoint = "http://127.0.0.1:4646"
    policy_namespace = "boxlite-prime"
    policy_path = "nomad/jobs"
    outbound_allow_all = false # Configure explicitly; this example grants no unrestricted egress
    outbound_allow_net = []
    start_timeout = "15m"
    # Numeric budgets and overhead come from operator-owned Nomad Variables.
  }
}
