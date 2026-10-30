package driver

import (
	"fmt"
	"strconv"
	"time"
)

// Process-local diagnostics only. They never determine VM lifetime or budget.
// Repeated gap notices are rate limited; every skip remains counted.
const statsGapNoticeInterval = time.Minute

type statsDiagnostics struct {
	Streams, SamplesSent, Skipped, WarmupSkipped, NoticesSuppressed uint64
	ActiveStreams                                                   int
	LastMeasuredNS, LastDeliveredNS, LastGapNS                      int64
	CurrentGap, LastGap, LastStreamEnd                              string
	LastHTTPStatus                                                  int
	lastNotice                                                      time.Time
	reportedGap                                                     bool
}

func (s *statsDiagnostics) attributes() map[string]string {
	return map[string]string{
		"metrics_streams_started": strconv.FormatUint(s.Streams, 10), "metrics_streams_active": strconv.Itoa(s.ActiveStreams),
		"metrics_samples_sent": strconv.FormatUint(s.SamplesSent, 10), "metrics_samples_skipped": strconv.FormatUint(s.Skipped, 10),
		"metrics_warmup_skipped": strconv.FormatUint(s.WarmupSkipped, 10), "metrics_notices_suppressed": strconv.FormatUint(s.NoticesSuppressed, 10),
		"metrics_last_measured_ns": strconv.FormatInt(s.LastMeasuredNS, 10), "metrics_last_delivered_ns": strconv.FormatInt(s.LastDeliveredNS, 10),
		"metrics_last_gap_ns": strconv.FormatInt(s.LastGapNS, 10), "metrics_current_gap": s.CurrentGap, "metrics_last_gap": s.LastGap,
		"metrics_last_gap_http_status": strconv.Itoa(s.LastHTTPStatus), "metrics_last_stream_end": s.LastStreamEnd,
	}
}
func (d *Driver) statsStarted(t *task, interval time.Duration) uint64 {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.stats.Streams++
	t.stats.ActiveStreams++
	id := t.stats.Streams
	d.emit(t, fmt.Sprintf("metrics_stream_started stream=%d interval_ms=%d", id, interval.Milliseconds()), nil)
	return id
}
func (d *Driver) statsStopped(t *task, id uint64, reason string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.stats.ActiveStreams > 0 {
		t.stats.ActiveStreams--
	}
	t.stats.LastStreamEnd = reason
	d.emit(t, fmt.Sprintf("metrics_stream_stopped stream=%d reason=%s", id, reason), nil)
}
func (d *Driver) statsSkipped(t *task, id uint64, reason string, httpStatus int, at time.Time) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if reason == "cpu_delta_warmup" {
		t.stats.WarmupSkipped++
		d.emit(t, fmt.Sprintf("metrics_warmup_sample_skipped stream=%d reason=cpu_delta_warmup", id), nil)
		return
	}
	s := &t.stats
	s.Skipped++
	s.CurrentGap = reason
	s.LastGap = reason
	s.LastGapNS = at.UnixNano()
	s.LastHTTPStatus = httpStatus
	if s.lastNotice.IsZero() || at.Sub(s.lastNotice) >= statsGapNoticeInterval {
		s.lastNotice = at
		s.reportedGap = true
		d.emit(t, fmt.Sprintf("metrics_sample_missing stream=%d reason=%s http_status=%d skipped_total=%d", id, reason, httpStatus, s.Skipped), nil)
		d.logger.Warn("metrics_sample_missing", "allocation_id", t.config.AllocID, "stream", id, "reason", reason, "http_status", httpStatus, "skipped_total", s.Skipped)
	} else {
		s.NoticesSuppressed++
	}
}
func (d *Driver) statsMeasured(t *task, at time.Time) {
	t.mu.Lock()
	t.stats.LastMeasuredNS = at.UnixNano()
	t.mu.Unlock()
}
func (d *Driver) statsDelivered(t *task, id uint64, at time.Time, first bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	s := &t.stats
	s.SamplesSent++
	s.LastDeliveredNS = at.UnixNano()
	s.CurrentGap = ""
	if s.reportedGap {
		d.emit(t, fmt.Sprintf("metrics_samples_resumed stream=%d skipped_total=%d last_gap=%s", id, s.Skipped, s.LastGap), nil)
		s.reportedGap = false
	} else if first {
		d.emit(t, fmt.Sprintf("metrics_samples_started stream=%d", id), nil)
	}
}
