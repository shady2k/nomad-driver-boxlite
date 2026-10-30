package main

import (
	"context"
	log "github.com/hashicorp/go-hclog"
	"github.com/hashicorp/nomad/plugins"
	"nomad-driver-boxlite/internal/driver"
)

func main() {
	plugins.ServeCtx(func(ctx context.Context, l log.Logger) any { return driver.New(ctx, l) })
}
