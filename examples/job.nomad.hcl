# DRAFT ONLY. Supply a trusted image digest and matching node architecture.
# No Prime/acpx dependency, environment secrets, mounts or network access.
variable "image" { type = string }
variable "architecture" { type = string }
job "boxlite-demo" {
  type = "batch"
  datacenters = ["dc1"]
  constraint {
    attribute = "${attr.cpu.arch}"
    value = var.architecture
  }
  constraint {
    attribute = "${attr.driver.boxlite.api}"
    value = "v1"
  }
  group "guest" {
    restart {
      attempts = 0
      mode = "fail"
    }
    reschedule {
      attempts = 0
      unlimited = false
    }
    # Replacement is a Nomad policy, not a new VM created by driver recovery.
    disconnect {
      lost_after = "2h"
      replace = false
      reconcile = "keep_original"
    }
    task "demo" {
      driver = "boxlite"
      config {
        image = var.image
        command = ["/bin/echo", "hello from an isolated guest"]
        guest_cpus = 1
        memory_mib = 512
        disk_gib = 12
      }
      resources {
        cpu = 1000
        memory = 1536 #512 guest+1024 configured overhead
      }
    }
  }
}
