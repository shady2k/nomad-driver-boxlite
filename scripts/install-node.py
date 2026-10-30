#!/usr/bin/env python3
"""Retired staging entrypoint. Provisioning is declarative Ansible on both hosts."""
import argparse
p=argparse.ArgumentParser(description="Use ansible-playbook -i PRIVATE_INVENTORY provision/site.yml --check, then stage. See provision/README.md. This retired entrypoint performs no installation.")
p.parse_args()
p.error("Python installer retired; use the shared Ansible boxlite_node role")
