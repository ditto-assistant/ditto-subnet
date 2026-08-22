package traces

// Metrics is the sink for the spool/uploader counters. The relay wires a
// Prometheus implementation (internal/metrics); tests and the backfill CLI
// use NopMetrics or a recorder.
type Metrics struct {
	Recorded   func(event, lane, kind string, bytes int64)
	Dropped    func(reason string)
	Rotated    func(reason string, records, bytes int64)
	Uploaded   func(sink string, bytes int64)
	UploadFail func(sink string)
	SpoolBytes func(bytes int64)
	Released   func(files int64)
}

// NopMetrics returns a Metrics whose hooks do nothing.
func NopMetrics() *Metrics {
	return &Metrics{
		Recorded:   func(string, string, string, int64) {},
		Dropped:    func(string) {},
		Rotated:    func(string, int64, int64) {},
		Uploaded:   func(string, int64) {},
		UploadFail: func(string) {},
		SpoolBytes: func(int64) {},
		Released:   func(int64) {},
	}
}
