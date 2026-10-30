# BoxLite runtime limitations

This project maintains the Nomad driver and Claude worker plugin. Development
of a BoxLite runtime fork has stopped. No custom BoxLite patch, DNS shim or
firmware is part of the published project.

## Official BoxLite 0.10.8

The official guest kernel does not provide all features required for full Docker
networking end to end. Installing Docker tools alone does not establish working
bridge/NAT, network namespaces, TUN, traffic control or writable cgroups. Do not
advertise Docker network e2e support for the official runtime.

The 0.10.8 REST request has no privileged field or operator guest privileged
setting. The native SDK having a privileged option does not make that option
available through this driver's REST integration. Existing discovery of optional
runtime capabilities fails closed when the server does not advertise them.
Operator feature declarations must describe capabilities actually provided by
the selected runtime; declarations cannot add missing runtime support.

A custom netgo shim was installed privately for Linux DNS. It is not a stock
BoxLite component, is not distributed here, and is not evidence of official
runtime DNS behavior. There is no instruction here to rebuild or deploy it.

## Driver and image behavior

An image containing Docker tools is an optional recipe. An image marker can ask
the trusted bootstrap to start its fixed Docker script before forwarding session
secrets. Bootstrap failure refuses ACP readiness. The marker does not supply a
kernel feature or change runtime privilege. Required guest feature constraints
must fail closed on stock runtimes that cannot satisfy them.

Guest isolation remains mandatory: never mount a host Docker socket, host
filesystems or host /dev/kvm into a worker. This publication does not change
running VMs or install or activate a runtime.

Disk requests remain bounded by operator policy and native Nomad group disk
reservations. Optional free-space checks include retained disks; no automatic
VM close or disk deletion is enabled.
