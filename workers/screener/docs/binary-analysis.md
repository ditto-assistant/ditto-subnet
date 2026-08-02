# Binary-analysis tool

Before the source reviewer starts, the screener analyzes each opaque file listed
in the bounded initial inventory. Compact summaries include format, digest,
entropy, benchmark-schema markers, representative strings, and the most useful
format metadata. This gives the reviewer broad binary visibility without first
spending a model turn per file.

The reviewer can still call `analyze_binary` for a full cached result when a
summary is ambiguous. Pre-analysis and tool calls share the same per-file cache,
so a drill-down does not hash or parse the member twice. Both surfaces provide
evidence, not classifications: the reviewer must connect the facts to the
effective build and runtime path.

## Safety boundary

Analysis is implemented in-process with bounded, read-only parsers. It never:

- executes a submitted binary or model;
- invokes a shell, network service, model runtime, or external file utility;
- decompresses archive payloads;
- follows ONNX external-data references; or
- reads more than 8 MiB into the structural-analysis buffer; or
- streams more than 32 MiB of one expanded member for hashing.

Members within the normal submission bound receive a complete SHA-256. The
separate 32 MiB expanded-member cap protects automatic pre-analysis from a
highly compressed tar member; larger members receive a labeled prefix digest.
Results always report analyzed and hashed byte counts plus whether structural
analysis or hashing was truncated.

## Evidence returned

Every result includes detected format and confidence, size, SHA-256, sampled
entropy, bounded printable strings, public DittoBench schema markers, and the
safety operations that were not performed. Format-specific metadata includes:

- ONNX graph, opset, operator, input/output, initializer, and external-data
  reference counts parsed directly from bounded protobuf fields;
- safetensors header, tensor shape/type, and declared byte ranges;
- ELF, PE, Mach-O, and WebAssembly header metadata;
- ZIP entry names and compressed/uncompressed sizes without extracting them;
- SQLite, gzip, PDF, PNG, and JPEG header metadata; and
- a bounded protobuf field summary for an otherwise unknown message.

Detection uses content structure and magic bytes, never the filename suffix.
A file called `answers.onnx` receives no model exemption unless its bytes parse
as an ONNX `ModelProto`; a valid ONNX file is still not automatically safe.

## DittoBench-grounded markers

The public benchmark contracts in `ditto-assistant/dittobench-datagen` and
`ditto-assistant/dittobench-api` define canonical dataset and grading fields
such as `memory_cases`, `tool_cases`, `expected_answer`, `answer_items`,
`forbidden_answer`, and `run_after_wave`. The analyzer reports these strings
when they occur in the bounded binary sample.

Those public words are not a finding by themselves. They can legitimately
appear in official fixtures, local evaluation utilities, or unreachable test
data. The reviewer must establish that the submitted service uses the binary
as an answer registry, evaluator, hidden-value source, provider bypass, or
another prohibited runtime shortcut.
