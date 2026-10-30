log_level = "warn"
pristine = true
vault {
  renew_token = false
  retry { enabled = false }
  ssl { verify = true }
}
secret {
  path = "CONFIGURE_OPERATOR_SECRET_PATH"
  no_prefix = true
  key { name = "LLM_API_KEY" }
}
secret {
  path = "CONFIGURE_OPERATOR_SECRET_PATH"
  no_prefix = true
  key { name = "BOXLITE_DISPATCHER_SEED" }
}
exec {
  env { allowlist = ["LLM_API_KEY", "BOXLITE_DISPATCHER_SEED"] }
}
